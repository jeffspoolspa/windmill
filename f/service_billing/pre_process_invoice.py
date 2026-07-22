# requirements:
# psycopg2-binary
# requests
# wmill

# f/service_billing/pre_process_invoice — the enrichment event handler.
#
# Deterministic enrichment of ONE newly-linked service invoice, as a sentence
# over f/billing/_lib: apply matched credits (apply_credits service, matcher
# stays policy here) -> resolve the payment route -> derive class -> compose
# the memo (deterministic, else LLM) -> PATCH QBO (update_invoice_sparse) ->
# echo the cache -> record facts (enrichment_ok, pre_processed_at, source
# fields). billing_status / needs_review_reason are OWNED by the projection
# triggers — this script stamps no status and reads the projection back only
# for its return value.
#
# Trigger: pg_net on invoice link (happy path) + dispatch_pre_processing
# (60s outbox backstop). Per-caller 429 retry was dropped with the local QBO
# plumbing — the dispatcher serializes (limit 1, 25/tick); the shared token
# bucket (ADR 008 §4, pending in _lib/qbo) is the structural fix.

import calendar
import json
import random
import time
from datetime import date as _date

import psycopg2.extras
import requests
import wmill

from f.billing._lib.db import get_db_conn
from f.billing._lib.qbo import (
    set_rate_limiter, refresh_qbo_token, fetch_qbo_invoice, fetch_qbo_classes,
    update_invoice_sparse,
)
from f.billing._lib.payments import load_applicable_credits, apply_credits
from f.billing._lib.cache import echo_invoice
from f.service_billing.refresh_customer_credits import main as refresh_customer_credits

OPENAI_KEY_VAR = "f/service_billing/OPENAI_API_KEY"
MEMO_CONFIDENCE_THRESHOLD = 0.85
MODEL = "gpt-4o-mini"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

STAGE_FETCHING = "fetching_qbo"
STAGE_CREDITS = "matching_credits"
STAGE_PAYMENT_METHOD = "resolving_payment_method"
STAGE_CLASS = "deriving_class"
STAGE_MEMO = "generating_memo"
STAGE_WRITING = "writing_qbo"
STAGE_DONE = "done"


# ── engine reads + fact writes ───────────────────────────────────────────────

def _row(conn, sql, params):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None


def _exec(conn, sql, params):
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit(); cur.close()


def set_stage(conn, qbo_invoice_id, stage):
    # live-progress fact for the UI; best-effort
    try:
        _exec(conn, "UPDATE billing.invoices SET pre_process_stage = %s WHERE qbo_invoice_id = %s",
              (stage, qbo_invoice_id))
    except Exception as e:
        print(f"  (set_stage warning: {e})")


def load_invoice(conn, qbo_invoice_id):
    return _row(conn, """SELECT i.*, s.derived_status
                         FROM billing.invoices i
                         JOIN billing.v_invoice_status s USING (qbo_invoice_id)
                         WHERE i.qbo_invoice_id = %s""", (qbo_invoice_id,))


def load_linked_wo(conn, qbo_invoice_id):
    return _row(conn, "SELECT * FROM public.work_orders WHERE qbo_invoice_id = %s LIMIT 1",
                (qbo_invoice_id,))


def is_memo_locked(invoice):
    return bool(invoice.get("memo_locked")) and bool(invoice.get("memo"))


def mark_enrichment_failed(conn, qbo_invoice_id):
    """Failure fact: enrichment_ok=false + pre_processed_at. The projection
    composes billing_status=needs_review / reason from it."""
    _exec(conn, """UPDATE billing.invoices
                   SET enrichment_ok = false, pre_processed_at = now(),
                       pre_process_stage = %s
                   WHERE qbo_invoice_id = %s""", (STAGE_DONE, qbo_invoice_id))


def write_result(conn, qbo_invoice_id, result):
    """Source-of-truth facts only. The single UPDATE fires the maintenance
    triggers (payment_method_ok etc.) + projection; this script never writes
    billing_status / needs_review_reason / subtotal_ok."""
    _exec(conn, """UPDATE billing.invoices
        SET payment_method            = %s,
            preferred_payment_type    = %s,
            target_payment_method_id  = %s,
            qbo_class                 = %s,
            memo                      = %s,
            statement_memo            = %s,
            memo_locked               = %s,
            enrichment_ok             = %s,
            credits_applied           = %s::jsonb,
            pre_processed_at          = now(),
            pre_process_stage         = %s
        WHERE qbo_invoice_id = %s
    """, (result.get("payment_method"), result.get("preferred_payment_type"),
          result.get("target_payment_method_id"), result.get("qbo_class"),
          result.get("memo"), result.get("statement_memo"),
          bool(result.get("memo_locked")), result.get("enrichment_ok"),
          json.dumps(result.get("credits_applied") or []), STAGE_DONE,
          qbo_invoice_id))


