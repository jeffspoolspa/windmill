# f/service_billing/pre_process_invoice
#
# Phase 2B-slim: pre_process is now deterministic enrichment-only.
#
# Old behavior (pre Phase 2B):
#   - Computed and wrote subtotal_ok, enrichment_ok, billing_status,
#     needs_review_reason directly. Built reason strings inline. Set the
#     billing.skip_recheck flag to suppress fan-out triggers during writes.
#
# New behavior (after Phase 2B):
#   - Pre_process owns ONLY two indicator-adjacent writes: enrichment_ok
#     (success/failure of its own work) and pre_processed_at (run timestamp).
#   - Plus its source-of-truth fields: payment_method, preferred_payment_type,
#     target_payment_method_id, qbo_class, memo, statement_memo, memo_locked,
#     credits_applied. These are values pre_process derives or applies; they
#     are NOT indicators — the source-table maintenance triggers recompute
#     payment_method_ok / credits_ok from them automatically.
#   - billing_status and needs_review_reason are owned by the projection
#     trigger. Pre_process does not write them. Final status is whatever
#     projection decided after pre_process's UPDATE fired the maintenance +
#     projection cascade. We read it back at the end for the return value.
#   - Subtotal check is removed entirely. The dispatch worker gates on
#     subtotal_ok=TRUE before firing pre_process. The subtotal_ok column
#     is owned by triggers on work_orders.sub_total / invoices.subtotal
#     changes — pre_process never writes it.
#   - Credits are still applied (auto-match against open credits). Each
#     successful apply decrements customer_payments.unapplied_amt, which
#     fires fn_set_credits_ok_from_payment → recomputes credits_ok →
#     projection. Pre_process doesn't append a "credit_review" reason
#     itself; projection composes it from credits_ok=false.
#   - skip_recheck flag removed. The new triggers are deterministic and
#     idempotent; running them during pre_process is correct, not wasteful
#     enough to justify the flag complexity.
#
# Failure paths write enrichment_ok=false + pre_processed_at=now(). The
# projection trigger then sets billing_status=needs_review with reason
# "enrichment_failed". Phase 2C will preserve detail (current memo prompt
# error, low-confidence percentage, etc.) — for now the failure detail is
# lost when status is composed by projection. Tradeoff accepted: simpler
# Phase 2B, richer failure detail comes in 2C via processing_attempts
# stage='pre_process' rows or a new enrichment_error column.

import calendar
import json
import random
import time
from datetime import date as _date

import psycopg2
import psycopg2.extras
import requests
import wmill

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"
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


def refresh_qbo_token():
    resource = wmill.get_resource(QBO_RESOURCE)
    resp = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"]), timeout=30,
    )
    if not resp.ok:
        raise Exception(f"QBO token refresh failed: {resp.status_code} - {resp.text}")
    tokens = resp.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(QBO_RESOURCE, resource)
    return tokens["access_token"], resource["realm_id"]


def get_db_conn():
    sb = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(
        host=sb["host"], port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"), user=sb["user"],
        password=sb["password"], sslmode=sb.get("sslmode", "require"),
    )


def set_stage(conn, qbo_invoice_id, stage):
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE billing.invoices SET pre_process_stage = %s WHERE qbo_invoice_id = %s",
            (stage, qbo_invoice_id),
        )
        conn.commit(); cur.close()
    except Exception as e:
        print(f"  (set_stage warning: {e})")


