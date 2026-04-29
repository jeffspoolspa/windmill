# Pre-processing for invoices (single or bulk).
# Persists credits_applied so the UI can show the credit check result.
# Emits pre_process_stage updates to billing.invoices at each step so the
# UI progress modal can subscribe via Supabase Realtime and animate.

import requests
import wmill
import psycopg2
import psycopg2.extras
import json
import time

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"
ANTHROPIC_KEY_VAR = "f/service_billing/ANTHROPIC_API_KEY"

MEMO_CONFIDENCE_THRESHOLD = 0.85
SUBTOTAL_TOLERANCE = 0.02
MODEL = "claude-sonnet-4-20250514"

# Stage values written to billing.invoices.pre_process_stage. These drive the
# progress modal UI; adding a new one doesn't require a DDL change (column is
# text without a check constraint by design).
STAGE_FETCHING = "fetching_qbo"
STAGE_SUBTOTAL = "checking_subtotal"
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
    """Persist the current stage so the UI progress modal can animate.
    Autocommits a small UPDATE so Realtime fires immediately — do NOT bundle
    this into a larger transaction or subscribers won't see the progression."""
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE billing.invoices SET pre_process_stage = %s WHERE qbo_invoice_id = %s",
            (stage, qbo_invoice_id),
        )
        conn.commit(); cur.close()
    except Exception as e:
        # Never fail the pipeline just because the stage write failed.
        print(f"  (set_stage warning: {e})")


def _qbo_request(method, path, access_token, realm_id, params=None, body=None,
                 max_attempts=5):
    """QBO HTTP call with 429/5xx/network retry + exponential backoff.

    Rationale: running multiple pre_process jobs in parallel (concurrent_limit=10)
    can burst 30-40 simultaneous calls into QBO, which trips their per-realm
    throttle. QBO returns 429 with a Retry-After header. We honor that header
    (clamped to 10s max) and otherwise use 0.5s, 1s, 2s, 4s backoff.

    Retries: 429, 500, 502, 503, 504, and requests.Timeout / ConnectionError.
    Passes through 4xx other than 429 (those are real errors, no point retrying).
    """
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
            # Network-level retry
            time.sleep(min(0.5 * (2 ** attempt), 8))
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt + 1 >= max_attempts:
                return resp  # out of retries, let caller surface the error
            ra = resp.headers.get("Retry-After")
            if ra and ra.isdigit():
                delay = min(int(ra), 10)
            else:
                delay = min(0.5 * (2 ** attempt), 8)
            time.sleep(delay)
            continue

        return resp

    # All attempts exhausted on network errors
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
    """Fetch invoice, apply sparse update, handle Stale Object collisions.

    429/5xx retries happen INSIDE qbo_get/qbo_post now, so by the time we
    reach this function an unrecoverable fetch miss means a real 404 or
    persistent server error. Still do one retry in case of transient races.
    """
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


def resolve_payment_method(wo_description, pms):
    desc = (wo_description or "").lower()
    if "*bill*" in desc:
        return "invoice"
    if any(pm.get("is_active") for pm in pms):
        return "on_file"
    return "invoice"


MEMO_PROMPT = """You write a short customer-friendly memo for a pool service invoice.

Given a work order's details, return a JSON object:
- memo: the memo text (NO WO number prefix — just the service description)
- confidence: 0.0 to 1.0 — how confident you are you understand what was done
- reasoning: 1 sentence

Style rules:
- Title Case, 2-7 words
- Equipment + Action format: "Autofill Valve Replacement", "Pool Pump Diagnosis"
- Use "&" to join two related items: "Salt Cell Cleaning & Filter Replacement"
- Use " — " for a qualifier: "Water Chemistry Service — Shock Treatment"
- Add context when meaningful: "Pre-Purchase Pool Inspection", "VSP Pump Error Diagnosis"
- Action words: Diagnosis, Replacement, Repair, Install, Delivery, Cleaning, Removal, Check, Clearing, Service
- No trailing punctuation
- Lean on corrective_action over work_description

If you cannot figure out what was done, return confidence below 0.6.

Return ONLY valid JSON."""