# ── decision-record writes (migration 20260722150834) ───────────────────────
# The pre-process row is the gate state machine; invoice_credit_decisions is
# the frozen candidate snapshot: EVERY open credit the matcher saw gets a row
# (reason non-null = auto-recommended). Terminal rows are history; only
# 'candidate' rows are ever maintained afterwards (by refresh_payment).

# Matches on the WO number are deterministic -> auto-apply. Amount heuristics
# (full_cover / half_deposit) stay 'candidate' for human review.
DETERMINISTIC_REASONS = {"wo_number_in_ref_num", "wo_number_in_memo"}


def credits_cache_fresh(conn, max_sweep_age_min=20):
    """DB-only freshness evidence for the credit cache — no QBO call.
    Green when BOTH hold:
      1. the stream is drained: no live qbo_inbox rows for Payment/CreditMemo
         (dead-letter rows attempts>=3 excluded — they'd block forever), and
      2. the CDC sweep ran recently and didn't fail (the unknown-unknowns
         bound: anything the stream missed is at most one sweep old).
    Returns (fresh, checked_at). checked_at is the provenance timestamp."""
    row = _row(conn, """SELECT
        NOT EXISTS (SELECT 1 FROM billing.qbo_inbox
                    WHERE finished_at IS NULL AND attempts < 3
                      AND entity_type IN ('Payment','CreditMemo')) AS inbox_drained,
        EXISTS (SELECT 1 FROM billing.cdc_cursors
                WHERE source = 'qbo'
                  AND last_run_status IN ('succeeded','partial')
                  AND last_run_at > now() - make_interval(mins => %s)) AS sweep_recent,
        now() AS checked_at""", (max_sweep_age_min,))
    fresh = bool(row and row["inbox_drained"] and row["sweep_recent"])
    if not fresh and row:
        print(f"  credit cache not provably fresh (inbox_drained={row['inbox_drained']}, "
              f"sweep_recent={row['sweep_recent']}) — falling back to QBO read-through")
    return fresh, (row["checked_at"] if row else None)


def upsert_pre_process_row(conn, qbo_invoice_id, credits_verified_at=None):
    _exec(conn, """INSERT INTO billing.invoice_pre_process
                     (qbo_invoice_id, state, credits_verified_at)
                   VALUES (%s, 'deciding', %s)
                   ON CONFLICT (qbo_invoice_id) DO UPDATE SET
                     state = 'deciding',
                     credits_verified_at = COALESCE(EXCLUDED.credits_verified_at,
                                                    billing.invoice_pre_process.credits_verified_at),
                     updated_at = now()""",
          (qbo_invoice_id, credits_verified_at))


def record_credit_decisions(conn, qbo_invoice_id, open_credits, reason_by_id):
    """One 'candidate' row per open credit considered, matched or not."""
    cur = conn.cursor()
    for c in open_credits:
        cur.execute("""INSERT INTO billing.invoice_credit_decisions
                         (qbo_invoice_id, credit_id, amount, unapplied_at_decision, reason)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (qbo_invoice_id, credit_id) DO UPDATE SET
                         amount = EXCLUDED.amount,
                         unapplied_at_decision = EXCLUDED.unapplied_at_decision,
                         reason = EXCLUDED.reason
                       WHERE billing.invoice_credit_decisions.state = 'candidate'""",
                    (qbo_invoice_id, c["qbo_payment_id"],
                     float(c.get("unapplied_amt") or 0),
                     float(c.get("unapplied_amt") or 0),
                     reason_by_id.get(c["qbo_payment_id"])))
    conn.commit(); cur.close()


def mark_decision_applied(conn, qbo_invoice_id, credit_id, amount):
    _exec(conn, """UPDATE billing.invoice_credit_decisions
                   SET state = 'applied', amount = %s, decided_by = 'auto',
                       applied_via = 'pre_process', decided_at = now(), applied_at = now()
                   WHERE qbo_invoice_id = %s AND credit_id = %s AND state = 'candidate'""",
          (amount, qbo_invoice_id, credit_id))


def read_projected_status(conn, qbo_invoice_id):
    row = _row(conn, "SELECT billing_status, needs_review_reason FROM billing.invoices WHERE qbo_invoice_id = %s",
               (qbo_invoice_id,))
    return (row["billing_status"], row["needs_review_reason"]) if row else (None, None)


# ── the sentences: route, class, credit matching, memo (all policy) ─────────