def _qbo_request(method, path, access_token, realm_id, params=None, body=None,
                 max_attempts=5):
    url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/{path}"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    if method.upper() == "POST":
        headers["Content-Type"] = "application/json"

    last_exc = None
    for attempt in range(max_attempts):
        try:
            resp = requests.request(
                method, url, headers=headers, params=params, json=body, timeout=30,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            time.sleep(min(0.5 * (2 ** attempt), 8))
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt + 1 >= max_attempts:
                return resp
            ra = resp.headers.get("Retry-After")
            if ra and ra.isdigit():
                delay = min(int(ra), 10)
            else:
                delay = min(0.5 * (2 ** attempt), 8)
            time.sleep(delay)
            continue

        return resp

    class _FakeResp:
        ok = False
        status_code = 0
        text = f"network error after {max_attempts} attempts: {last_exc}"
        headers = {}
        def json(self): return {}
    return _FakeResp()


def qbo_get(path, access_token, realm_id, params=None):
    return _qbo_request("GET", path, access_token, realm_id, params=params)


def qbo_post(path, access_token, realm_id, body):
    return _qbo_request("POST", path, access_token, realm_id, body=body)


def qbo_invoice_subtotal(inv):
    for line in inv.get("Line", []) or []:
        if line.get("DetailType") == "SubTotalLineDetail":
            try:
                return round(float(line.get("Amount", 0) or 0), 2)
            except (TypeError, ValueError):
                pass
    total = float(inv.get("TotalAmt", 0) or 0)
    tax = float((inv.get("TxnTaxDetail") or {}).get("TotalTax", 0) or 0)
    return round(total - tax, 2)


def fetch_qbo_classes(access_token, realm_id):
    resp = qbo_get("query", access_token, realm_id,
                   params={"query": "SELECT * FROM Class WHERE Active = true MAXRESULTS 1000"})
    if not resp.ok:
        return {}
    classes = resp.json().get("QueryResponse", {}).get("Class", [])
    return {c["Name"].lower(): c["Id"] for c in classes}


def fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id):
    resp = qbo_get(f"invoice/{qbo_invoice_id}", access_token, realm_id)
    if not resp.ok:
        return None
    return resp.json().get("Invoice")


def update_qbo_invoice_with_retry(qbo_invoice_id, updates, access_token, realm_id, max_retries=2):
    last_err = None
    for attempt in range(max_retries + 1):
        inv = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
        if not inv:
            if attempt < max_retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            return {"success": False, "error": f"fetch failed after {attempt+1} attempts"}
        body = {"Id": inv["Id"], "SyncToken": inv["SyncToken"], "sparse": True, **updates}
        resp = qbo_post("invoice", access_token, realm_id, body)
        if resp.ok:
            return {"success": True, "invoice": resp.json().get("Invoice")}
        text = resp.text[:400]
        last_err = f"HTTP {resp.status_code}: {text}"
        if "Stale Object" in text and attempt < max_retries:
            time.sleep(0.5 * (attempt + 1))
            continue
        break
    return {"success": False, "error": last_err}


def apply_credit(credit_id, credit_type, invoice_id, customer_ref, amount, access_token, realm_id):
    try:
        if credit_type == "credit_memo":
            cm_id = credit_id.replace("CM-", "") if credit_id.startswith("CM-") else credit_id
            resp = qbo_post("payment", access_token, realm_id, {
                "CustomerRef": customer_ref, "TotalAmt": 0,
                "Line": [{"Amount": amount,
                          "LinkedTxn": [{"TxnId": cm_id, "TxnType": "CreditMemo"},
                                        {"TxnId": invoice_id, "TxnType": "Invoice"}]}],
            })
            return {"success": True} if resp.ok else {"success": False, "error": f"CM apply: {resp.text[:200]}"}
        pmt_resp = qbo_get(f"payment/{credit_id}", access_token, realm_id)
        if not pmt_resp.ok:
            return {"success": False, "error": f"fetch payment: {pmt_resp.status_code}"}
        payment = pmt_resp.json().get("Payment", {})
        payment.setdefault("Line", []).append({
            "Amount": amount,
            "LinkedTxn": [{"TxnId": invoice_id, "TxnType": "Invoice"}],
        })
        payment["sparse"] = True
        resp = qbo_post("payment", access_token, realm_id, payment)
        return {"success": True} if resp.ok else {"success": False, "error": f"payment apply: {resp.text[:200]}"}
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}


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


def load_invoice(conn, qbo_invoice_id):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM billing.invoices WHERE qbo_invoice_id = %s", (qbo_invoice_id,))
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None


def is_memo_locked(invoice):
    return bool(invoice.get("memo_locked")) and bool(invoice.get("memo"))