MEMO_EXAMPLES = [
    {"input": {"type": "POOL INSPECTION", "description": "Pool inspection", "corrective": "Pool Inspection"},
     "output": {"memo": "Pool Inspection", "confidence": 0.97, "reasoning": "Straight pool inspection."}},
    {"input": {"type": "GENERAL SERVICE", "description": "Valve was clogged with leaves and a wiffle ball.", "corrective": "Unclogged valve with leaves and wiffle ball."},
     "output": {"memo": "Clogged Valve Clearing", "confidence": 0.96, "reasoning": "Valve was clogged and cleared."}},
    {"input": {"type": "DIAGNOSIS", "description": "Electric heater making buzzing noise, then clicks off every ~3 min.", "corrective": "Found bad capacitor. Replaced. Unit started right up."},
     "output": {"memo": "Electric Heater Diagnosis", "confidence": 0.95, "reasoning": "Electric heater diagnosed and repaired."}},
    {"input": {"type": "GENERAL SERVICE", "description": "Remove Pool Cover", "corrective": "Removed cover."},
     "output": {"memo": "Pool Cover Removal", "confidence": 0.98, "reasoning": "Pool cover removed."}},
    {"input": {"type": "MAINTENANCE", "description": "Clean salt cell and replace filter.", "corrective": "Cleaned salt cell. Installed the filter no problem."},
     "output": {"memo": "Salt Cell Cleaning & Filter Replacement", "confidence": 0.92, "reasoning": "Both services done."}},
    {"input": {"type": "DELIVERY", "description": "Deliver a 50lb bucket of chlorine tabs", "corrective": "Delivered"},
     "output": {"memo": "Chlorine Tab Delivery", "confidence": 0.98, "reasoning": "Standard chemical delivery."}},
    {"input": {"type": "GENERAL SERVICE", "description": "Hot tub showing Gas Off — Check Auxiliary error.", "corrective": "Diagnose. Heater had no flow due to debris."},
     "output": {"memo": "Hot Tub Diagnosis — Gas Error", "confidence": 0.94, "reasoning": "Hot tub diagnosed for gas error."}},
    {"input": {"type": "GENERAL SERVICE", "description": "Spa Pump running loud. Motor + seal plate needed.", "corrective": "Installed new plate and motor."},
     "output": {"memo": "Spa Pump Motor & Seal Plate Replacement", "confidence": 0.96, "reasoning": "Spa pump motor + seal plate replacement."}},
    {"input": {"type": "POOL INSPECTION", "description": "Pool Inspection. Due diligence 3/25 or 3/26. Potential buyer access.", "corrective": "."},
     "output": {"memo": "Pre-Purchase Pool Inspection", "confidence": 0.93, "reasoning": "Pool inspection for potential buyer."}},
]


def generate_memo(wo, api_key, max_retries=3):
    user_msg = json.dumps({
        "type": wo.get("type"),
        "description": wo.get("work_description") or "",
        "corrective": wo.get("corrective_action") or "",
    })
    examples_text = "\n\nExamples:"
    for ex in MEMO_EXAMPLES:
        examples_text += f"\nInput: {json.dumps(ex['input'])}\nOutput: {json.dumps(ex['output'])}\n"
    body = {"model": MODEL, "max_tokens": 256,
            "system": [{
                "type": "text",
                "text": MEMO_PROMPT + examples_text,
                "cache_control": {"type": "ephemeral"}
            }],
            "messages": [{"role": "user", "content": user_msg}]}
    last_err = None
    for attempt in range(max_retries + 1):
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=body, timeout=30,
        )
        if resp.ok:
            text = resp.json()["content"][0]["text"].strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"error": f"Failed to parse: {text[:200]}"}
        last_err = f"Claude API {resp.status_code}: {resp.text[:200]}"
        if resp.status_code == 429 and attempt < max_retries:
            retry_after = resp.headers.get("retry-after")
            delay = min(int(retry_after), 30) if (retry_after and retry_after.isdigit()) else min(2 ** attempt, 30)
            time.sleep(delay)
            continue
        break
    return {"error": last_err}


def load_invoice(conn, qbo_invoice_id):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM billing.invoices WHERE qbo_invoice_id = %s", (qbo_invoice_id,))
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None


def is_memo_locked(invoice):
    """User has either explicitly edited (ClassificationEditor) or approved
    (mark_invoice_ready / triage approve) this invoice's memo. Pre-processing
    should preserve it on re-runs instead of overwriting with fresh Claude
    output. Other stages (subtotal check, credit matching, class, QBO write)
    still run normally."""
    return bool(invoice.get("memo_locked")) and bool(invoice.get("memo"))