def resolve_payment_for_invoice(conn, qbo_customer_id, wo_description):
    cur = conn.cursor()

    cur.execute(
        "SELECT billing.resolve_preferred_payment_type(%s, %s)",
        (qbo_customer_id, wo_description),
    )
    preferred = cur.fetchone()[0]

    cur.execute(
        "SELECT billing.pick_target_payment_method(%s, %s)",
        (qbo_customer_id, preferred),
    )
    target_pm_id = cur.fetchone()[0]

    cur.close()

    legacy = 'invoice' if preferred == 'email' else 'on_file'

    return {
        "preferred":              preferred,
        "legacy_payment_method":  legacy,
        "target_pm_id":           target_pm_id,
    }


def derive_qbo_class(assigned_to, wo_type, description):
    assigned = (assigned_to or "").upper()
    desc = (description or "").lower()
    wo = (wo_type or "").upper()
    if assigned.startswith("MNT-"):
        return "Maintenance"
    if wo == "DELIVERY" or (assigned.startswith("SVC-") and "deliver" in desc and len(desc) < 80):
        return "Delivery"
    if "renovation" in desc or "replaster" in desc or "retile" in desc:
        return "Renovation"
    return "Service"


def match_credits_to_wo(open_credits, wo, qbo_inv=None):
    wo_number = wo.get("wo_number")
    wo_subtotal = float(wo.get("sub_total") or 0)
    qbo_total = float((qbo_inv or {}).get("TotalAmt") or 0)
    qbo_balance = float((qbo_inv or {}).get("Balance") or 0)
    full_targets = [t for t in (wo_subtotal, qbo_total, qbo_balance) if t > 0]
    half_targets = [round(t / 2, 2) for t in full_targets]

    def close(a, b):
        return abs(a - b) < 0.01

    matches = []
    for c in open_credits:
        memo = (c.get("memo") or "").lower()
        ref_num = (c.get("ref_num") or "").lower()
        unapplied = float(c.get("unapplied_amt") or 0)
        if unapplied <= 0:
            continue
        match_reason = None
        wo_l = (wo_number or "").lower()
        if wo_l and wo_l in ref_num:
            match_reason = "wo_number_in_ref_num"
        elif wo_l and wo_l in memo:
            match_reason = "wo_number_in_memo"
        elif any(close(unapplied, t) for t in full_targets):
            match_reason = "full_cover"
        elif any(close(unapplied, t) for t in half_targets):
            match_reason = "half_deposit"
        if match_reason:
            matches.append((c, unapplied, match_reason))
    return matches


MEMO_PROMPT = """You write a short customer-friendly memo for a pool service invoice.

Input: a JSON object with these fields:
- customer: customer name as it appears in QBO (may be "LAST, FIRST" or "First Last")
- type: work order type (e.g. "GENERAL SERVICE", "DELIVERY", "MAINTENANCE")
- description: what the technician was sent to do
- corrective: what the technician actually did - usually most reliable
- tech_instructions: notes from the office about the job - often clarifies ambiguity

Output: a JSON object with:
- memo: the memo text (NO WO number prefix - just the service description)
- confidence: 0.0 to 1.0 - how confident you are you understand what was done
- reasoning: 1 sentence

Style rules:
- Title Case, 2-7 words. NEVER more than 7 words.
- Equipment + Action format: "Autofill Valve Replacement", "Pool Pump Diagnosis"
- Use "&" to join two related items: "Salt Cell Cleaning & Filter Replacement"
- Use " - " for a qualifier: "Water Chemistry Service - Shock Treatment"
- Add context when meaningful: "Pre-Purchase Pool Inspection", "VSP Pump Error Diagnosis"
- Action words: Diagnosis, Replacement, Repair, Install, Delivery, Cleaning, Removal, Check, Clearing, Service
- No trailing punctuation
- Lean on `corrective` over `description`; use `tech_instructions` to disambiguate

**SPECIAL CUSTOMER RULE — ROBERT O'BRIEN (3-pool property)**

If the `customer` field contains BOTH "ROBERT" AND ("O'BRIEN" or "OBRIEN") —
case-insensitive, any order ("ROBERT O'BRIEN", "O'BRIEN, ROBERT",
"obrien robert" all qualify) — this rule applies:

1. The memo body describes the SERVICE only. Do NOT include the pool name
   in the body.
2. The memo MUST END with EXACTLY ONE of these tags (uppercase, in parens):
       (LAP POOL)
       (VOLLEYBALL)
       (SPA)
3. The tag is REQUIRED. The tag does NOT count toward the 7-word memo limit.
4. Pick the tag by scanning description, corrective, and tech_instructions
   for these keywords (case-insensitive):
       "lap pool"                               → (LAP POOL)
       "volleyball" / "vball" / "v-ball"        → (VOLLEYBALL)
       "spa"                                    → (SPA)
5. If you cannot find ANY pool keyword in the inputs, return confidence
   below 0.6 — DO NOT guess.

✅ CORRECT format (note: action first, tag at the end, ALL CAPS in parens):
   "Heat Exchanger Diagnosis (VOLLEYBALL)"
   "Spa Heater Repair (SPA)"
   "Booster Pump Replacement (LAP POOL)"
   "Salt Cell Cleaning & Filter Replacement (LAP POOL)"

❌ WRONG format (do NOT produce any of these):
   "Volleyball Pool Heat Exchanger Diagnosis"    ← pool name in body, no tag
   "Heat Exchanger Diagnosis (Volleyball Pool)"  ← wrong tag wording/case
   "Heat Exchanger Diagnosis VOLLEYBALL"         ← missing parens
   "Heat Exchanger Diagnosis"                    ← tag missing entirely
   "Volleyball Heat Exchanger Diagnosis (VOLLEYBALL)"  ← redundant pool reference

If you cannot figure out what was done, return confidence below 0.6.

Return ONLY valid JSON matching the schema."""