def load_linked_wo(conn, qbo_invoice_id):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM public.work_orders WHERE qbo_invoice_id = %s LIMIT 1", (qbo_invoice_id,))
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None


def load_open_credits(conn, qbo_customer_id):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT * FROM billing.customer_payments
        WHERE qbo_customer_id = %s
          AND unapplied_amt > 0
          AND (memo IS NULL OR memo !~* 'maint')
          AND (txn_date IS NULL OR txn_date >= (now() - interval '6 months')::date)
        ORDER BY txn_date ASC
    """, (qbo_customer_id,))
    rows = [dict(r) for r in cur.fetchall()]; cur.close()
    return rows


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


def refresh_invoice_cache(conn, qbo_invoice_id, qbo_invoice):
    """Write QBO body fields back to the cache. The UPDATE on
    billing.invoices.subtotal fires the maintenance trigger that recomputes
    subtotal_ok and (via projection) billing_status."""
    subtotal = qbo_invoice_subtotal(qbo_invoice)
    balance = float(qbo_invoice.get("Balance", 0) or 0)
    total_amt = float(qbo_invoice.get("TotalAmt", 0) or 0)
    email_status = qbo_invoice.get("EmailStatus")
    cur = conn.cursor()
    cur.execute("""
        UPDATE billing.invoices
        SET subtotal = %s, balance = %s, total_amt = %s,
            email_status = %s, raw = %s::jsonb, fetched_at = now()
        WHERE qbo_invoice_id = %s
    """, (subtotal, balance, total_amt, email_status, json.dumps(qbo_invoice), qbo_invoice_id))
    conn.commit(); cur.close()


def mark_enrichment_failed(conn, qbo_invoice_id):
    """Failure path. Writes enrichment_ok=false + pre_processed_at=now().
    The projection trigger then sets billing_status=needs_review with reason
    'enrichment_failed'.

    Phase 2C will preserve detail (the specific error: low-confidence %,
    API 5xx, QBO write failure, etc.) by either (a) logging a row to
    processing_attempts stage='pre_process' or (b) writing to a new
    enrichment_error column. For Phase 2B the failure detail is lost when
    projection composes the reason.
    """
    cur = conn.cursor()
    cur.execute("""
        UPDATE billing.invoices
        SET enrichment_ok    = false,
            pre_processed_at = now(),
            pre_process_stage = %s
        WHERE qbo_invoice_id = %s
    """, (STAGE_DONE, qbo_invoice_id))
    conn.commit(); cur.close()


def write_result(conn, qbo_invoice_id, result):
    """Write enrichment_ok + source-of-truth fields. The single UPDATE fires
    the maintenance triggers (payment_method_ok recompute, etc.) and the
    projection trigger which writes billing_status + needs_review_reason.

    Pre_process does NOT write billing_status, needs_review_reason, or
    subtotal_ok — those are owned by triggers post Phase 2B.
    """
    cur = conn.cursor()
    cur.execute("""
        UPDATE billing.invoices
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
    """, (result.get("payment_method"),
          result.get("preferred_payment_type"),
          result.get("target_payment_method_id"),
          result.get("qbo_class"),
          result.get("memo"),
          result.get("statement_memo"),
          bool(result.get("memo_locked")),
          result.get("enrichment_ok"),
          json.dumps(result.get("credits_applied") or []),
          STAGE_DONE,
          qbo_invoice_id))
    conn.commit(); cur.close()


def read_projected_status(conn, qbo_invoice_id):
    """After write_result, read back what the projection trigger decided.
    Returns (billing_status, needs_review_reason)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT billing_status, needs_review_reason FROM billing.invoices WHERE qbo_invoice_id = %s",
        (qbo_invoice_id,),
    )
    row = cur.fetchone(); cur.close()
    return (row[0], row[1]) if row else (None, None)