def load_linked_wo(conn, qbo_invoice_id):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM public.work_orders WHERE qbo_invoice_id = %s LIMIT 1", (qbo_invoice_id,))
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None


def load_pms(conn, qbo_customer_id):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM billing.customer_payment_methods WHERE qbo_customer_id = %s AND is_active = true",
                (qbo_customer_id,))
    rows = [dict(r) for r in cur.fetchall()]; cur.close()
    return rows


def load_open_credits(conn, qbo_customer_id):
    """Applicable credits only — excludes maint-scoped and stale (>6mo).
    Same filter process_invoice uses, so both stages agree on what should
    have been auto-applied vs. left alone."""
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
    """Auto-apply rules:
      - WO# in credit.ref_num (most common — QBO PaymentRefNum)
      - WO# in credit.memo (PrivateNote)
      - full_cover: credit exactly equals WO subtotal OR QBO invoice total OR QBO balance
      - half_deposit: credit equals half of WO subtotal OR half of QBO total

    Checking invoice total/balance (not just WO subtotal) catches the case
    where the customer pre-paid the exact invoice amount including tax.
    """
    wo_number = wo.get("wo_number")
    wo_subtotal = float(wo.get("sub_total") or 0)
    qbo_total = float((qbo_inv or {}).get("TotalAmt") or 0)
    qbo_balance = float((qbo_inv or {}).get("Balance") or 0)

    # All the "target amounts" a credit could match to trigger a full or half match.
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


def fail_flag(conn, qbo_invoice_id, billing_status, reason):
    """Narrow UPDATE for early-exit failures (QBO fetch failed, etc).
    Touches ONLY billing_status + needs_review_reason + pre_process_stage —
    preserves memo, class, payment_method, credits_applied, memo_locked.
    Use this when failing BEFORE those fields were computed/loaded into result.
    """
    cur = conn.cursor()
    cur.execute("""
        UPDATE billing.invoices
        SET billing_status = %s,
            needs_review_reason = %s,
            pre_processed_at = now(),
            pre_process_stage = %s
        WHERE qbo_invoice_id = %s
    """, (billing_status, reason, STAGE_DONE, qbo_invoice_id))
    conn.commit(); cur.close()


def write_result(conn, qbo_invoice_id, result):
    # memo_locked is preserved by omission — we don't touch it. User lock
    # only cleared via an explicit "unlock" action (not implemented yet).
    cur = conn.cursor()
    cur.execute("""
        UPDATE billing.invoices
        SET billing_status = %s, needs_review_reason = %s, payment_method = %s,
            qbo_class = %s, memo = %s, statement_memo = %s,
            subtotal_ok = %s, enrichment_ok = %s,
            credits_applied = %s::jsonb,
            pre_processed_at = now(),
            pre_process_stage = %s
        WHERE qbo_invoice_id = %s
    """, (result["billing_status"], result.get("needs_review_reason"), result.get("payment_method"),
          result.get("qbo_class"), result.get("memo"), result.get("statement_memo"),
          result.get("subtotal_ok"), result.get("enrichment_ok"),
          json.dumps(result.get("credits_applied") or []),
          STAGE_DONE,
          qbo_invoice_id))
    conn.commit(); cur.close()