MEMO_EXAMPLES = [
    {"input": {"customer": "Smith, Jo", "type": "POOL INSPECTION", "description": "Pool inspection", "corrective": "Pool Inspection", "tech_instructions": ""},
     "output": {"memo": "Pool Inspection", "confidence": 0.97, "reasoning": "Straight pool inspection."}},
    {"input": {"customer": "Doe, John", "type": "GENERAL SERVICE", "description": "Valve was clogged with leaves and a wiffle ball.", "corrective": "Unclogged valve with leaves and wiffle ball.", "tech_instructions": ""},
     "output": {"memo": "Clogged Valve Clearing", "confidence": 0.96, "reasoning": "Valve was clogged and cleared."}},
    {"input": {"customer": "Williams, Bob", "type": "DIAGNOSIS", "description": "Electric heater making buzzing noise, then clicks off every ~3 min.", "corrective": "Found bad capacitor. Replaced. Unit started right up.", "tech_instructions": ""},
     "output": {"memo": "Electric Heater Diagnosis", "confidence": 0.95, "reasoning": "Electric heater diagnosed and repaired."}},
    {"input": {"customer": "Jones, Mary", "type": "GENERAL SERVICE", "description": "Remove Pool Cover", "corrective": "Removed cover.", "tech_instructions": ""},
     "output": {"memo": "Pool Cover Removal", "confidence": 0.98, "reasoning": "Pool cover removed."}},
    {"input": {"customer": "Brown, Alice", "type": "MAINTENANCE", "description": "Clean salt cell and replace filter.", "corrective": "Cleaned salt cell. Installed the filter no problem.", "tech_instructions": ""},
     "output": {"memo": "Salt Cell Cleaning & Filter Replacement", "confidence": 0.92, "reasoning": "Both services done."}},
    {"input": {"customer": "Davis, Chuck", "type": "DELIVERY", "description": "Deliver a 50lb bucket of chlorine tabs", "corrective": "Delivered", "tech_instructions": ""},
     "output": {"memo": "Chlorine Tab Delivery", "confidence": 0.98, "reasoning": "Standard chemical delivery."}},
    {"input": {"customer": "Wilson, Tom", "type": "GENERAL SERVICE", "description": "Spa Pump running loud. Motor + seal plate needed.", "corrective": "Installed new plate and motor.", "tech_instructions": ""},
     "output": {"memo": "Spa Pump Motor & Seal Plate Replacement", "confidence": 0.96, "reasoning": "Spa pump motor + seal plate replacement."}},
    {"input": {"customer": "Anderson, Pat", "type": "POOL INSPECTION", "description": "Pool Inspection. Due diligence 3/25 or 3/26. Potential buyer access.", "corrective": ".", "tech_instructions": ""},
     "output": {"memo": "Pre-Purchase Pool Inspection", "confidence": 0.93, "reasoning": "Pool inspection for potential buyer."}},
    {"input": {"customer": "Miller, Sam", "type": "DIAGNOSIS", "description": "Heater not firing", "corrective": "Replaced thermistor.", "tech_instructions": "Customer reports gas heater showing IF code intermittently"},
     "output": {"memo": "Gas Heater Diagnosis & Thermistor Replacement", "confidence": 0.93, "reasoning": "Tech instructions clarified gas heater + IF code; thermistor replaced."}},
    {"input": {"customer": "O'BRIEN, ROBERT", "type": "GENERAL SERVICE", "description": "Replace lid assembly on commercial chlorinator on the volleyball pool.", "corrective": "Installed new lid assembly. Tested and functional.", "tech_instructions": ""},
     "output": {"memo": "Commercial Chlorinator Lid Assembly Replacement (VOLLEYBALL)", "confidence": 0.95, "reasoning": "Volleyball pool chlorinator lid replaced."}},
    {"input": {"customer": "ROBERT O'BRIEN", "type": "DIAGNOSIS", "description": "Heater not firing", "corrective": "Replaced thermistor", "tech_instructions": "Spa heater issue - check IF code"},
     "output": {"memo": "Spa Heater Diagnosis & Thermistor Replacement (SPA)", "confidence": 0.93, "reasoning": "Tech instructions specified spa heater."}},
    {"input": {"customer": "O'BRIEN, ROBERT", "type": "GENERAL SERVICE", "description": "Replaced O-ring", "corrective": "O-ring replaced", "tech_instructions": ""},
     "output": {"memo": "O-Ring Replacement", "confidence": 0.45, "reasoning": "O'Brien WO but no pool name in any field - cannot determine which pool."}},
    {"input": {"customer": "O'BRIEN, ROBERT", "type": "DIAGNOSIS", "description": "Travis received call the Vball pool drained on Saturday. Need to diagnose. Customer filling, equipment off.", "corrective": "Diagnosed. Volley ball heat exchanger cracked draining pool. Shut off bypass to faulty heat pump.", "tech_instructions": ""},
     "output": {"memo": "Heat Exchanger Diagnosis (VOLLEYBALL)", "confidence": 0.95, "reasoning": "Vball/volleyball mentioned in both description and corrective — heat exchanger diagnosis on the volleyball pool."}},
    {"input": {"customer": "O'BRIEN, ROBERT", "type": "GENERAL SERVICE", "description": "Lap pool booster pump making grinding noise", "corrective": "Replaced booster pump motor and seal", "tech_instructions": ""},
     "output": {"memo": "Booster Pump Motor & Seal Replacement (LAP POOL)", "confidence": 0.96, "reasoning": "Lap pool explicitly named; booster pump motor + seal replacement."}},
    {"input": {"customer": "O'BRIEN, ROBERT", "type": "GENERAL SERVICE", "description": "Salt cell needs cleaning on volleyball", "corrective": "Cleaned salt cell, replaced o-rings", "tech_instructions": ""},
     "output": {"memo": "Salt Cell Cleaning & O-Ring Replacement (VOLLEYBALL)", "confidence": 0.94, "reasoning": "Volleyball pool salt cell cleaning + o-ring replacement."}},
]