def process_one(conn, qbo_invoice_id, access_token, realm_id, api_key, force=False):
    result = {
        "qbo_invoice_id": qbo_invoice_id,
        "payment_method": None, "preferred_payment_type": None, "target_payment_method_id": None,
        "qbo_class": None, "memo": None, "statement_memo": None, "memo_locked": False,
        "enrichment_ok": None, "credits_applied": [],
    }

    invoice = load_invoice(conn, qbo_invoice_id)
    if not invoice:
        return {"status": "error", "qbo_invoice_id": qbo_invoice_id, "error": "not found"}
    if invoice.get("billing_status") == "processed":
        return {"status": "skipped", "qbo_invoice_id": qbo_invoice_id,
                "reason": "already processed (terminal)"}
    if not force and invoice.get("billing_status") == "processing":
        return {"status": "skipped", "qbo_invoice_id": qbo_invoice_id,
                "reason": "already processing"}

    wo = load_linked_wo(conn, qbo_invoice_id)
    if not wo:
        # Dispatcher's WO-link filter should prevent this; if we somehow get
        # here, mark enrichment failed so we don't retry endlessly.
        mark_enrichment_failed(conn, qbo_invoice_id)
        return {"status": "error", "qbo_invoice_id": qbo_invoice_id, "error": "no_linked_wo"}

    wo_number = wo["wo_number"]
    qbo_customer_id = invoice.get("qbo_customer_id")

    try:
        set_stage(conn, qbo_invoice_id, STAGE_FETCHING)
        qbo_inv = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
        if not qbo_inv:
            mark_enrichment_failed(conn, qbo_invoice_id)
            return {"status": "needs_review", "qbo_invoice_id": qbo_invoice_id,
                    "reason": "qbo_fetch_failed"}

        # NO subtotal check here — dispatch worker gates on subtotal_ok=TRUE
        # before firing pre_process. The maintenance trigger on
        # invoices.subtotal/work_orders.sub_total maintains subtotal_ok.

        # Apply credits — each successful apply decrements
        # customer_payments.unapplied_amt, which fires
        # fn_set_credits_ok_from_payment → recompute credits_ok →
        # projection. Pre_process does NOT append a credit_review reason
        # itself; projection composes it from credits_ok=false.
        set_stage(conn, qbo_invoice_id, STAGE_CREDITS)
        open_credits = load_open_credits(conn, qbo_customer_id)
        matches = match_credits_to_wo(open_credits, wo, qbo_inv)
        remaining = float(qbo_inv.get("Balance", 0) or 0)
        for credit, amt, reason in matches:
            amt = min(amt, remaining)
            if amt <= 0:
                break
            ar = apply_credit(credit["qbo_payment_id"], credit["type"], qbo_inv["Id"],
                              qbo_inv.get("CustomerRef"), amt, access_token, realm_id)
            result["credits_applied"].append({
                "credit_id": credit["qbo_payment_id"], "amount": amt,
                "reason": reason, "success": ar["success"],
                "error": ar.get("error"),
            })
            if ar["success"]:
                remaining -= amt
                cur = conn.cursor()
                cur.execute(
                    "UPDATE billing.customer_payments SET unapplied_amt = GREATEST(unapplied_amt - %s, 0) "
                    "WHERE qbo_payment_id = %s",
                    (amt, credit["qbo_payment_id"]),
                )
                cur.execute(
                    """INSERT INTO billing.payment_invoice_links
                         (payment_id, invoice_id, amount, applied_via)
                       VALUES (%s, %s, %s, 'auto_match')
                       ON CONFLICT (payment_id, invoice_id) DO UPDATE SET
                         amount = billing.payment_invoice_links.amount + EXCLUDED.amount""",
                    (credit["qbo_payment_id"], qbo_invoice_id, amt),
                )
                conn.commit(); cur.close()

        # Resolve payment method (fires fn_set_payment_method_ok_from_invoice
        # via the per-source trigger when these columns change in write_result)
        set_stage(conn, qbo_invoice_id, STAGE_PAYMENT_METHOD)
        pm_resolution = resolve_payment_for_invoice(
            conn, qbo_customer_id, wo.get("work_description"),
        )
        result["preferred_payment_type"]   = pm_resolution["preferred"]
        result["payment_method"]           = pm_resolution["legacy_payment_method"]
        result["target_payment_method_id"] = pm_resolution["target_pm_id"]

        # Derive QBO class
        set_stage(conn, qbo_invoice_id, STAGE_CLASS)
        result["qbo_class"] = derive_qbo_class(
            wo.get("assigned_to"), wo.get("type"), wo.get("work_description"),
        )

        # Memo generation
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
                customer_for_check = (
                    invoice.get("customer_name") or wo.get("customer") or ""
                )
                if (_is_obrien_customer(customer_for_check)
                        and not _has_obrien_pool_tag(memo_result["memo"])):
                    orig_reason = memo_result.get("reasoning") or ""
                    memo_result = {
                        **memo_result,
                        "confidence": min(memo_result.get("confidence", 0), 0.4),
                        "reasoning": (
                            f"O'Brien customer but memo lacks pool tag - "
                            f"flagged for human review. Original: {orig_reason}"
                        ),
                    }

            memo_text = None
            memo_locked_new = False
            if "error" in memo_result:
                enrichment_ok = False
                print(f"  memo failed: {memo_result['error'][:120]}")
            elif memo_result.get("confidence", 0) < MEMO_CONFIDENCE_THRESHOLD:
                enrichment_ok = False
                print(f"  memo low confidence: {memo_result.get('confidence', 0):.0%}")
                memo_text = memo_result.get("memo")
            else:
                memo_text = memo_result.get("memo")
                memo_locked_new = True

            composed = f"WO#{wo_number}: {memo_text}" if memo_text else None
            result["memo"] = composed
            result["statement_memo"] = composed
            result["memo_locked"] = memo_locked_new
            print(f"  memo via {memo_source}: {composed} (locked={memo_locked_new})")

        # Write to QBO if enrichment OK
        if enrichment_ok and composed:
            set_stage(conn, qbo_invoice_id, STAGE_WRITING)
            classes = fetch_qbo_classes(access_token, realm_id)
            class_id = classes.get(result["qbo_class"].lower())
            updates = {"PrivateNote": composed, "CustomerMemo": {"value": composed}}
            if class_id:
                updates["ClassRef"] = {"value": class_id, "name": result["qbo_class"]}
            uw = update_qbo_invoice_with_retry(qbo_invoice_id, updates, access_token, realm_id)
            if not uw["success"]:
                enrichment_ok = False
                print(f"  qbo write failed: {(uw.get('error') or '')[:120]}")
            else:
                qbo_inv = uw.get("invoice") or fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id) or qbo_inv

        result["enrichment_ok"] = enrichment_ok

        # Refresh local cache from QBO (may fire subtotal_ok recompute trigger)
        refresh_invoice_cache(conn, qbo_invoice_id, qbo_inv)

        # The single UPDATE here fires payment_method_ok recompute (because
        # payment_method/target/preferred changed) AND projection (because
        # enrichment_ok + pre_processed_at changed). Final billing_status
        # is set by projection.
        write_result(conn, qbo_invoice_id, result)

        final_status, final_reason = read_projected_status(conn, qbo_invoice_id)

        return {
            "status":                   final_status or "unknown",
            "qbo_invoice_id":           qbo_invoice_id,
            "wo_number":                wo_number,
            "enrichment_ok":            enrichment_ok,
            "payment_method":           result["payment_method"],
            "preferred_payment_type":   result["preferred_payment_type"],
            "target_payment_method_id": result["target_payment_method_id"],
            "qbo_class":                result["qbo_class"],
            "memo":                     composed,
            "credits_applied_count":    len([c for c in result["credits_applied"] if c["success"]]),
            "needs_review_reason":      final_reason,
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
        sql = (f"SELECT qbo_invoice_id FROM billing.invoices "
               f"WHERE billing_status IN ({', '.join(statuses)}) "
               f"ORDER BY txn_date DESC NULLS LAST")
        if limit:
            sql += f" LIMIT {int(limit)}"
        cur.execute(sql)
        targets = [r[0] for r in cur.fetchall()]
        cur.close()
        print(f"Found {len(targets)} invoices to pre-process")

        stats = {"ready_to_process": 0, "needs_review": 0, "error": 0, "skipped": 0}
        sample = []
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