def process_one(conn, qbo_invoice_id, access_token, realm_id, api_key, force=False):
    issues = []
    result = {"qbo_invoice_id": qbo_invoice_id, "billing_status": None, "needs_review_reason": None,
              "payment_method": None, "qbo_class": None, "memo": None, "statement_memo": None,
              "subtotal_ok": None, "enrichment_ok": None, "credits_applied": []}

    invoice = load_invoice(conn, qbo_invoice_id)
    if not invoice:
        return {"status": "error", "qbo_invoice_id": qbo_invoice_id, "error": "not found"}
    # 'processed' is terminal — NEVER downgrade, even with force=True.
    # Historical bug: force=True bypassed this guard, then write_result
    # unconditionally overwrote billing_status back to ready_to_process,
    # reverting a successfully-processed invoice. If you really need to
    # re-run pre-processing on a processed invoice, use Revert to Review
    # first — that explicit flow makes the downgrade intentional.
    if invoice.get("billing_status") == "processed":
        return {"status": "skipped", "qbo_invoice_id": qbo_invoice_id,
                "reason": "already processed (terminal — revert first to re-run)"}
    if not force and invoice.get("billing_status") == "processing":
        return {"status": "skipped", "qbo_invoice_id": qbo_invoice_id,
                "reason": "already processing"}
    wo = load_linked_wo(conn, qbo_invoice_id)
    if not wo:
        # Preserve existing fields — just flag. Early exit path.
        fail_flag(conn, qbo_invoice_id, "needs_review", "no_linked_wo")
        return {"status": "needs_review", "qbo_invoice_id": qbo_invoice_id, "reason": "no_linked_wo"}

    wo_number = wo["wo_number"]
    qbo_customer_id = invoice.get("qbo_customer_id")

    try:
        set_stage(conn, qbo_invoice_id, STAGE_FETCHING)
        qbo_inv = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
        if not qbo_inv:
            # Early failure BEFORE we've loaded/computed the memo, class, etc.
            # Do NOT call write_result — it would null out the existing
            # (possibly user-edited) memo/class fields. Just flag the status.
            fail_flag(conn, qbo_invoice_id, "needs_review", "qbo_fetch_failed")
            return {"status": "needs_review", "qbo_invoice_id": qbo_invoice_id, "reason": "qbo_fetch_failed"}

        set_stage(conn, qbo_invoice_id, STAGE_SUBTOTAL)
        wo_subtotal = float(wo.get("sub_total") or 0)
        qbo_subtotal = qbo_invoice_subtotal(qbo_inv)
        subtotal_ok = True
        if wo_subtotal > 0 and qbo_subtotal > 0 and abs(wo_subtotal - qbo_subtotal) >= SUBTOTAL_TOLERANCE:
            subtotal_ok = False
            issues.append(f"subtotal_mismatch (WO ${wo_subtotal:.2f} vs QBO ${qbo_subtotal:.2f})")
        result["subtotal_ok"] = subtotal_ok

        if subtotal_ok:
            set_stage(conn, qbo_invoice_id, STAGE_CREDITS)
            # Applicable = non-maint, not stale. Everything here is a candidate
            # for auto-apply OR human review — never silently ignored.
            open_credits = load_open_credits(conn, qbo_customer_id)
            matches = match_credits_to_wo(open_credits, wo, qbo_inv)
            matched_ids = {c["qbo_payment_id"] for c, _, _ in matches}
            remaining = float(qbo_inv.get("Balance", 0) or 0)
            for credit, amt, reason in matches:
                amt = min(amt, remaining)
                if amt <= 0:
                    break
                ar = apply_credit(credit["qbo_payment_id"], credit["type"], qbo_inv["Id"],
                                  qbo_inv.get("CustomerRef"), amt, access_token, realm_id)
                result["credits_applied"].append({"credit_id": credit["qbo_payment_id"], "amount": amt,
                                                   "reason": reason, "success": ar["success"],
                                                   "error": ar.get("error")})
                if ar["success"]:
                    remaining -= amt
                    cur = conn.cursor()
                    # Decrement local unapplied balance
                    cur.execute(
                        "UPDATE billing.customer_payments SET unapplied_amt = GREATEST(unapplied_amt - %s, 0) "
                        "WHERE qbo_payment_id = %s",
                        (amt, credit["qbo_payment_id"]),
                    )
                    # Record the application in the link table so it persists
                    # after re-runs (which may clear credits_applied jsonb) and
                    # so UI queries have a stable source of truth.
                    cur.execute(
                        """INSERT INTO billing.payment_invoice_links
                             (payment_id, invoice_id, amount, applied_via)
                           VALUES (%s, %s, %s, 'auto_match')
                           ON CONFLICT (payment_id, invoice_id) DO UPDATE SET
                             amount = billing.payment_invoice_links.amount + EXCLUDED.amount""",
                        (credit["qbo_payment_id"], qbo_invoice_id, amt),
                    )
                    conn.commit(); cur.close()

            # Flag unmatched applicable credits for human review — UNLESS the
            # user already overrode credit_review (credits are for another WO,
            # not applicable, etc). Override persists across re-runs so
            # pre_process doesn't keep re-flagging.
            unmatched = [
                c for c in open_credits
                if c["qbo_payment_id"] not in matched_ids
                and float(c.get("unapplied_amt") or 0) > 0
            ]
            if unmatched and not invoice.get("credit_review_overridden_at"):
                total_unmatched = sum(float(c.get("unapplied_amt") or 0) for c in unmatched)
                issues.append(
                    f"credit_review ({len(unmatched)} unmatched credit(s), "
                    f"${total_unmatched:.2f} unapplied)"
                )

        set_stage(conn, qbo_invoice_id, STAGE_PAYMENT_METHOD)
        pms = load_pms(conn, qbo_customer_id)
        result["payment_method"] = resolve_payment_method(wo.get("work_description"), pms)

        set_stage(conn, qbo_invoice_id, STAGE_CLASS)
        result["qbo_class"] = derive_qbo_class(wo.get("assigned_to"), wo.get("type"),
                                                wo.get("work_description"))

        set_stage(conn, qbo_invoice_id, STAGE_MEMO)
        enrichment_ok = True
        composed = None

        if is_memo_locked(invoice):
            # User already approved / edited this memo — preserve it.
            composed = invoice.get("memo")
            result["memo"] = composed
            result["statement_memo"] = invoice.get("statement_memo") or composed
            print(f"  memo locked — preserving '{composed}'")
        else:
            memo_result = generate_memo(wo, api_key)
            memo_text = None
            if "error" in memo_result:
                enrichment_ok = False
                issues.append(f"memo_api_error ({memo_result['error'][:80]})")
            elif memo_result.get("confidence", 0) < MEMO_CONFIDENCE_THRESHOLD:
                enrichment_ok = False
                issues.append(f"memo_low_confidence ({memo_result.get('confidence', 0):.0%})")
                memo_text = memo_result.get("memo")
            else:
                memo_text = memo_result.get("memo")

            composed = f"WO#{wo_number}: {memo_text}" if memo_text else None
            result["memo"] = composed
            result["statement_memo"] = composed

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
                issues.append(f"qbo_write_failed ({uw.get('error', '')[:80]})")
            else:
                qbo_inv = uw.get("invoice") or fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id) or qbo_inv

        result["enrichment_ok"] = enrichment_ok
        refresh_invoice_cache(conn, qbo_invoice_id, qbo_inv)

        if issues:
            result["billing_status"] = "needs_review"
            result["needs_review_reason"] = ", ".join(issues)
        else:
            result["billing_status"] = "ready_to_process"
        write_result(conn, qbo_invoice_id, result)

        return {"status": result["billing_status"], "qbo_invoice_id": qbo_invoice_id,
                "wo_number": wo_number, "subtotal_ok": subtotal_ok, "enrichment_ok": enrichment_ok,
                "payment_method": result["payment_method"], "qbo_class": result["qbo_class"],
                "memo": composed,
                "credits_applied_count": len([c for c in result["credits_applied"] if c["success"]]),
                "needs_review_reason": result.get("needs_review_reason")}

    except Exception as e:
        # Exception path — preserve existing invoice fields (memo, class, etc)
        # via narrow UPDATE instead of full write_result that would null them.
        try:
            fail_flag(conn, qbo_invoice_id, "needs_review",
                      f"pre_processing_error: {str(e)[:200]}")
        except Exception:
            pass
        return {"status": "error", "qbo_invoice_id": qbo_invoice_id, "error": str(e)[:500]}


def main(qbo_invoice_id: str = None, force: bool = False,
         bulk_all: bool = True, limit: int = None, sleep_ms: int = 1500,
         include_needs_review: bool = True,
         include_ready_to_process: bool = False):
    if not qbo_invoice_id and not bulk_all:
        return {"status": "error", "error": "pass qbo_invoice_id or bulk_all=True"}

    print(f"=== pre_process_invoice (bulk={bulk_all}, limit={limit}, force={force}, sleep={sleep_ms}ms) ===")
    conn = get_db_conn()
    try:
        access_token, realm_id = refresh_qbo_token()
        api_key = wmill.get_variable(ANTHROPIC_KEY_VAR)

        if not bulk_all:
            return process_one(conn, qbo_invoice_id, access_token, realm_id, api_key, force)

        cur = conn.cursor()
        statuses = ["'awaiting_pre_processing'"]
        if include_needs_review:
            statuses.append("'needs_review'")
        if include_ready_to_process:
            # One-time cleanup / full-queue re-audit. Default off to prevent
            # accidental overwrite of ready_to_process invoices during normal runs.
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