def deterministic_memo(wo, invoice):
    desc = (wo.get("work_description") or "").lower()
    corr = (wo.get("corrective_action") or "").lower()
    instr = (wo.get("technician_instructions") or "").lower()
    haystack = f"{desc} {corr} {instr}"

    if "not on consumables" in haystack:
        date_val = (invoice or {}).get("txn_date") or wo.get("completed")
        if date_val:
            try:
                if isinstance(date_val, str):
                    d = _date.fromisoformat(date_val[:10])
                else:
                    d = date_val
                month_name = calendar.month_name[d.month]
                return {
                    "memo": f"{month_name} Supplies",
                    "confidence": 1.0,
                    "reasoning": "Monthly maintenance supplies (description marked 'not on consumables').",
                }
            except (ValueError, AttributeError):
                pass

    return None


_OBRIEN_POOL_TAGS = ("(LAP POOL)", "(VOLLEYBALL)", "(SPA)")


def _is_obrien_customer(name):
    if not name:
        return False
    n = name.lower().replace(",", " ")
    return ("robert" in n) and ("obrien" in n or "o'brien" in n)


def _has_obrien_pool_tag(memo):
    if not memo:
        return False
    upper = memo.upper()
    return any(tag in upper for tag in _OBRIEN_POOL_TAGS)


def generate_memo(wo, invoice, api_key, max_retries=3):
    customer_name = (invoice or {}).get("customer_name") or wo.get("customer") or ""
    user_payload = {
        "customer": customer_name,
        "type": wo.get("type"),
        "description": wo.get("work_description") or "",
        "corrective": wo.get("corrective_action") or "",
        "tech_instructions": wo.get("technician_instructions") or "",
    }
    user_msg = json.dumps(user_payload)

    examples_text = "\n\nExamples:"
    for ex in MEMO_EXAMPLES:
        examples_text += f"\nInput: {json.dumps(ex['input'])}\nOutput: {json.dumps(ex['output'])}\n"

    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": MEMO_PROMPT + examples_text},
            {"role": "user", "content": user_msg},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "memo_response",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "memo": {"type": "string"},
                        "confidence": {"type": "number"},
                        "reasoning": {"type": "string"},
                    },
                    "required": ["memo", "confidence", "reasoning"],
                    "additionalProperties": False,
                },
            },
        },
        "max_tokens": 256,
        "temperature": 0.2,
    }

    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(
                OPENAI_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body, timeout=30,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = f"OpenAI network error: {e}"
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 30))
                continue
            break

        if resp.ok:
            try:
                content = resp.json()["choices"][0]["message"]["content"]
                usage = resp.json().get("usage") or {}
                cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                total_in = usage.get("prompt_tokens", 0)
                print(f"  openai usage: prompt={total_in} (cached={cached}), out={usage.get('completion_tokens', 0)}")
                return json.loads(content)
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                return {"error": f"Failed to parse OpenAI response: {e}"}

        last_err = f"OpenAI API {resp.status_code}: {resp.text[:200]}"
        if resp.status_code == 429 and attempt < max_retries:
            retry_after = resp.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                base = min(int(retry_after), 30)
            else:
                base = min(2 ** attempt, 30)
            time.sleep(base + random.random() * base * 0.5)
            continue
        break

    return {"error": last_err}


def process_one(conn, qbo_invoice_id, access_token, realm_id, api_key, force=False):
    """The enrichment sentence: load -> credits -> route -> class -> memo ->
    PATCH QBO -> echo -> record facts -> report what projection decided."""
    result = {
        "qbo_invoice_id": qbo_invoice_id,
        "payment_method": None, "preferred_payment_type": None, "target_payment_method_id": None,
        "qbo_class": None, "memo": None, "statement_memo": None, "memo_locked": False,
        "enrichment_ok": None, "credits_applied": [],
    }

    invoice = load_invoice(conn, qbo_invoice_id)
    if not invoice:
        return {"status": "error", "qbo_invoice_id": qbo_invoice_id, "error": "not found"}
    # delivered invoices are terminal for enrichment — never rewrite a memo
    # the customer already received
    if invoice.get("derived_status") in ("processed", "open_ar"):
        return {"status": "skipped", "qbo_invoice_id": qbo_invoice_id,
                "reason": f"terminal for enrichment ({invoice.get('derived_status')})"}
    if not force and invoice.get("billing_status") == "processing":
        return {"status": "skipped", "qbo_invoice_id": qbo_invoice_id,
                "reason": "already processing"}

    wo = load_linked_wo(conn, qbo_invoice_id)
    if not wo:
        mark_enrichment_failed(conn, qbo_invoice_id)
        return {"status": "error", "qbo_invoice_id": qbo_invoice_id, "error": "no_linked_wo"}
    wo_number = wo["wo_number"]
    qbo_customer_id = invoice.get("qbo_customer_id")

    try:
        # The cache row IS the fresh read — refresh_invoice upserted it from
        # QBO moments ago (webhook -> inbox -> drain), and update_invoice_sparse
        # does its own SyncToken-CAS fetch at write time. Only fall back to a
        # live fetch when raw is missing (pre-inbox rows).
        qbo_inv = invoice.get("raw")
        if not qbo_inv:
            set_stage(conn, qbo_invoice_id, STAGE_FETCHING)
            qbo_inv, _err = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
        if not qbo_inv:
            mark_enrichment_failed(conn, qbo_invoice_id)
            return {"status": "needs_review", "qbo_invoice_id": qbo_invoice_id,
                    "reason": "qbo_fetch_failed"}

        # Credits: matching is THIS workflow's policy (WO-number / amount
        # heuristics, oldest first); applying + echoing is the shared service.
        set_stage(conn, qbo_invoice_id, STAGE_CREDITS)
        # Money decision reads the CACHE — the stream (webhooks -> inbox ->
        # drain) + CDC sweep keep customer_payments current; re-polling QBO
        # per invoice would pay twice for what the sync already maintains
        # (Carter 2026-07-22). Trust is evidence-based, not assumed: the
        # DB-only freshness check below. Only when the evidence is red (inbox
        # backlog on Payment/CreditMemo, or the sweep hasn't run) do we fall
        # back to the targeted QBO read-through, reusing our token.
        fresh, checked_at = credits_cache_fresh(conn)
        credits_verified_at = checked_at if fresh else None
        if not fresh:
            try:
                refresh_customer_credits(qbo_customer_id,
                                         access_token=access_token, realm_id=realm_id)
                cur = conn.cursor()
                cur.execute("SELECT now()"); credits_verified_at = cur.fetchone()[0]; cur.close()
            except Exception as e:
                # decision proceeds on the cache; provenance stays NULL =
                # "decided without confirmed freshness" (visible in
                # v_service_billing_state)
                print(f"  (credit read-through warning: {e})")

        open_credits = sorted(
            load_applicable_credits(conn, qbo_customer_id),
            key=lambda c: (c.get("txn_date") is None, c.get("txn_date")))
        matches = match_credits_to_wo(open_credits, wo, qbo_inv)
        reason_by_id = {c["qbo_payment_id"]: reason for c, _amt, reason in matches}

        # Decision record: the gate row + one 'candidate' row per credit SEEN
        # (matched or not) — the frozen snapshot of what this decision saw.
        upsert_pre_process_row(conn, qbo_invoice_id, credits_verified_at)
        record_credit_decisions(conn, qbo_invoice_id, open_credits, reason_by_id)

        # Auto-apply ONLY deterministic (WO-number) matches; amount heuristics
        # stay 'candidate' for human review. apply_credits fresh-reads the
        # balance, applies in QBO, echoes customer_payments + links from the
        # response; we then flip each applied decision row.
        auto = [(c, amt, r) for c, amt, r in matches if r in DETERMINISTIC_REASONS]
        if auto:
            ar = apply_credits(conn, qbo_customer_id, qbo_invoice_id,
                               access_token, realm_id,
                               credits=[c for c, _amt, _r in auto],
                               applied_via="pre_process")
            for e in ar["applied"]:
                mark_decision_applied(conn, qbo_invoice_id,
                                      e["qbo_payment_id"], e["amount"])
            # transition dual-write: keep the legacy jsonb until the projection
            # reads invoice_credit_decisions (then drop the column)
            result["credits_applied"] = (
                [{"credit_id": e["qbo_payment_id"], "amount": e["amount"],
                  "reason": reason_by_id.get(e["qbo_payment_id"]), "success": True}
                 for e in ar["applied"]] +
                [{"credit_id": f["qbo_payment_id"], "amount": f["amount"],
                  "reason": reason_by_id.get(f["qbo_payment_id"]), "success": False,
                  "error": f.get("error")}
                 for f in ar["failed"]])

        # Payment route. The "*bill*" override fires from ANY office text
        # field (description / tech instructions / corrective) — see CHESSER
        # WO 5007168 + OLSON WO 5000640, auto-charged 2026-05-21.
        set_stage(conn, qbo_invoice_id, STAGE_PAYMENT_METHOD)
        wo_text_blob = " ".join(filter(None, [
            wo.get("work_description"), wo.get("technician_instructions"),
            wo.get("corrective_action")]))
        pm = resolve_payment_for_invoice(conn, qbo_customer_id, wo_text_blob)
        result["preferred_payment_type"] = pm["preferred"]
        result["payment_method"] = pm["legacy_payment_method"]
        result["target_payment_method_id"] = pm["target_pm_id"]

        set_stage(conn, qbo_invoice_id, STAGE_CLASS)
        result["qbo_class"] = derive_qbo_class(
            wo.get("assigned_to"), wo.get("type"), wo.get("work_description"))

        # Memo: locked memos are preserved; deterministic rule first, LLM
        # after; O'Brien three-pool guard demotes untagged memos to review.
        set_stage(conn, qbo_invoice_id, STAGE_MEMO)
        enrichment_ok = True
        composed = None
        if is_memo_locked(invoice):
            composed = invoice.get("memo")
            result["memo"] = composed
            result["statement_memo"] = invoice.get("statement_memo") or composed
            result["memo_locked"] = True
            print(f"  memo locked - preserving '{composed}'")
        else:
            memo_result = deterministic_memo(wo, invoice)
            memo_source = "deterministic"
            if memo_result is None:
                memo_result = generate_memo(wo, invoice, api_key)
                memo_source = "llm"
            if memo_result.get("memo") and "error" not in memo_result:
                customer_for_check = invoice.get("customer_name") or wo.get("customer") or ""
                if (_is_obrien_customer(customer_for_check)
                        and not _has_obrien_pool_tag(memo_result["memo"])):
                    memo_result = {**memo_result,
                                   "confidence": min(memo_result.get("confidence", 0), 0.4),
                                   "reasoning": "O'Brien customer but memo lacks pool tag - "
                                                f"flagged for human review. Original: {memo_result.get('reasoning') or ''}"}
            memo_text, memo_locked_new = None, False
            if "error" in memo_result:
                enrichment_ok = False
                print(f"  memo failed: {memo_result['error'][:120]}")
            elif memo_result.get("confidence", 0) < MEMO_CONFIDENCE_THRESHOLD:
                enrichment_ok = False
                memo_text = memo_result.get("memo")
                print(f"  memo low confidence: {memo_result.get('confidence', 0):.0%}")
            else:
                memo_text = memo_result.get("memo")
                memo_locked_new = True
            composed = f"WO#{wo_number}: {memo_text}" if memo_text else None
            result["memo"] = composed
            result["statement_memo"] = composed
            result["memo_locked"] = memo_locked_new
            print(f"  memo via {memo_source}: {composed} (locked={memo_locked_new})")

        # PATCH QBO: memo (both fields), class, TxnDate aligned to the actual
        # work-completion date (office often creates the invoice days later).
        if enrichment_ok and composed:
            set_stage(conn, qbo_invoice_id, STAGE_WRITING)
            class_id = fetch_qbo_classes(access_token, realm_id).get(result["qbo_class"].lower())
            updates = {"PrivateNote": composed, "CustomerMemo": {"value": composed}}
            if class_id:
                updates["ClassRef"] = {"value": class_id, "name": result["qbo_class"]}
            wo_completed = wo.get("completed")
            if wo_completed is not None:
                completed_iso = (wo_completed.isoformat()
                                 if hasattr(wo_completed, "isoformat")
                                 else str(wo_completed))[:10]
                if qbo_inv.get("TxnDate") != completed_iso:
                    updates["TxnDate"] = completed_iso
            uw = update_invoice_sparse(qbo_invoice_id, updates, access_token, realm_id)
            if not uw["success"]:
                enrichment_ok = False
                print(f"  qbo write failed: {(uw.get('error') or '')[:120]}")
            else:
                qbo_inv = uw.get("invoice") or qbo_inv

        result["enrichment_ok"] = enrichment_ok
        echo_invoice(conn, qbo_invoice_id, qbo_inv)   # fires subtotal_ok recompute
        write_result(conn, qbo_invoice_id, result)    # fires PM recompute + projection

        final_status, final_reason = read_projected_status(conn, qbo_invoice_id)
        return {
            "status": final_status or "unknown",
            "qbo_invoice_id": qbo_invoice_id,
            "wo_number": wo_number,
            "enrichment_ok": enrichment_ok,
            "payment_method": result["payment_method"],
            "preferred_payment_type": result["preferred_payment_type"],
            "target_payment_method_id": result["target_payment_method_id"],
            "qbo_class": result["qbo_class"],
            "memo": composed,
            "credits_applied_count": len([c for c in result["credits_applied"] if c["success"]]),
            "needs_review_reason": final_reason,
        }
    except Exception as e:
        try:
            mark_enrichment_failed(conn, qbo_invoice_id)
        except Exception:
            pass
        return {"status": "error", "qbo_invoice_id": qbo_invoice_id, "error": str(e)[:500]}


def main(qbo_invoice_id: str = None, force: bool = False,
         bulk_all: bool = False, limit: int = None, sleep_ms: int = 1500,
         include_needs_review: bool = True,
         include_ready_to_process: bool = False):
    if not qbo_invoice_id and not bulk_all:
        return {"status": "error", "error": "pass qbo_invoice_id or bulk_all=True"}

    print(f"=== pre_process_invoice (bulk={bulk_all}, limit={limit}, force={force}, sleep={sleep_ms}ms, model={MODEL}) ===")
    conn = get_db_conn()
    set_rate_limiter(conn)  # ADR 008 §4: every QBO call claims
    try:
        access_token, realm_id = refresh_qbo_token()
        api_key = wmill.get_variable(OPENAI_KEY_VAR)

        if not bulk_all:
            return process_one(conn, qbo_invoice_id, access_token, realm_id, api_key, force)

        cur = conn.cursor()
        statuses = ["'awaiting_pre_processing'"]
        if include_needs_review:
            statuses.append("'needs_review'")
        if include_ready_to_process:
            statuses.append("'ready_to_process'")
        cur.execute(f"SELECT qbo_invoice_id FROM billing.invoices "
                    f"WHERE billing_status IN ({', '.join(statuses)}) "
                    f"ORDER BY txn_date DESC NULLS LAST"
                    + (f" LIMIT {int(limit)}" if limit else ""))
        targets = [r[0] for r in cur.fetchall()]
        cur.close()
        print(f"Found {len(targets)} invoices to pre-process")

        stats, sample = {}, []
        for i, qid in enumerate(targets):
            res = process_one(conn, qid, access_token, realm_id, api_key, force=True)
            status = res.get("status", "error")
            stats[status] = stats.get(status, 0) + 1
            if i < 15:
                sample.append({"qbo_invoice_id": qid, "status": status,
                               "wo_number": res.get("wo_number"), "memo": res.get("memo"),
                               "needs_review_reason": res.get("needs_review_reason")})
            print(f"  [{i+1}/{len(targets)}] {qid} -> {status}")
            if sleep_ms and i + 1 < len(targets):
                time.sleep(sleep_ms / 1000.0)

        print(f"=== done: {stats} ===")
        return {"status": "success", "total": len(targets), "stats": stats, "sample": sample}
    finally:
        conn.close()
