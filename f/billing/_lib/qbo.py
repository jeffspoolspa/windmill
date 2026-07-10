# requirements:
# requests
# wmill

"""
f/billing/_lib/qbo — shared QuickBooks Online / Intuit Payments primitives.

ADR 009: one primitive = one external side effect. Despite the f/billing path
(the Windmill-proven place for a shared module; cross-area import works — see
billing_audit importing f.ION._lib), these are shared across billing AND
service_billing. Extracted VERBATIM from the deployed engines
(process_maint_period / process_invoice) so behavior is unchanged; the only
new code is the send_receipt / send_invoice split (ADR 009) and this
self-check.

Import as:  from f.billing._lib.qbo import charge_card, get_qbo_invoice_details, ...

Scope: auth (refresh_qbo_token — THE single implementation; the 35 engine
copies retire onto this one caller batch at a time), generic GET/POST, the
charge / fresh-read / payment / send primitives, and two thin compositions
(send_invoice_email, bump_invoice_due_date_to_today) that are QBO-generic and
carry no billing policy.

Every function is ONE external call (or pure), except the marked compositions.
No WAL / state-machine / idempotency-sequencing logic — that lives in
f/billing/_lib/payments (the service) and the engines.
"""

import json
import time
import requests
import wmill
from datetime import datetime, date

QBO_RESOURCE = "u/carter/quickbooks_api"

QBO_PMT_METHOD_CC = "21"
QBO_PMT_METHOD_ACH = "20"

_PAYMENTS_BASE = "https://api.intuit.com/quickbooks/v4/payments"
_QBO_BASE = "https://quickbooks.api.intuit.com/v3/company"


# ── auth (the ONE token refresh — the rotating refresh_token burns if two
#    copies race; see quickbooks-windmill skill) ─────────────────────────────

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


# ── generic HTTP verbs (one call each) ──────────────────────────────────────

def qbo_get(path, access_token, realm_id, params=None):
    return requests.get(
        f"{_QBO_BASE}/{realm_id}/{path}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        params=params, timeout=30,
    )


def qbo_post(path, access_token, realm_id, body):
    return requests.post(
        f"{_QBO_BASE}/{realm_id}/{path}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                 "Content-Type": "application/json"},
        json=body, timeout=30,
    )


def fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id):
    """Full invoice read: (Invoice dict, None) or (None, error). Use
    get_qbo_invoice_details when only balance/email_status matter."""
    resp = qbo_get(f"invoice/{qbo_invoice_id}", access_token, realm_id)
    if not resp.ok:
        return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
    return resp.json().get("Invoice"), None


def fetch_qbo_customer_email(customer_id, access_token, realm_id):
    resp = qbo_get(f"customer/{customer_id}", access_token, realm_id)
    if not resp.ok:
        return None
    customer = resp.json().get("Customer", {})
    return (customer.get("PrimaryEmailAddr") or {}).get("Address")


# ── pure helpers (no I/O — safe to unit-check) ──────────────────────────────

def _classify_charge_response(resp, payment_type):
    if resp is None:
        return "uncertain"
    sc = resp.status_code
    if sc >= 500:
        return "uncertain"
    if not resp.ok:
        return "declined"
    try:
        result = resp.json()
        status = (result.get("status") or "").upper()
        if payment_type == "card":
            return "success" if status == "CAPTURED" else "declined"
        return "success" if status in ("PENDING", "SUCCEEDED") else "declined"
    except Exception:
        return "uncertain"


def extract_charge_error(resp, body=None):
    if resp is None:
        return "no response from Intuit (network error)"
    if body is None:
        try:
            body = resp.json()
        except Exception:
            body = None
    sc = resp.status_code
    if body is None:
        text = (resp.text or "").strip()
        if text.startswith("<") or "<html" in text[:200].lower():
            return f"HTTP {sc}: gateway returned HTML (likely 5xx upstream)"
        return f"HTTP {sc}: {text[:300] if text else 'empty body'}"
    errors = body.get("errors") or []
    if errors:
        e = errors[0] if isinstance(errors[0], dict) else {}
        parts = [p for p in [e.get("message"), e.get("detail"),
                             f"code={e['code']}" if e.get("code") else None] if p]
        if parts:
            return f"HTTP {sc}: " + " | ".join(parts)
    if body.get("status") and body.get("status") not in ("CAPTURED", "PENDING", "SUCCEEDED"):
        msg = body.get("message") or body.get("detail") or ""
        return f"HTTP {sc}: status={body.get('status')}" + (f" | {msg}" if msg else "")
    return f"HTTP {sc}: " + json.dumps(body)[:300]


# ── charge primitives (one Intuit call each) ────────────────────────────────

def charge_card(card_id, amount, request_id, invoice_num, customer_name, access_token):
    payload = {"amount": f"{amount:.2f}", "currency": "USD", "capture": True,
               "cardOnFile": card_id, "context": {"mobile": False, "isEcommerce": True},
               "description": f"Invoice {invoice_num} - {customer_name}"}
    try:
        resp = requests.post(
            f"{_PAYMENTS_BASE}/charges",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                     "Content-Type": "application/json", "Request-Id": request_id},
            json=payload, timeout=30)
    except (requests.Timeout, requests.ConnectionError) as e:
        return {"classification": "uncertain", "error": f"network: {str(e)[:200]}",
                "request_id": request_id, "payment_type": "card"}
    classification = _classify_charge_response(resp, "card")
    base = {"classification": classification, "request_id": request_id, "payment_type": "card",
            "status_code": resp.status_code, "amount_requested": amount}
    body = None
    try:
        body = resp.json()
        base["raw_response"] = body
    except Exception:
        base["raw_text"] = resp.text[:500]
    if classification == "success" and body:
        return {**base, "charge_id": body.get("id"), "amount": float(body.get("amount", 0)),
                "auth_code": body.get("authCode"), "status": body.get("status"),
                "card_last4": (body.get("card") or {}).get("number", "")[-4:],
                "card_type": (body.get("card") or {}).get("cardType")}
    return {**base, "error": extract_charge_error(resp, body)}


def charge_bank_account(bank_id, amount, request_id, invoice_num, customer_name, access_token):
    payload = {"amount": f"{amount:.2f}", "bankAccountOnFile": bank_id,
               "description": f"Invoice {invoice_num} - {customer_name}",
               "paymentMode": "WEB",
               "context": {"deviceInfo": {"macAddress": "", "ipAddress": "", "longitude": "",
                                          "latitude": "", "phoneNumber": ""}}}
    try:
        resp = requests.post(
            f"{_PAYMENTS_BASE}/echecks",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                     "Content-Type": "application/json", "Request-Id": request_id},
            json=payload, timeout=30)
    except (requests.Timeout, requests.ConnectionError) as e:
        return {"classification": "uncertain", "error": f"network: {str(e)[:200]}",
                "request_id": request_id, "payment_type": "ach"}
    classification = _classify_charge_response(resp, "ach")
    base = {"classification": classification, "request_id": request_id, "payment_type": "ach",
            "status_code": resp.status_code, "amount_requested": amount}
    body = None
    try:
        body = resp.json()
        base["raw_response"] = body
    except Exception:
        base["raw_text"] = resp.text[:500]
    if classification == "success" and body:
        return {**base, "charge_id": body.get("id"), "amount": float(body.get("amount", 0)),
                "auth_code": body.get("authCode", ""), "status": body.get("status"),
                "card_last4": (body.get("bankAccount") or {}).get("accountNumber", "")[-4:],
                "card_type": "ACH"}
    return {**base, "error": extract_charge_error(resp, body)}


# ── invoice read (the money-path fresh read) ────────────────────────────────

def get_qbo_invoice_details(invoice_id, realm_id, access_token):
    """Fresh leader read of ONE invoice — money paths decide on this, not the
    cache. Returns {balance, email_status} or None on ANY failure (caller MUST
    halt on None; never fall back to the cache for a charge decision)."""
    try:
        resp = requests.get(
            f"{_QBO_BASE}/{realm_id}/invoice/{invoice_id}?minorversion=65",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=30)
        if not resp.ok:
            return None
        inv = resp.json().get("Invoice", {})
        if "Balance" not in inv:
            return None
        return {"balance": float(inv["Balance"]), "email_status": inv.get("EmailStatus")}
    except Exception:
        return None


# ── payment (one QBO Payment create; supports multi-invoice lines) ──────────

def build_payment_note(memo_prefix, charge_result):
    """PrivateNote for a recorded payment: caller's policy prefix (e.g.
    'Auto-charge | WO# 123 | Inv# 456' or 'June Pool Maintenance | Inv# 456')
    + the charge facts. Pure — the prefix is DATA; this module never knows
    what kind of invoice was charged."""
    return (f"{memo_prefix} | "
            f"Charge ID: {charge_result.get('charge_id', '')} | "
            f"Auth: {charge_result.get('auth_code', '')} | "
            f"{charge_result.get('card_type', '')} x{charge_result.get('card_last4', '')} | "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")


def record_qbo_payment(customer_id, amount, charge_result, payment_ref, memo_prefix,
                       access_token, realm_id, lines):
    """QBO Payment linked to the invoice(s), CCTransId = charge id.
    lines: [(qbo_invoice_id, amount), ...] — ONE payment applied across
    invoices (single-invoice callers pass one line). payment_ref / memo_prefix
    are caller policy (WO number vs month label) passed as data.
    Returns {success, payment_id} or {success: False, error, ...}."""
    charge_id = charge_result.get("charge_id", "")
    pmt_method_id = (QBO_PMT_METHOD_ACH if charge_result.get("payment_type") == "ach"
                     else QBO_PMT_METHOD_CC)
    resp = requests.post(
        f"{_QBO_BASE}/{realm_id}/payment",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                 "Content-Type": "application/json"},
        json={"CustomerRef": {"value": customer_id}, "TotalAmt": amount,
              "PaymentMethodRef": {"value": pmt_method_id},
              "PaymentRefNum": (payment_ref or "")[:21],
              "TxnDate": datetime.now().strftime("%Y-%m-%d"),
              "Line": [{"Amount": ln_amount,
                        "LinkedTxn": [{"TxnId": ln_invoice, "TxnType": "Invoice"}]}
                       for ln_invoice, ln_amount in lines],
              "PrivateNote": build_payment_note(memo_prefix, charge_result),
              "CreditCardPayment": {
                  "CreditChargeInfo": {"ProcessPayment": True, "Amount": amount},
                  "CreditChargeResponse": {"Status": "Completed", "CCTransId": charge_id}},
              "TxnSource": "IntuitPayment"},
        timeout=60)
    if not resp.ok:
        # QBO's Fault envelope differs from Intuit Payments'. Try it first,
        # then fall back to the generic extractor. (Hardening from the WO
        # engine's copy — now everyone gets it.)
        body = None
        try:
            body = resp.json()
        except Exception:
            pass
        err_msg = None
        if body:
            fault = (body.get("Fault") or {}).get("Error") or []
            if fault:
                f = fault[0] if isinstance(fault[0], dict) else {}
                parts = [f.get("Message"), f.get("Detail"),
                         f"code={f.get('code')}" if f.get("code") else None]
                err_msg = " | ".join(p for p in parts if p)
        if not err_msg:
            err_msg = extract_charge_error(resp, body)
        return {"success": False, "error": err_msg,
                "status_code": resp.status_code,
                "raw_response": body or resp.text[:500]}
    payment = resp.json().get("Payment", {})
    return {"success": True, "payment_id": payment.get("Id"),
            "payment_ref": payment.get("PaymentRefNum"),
            "total_amt": payment.get("TotalAmt")}


# ── send primitives — ONE call each (ADR 009 split) ─────────────────────────

def send_receipt(payment_id, email, access_token, realm_id):
    """Email a QBO Payment receipt (one call). {ok, error}."""
    if not email:
        return {"ok": False, "error": "no email on file"}
    r = requests.post(
        f"{_QBO_BASE}/{realm_id}/payment/{payment_id}/send?sendTo={email}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                 "Content-Type": "application/octet-stream"}, timeout=60)
    return {"ok": r.ok, "error": None if r.ok else f"receipt: HTTP {r.status_code} {r.text[:150]}"}


def send_invoice(invoice_id, email, access_token, realm_id):
    """Email a QBO invoice copy (one call). {ok, error}."""
    if not email:
        return {"ok": False, "error": "no email on file"}
    r = requests.post(
        f"{_QBO_BASE}/{realm_id}/invoice/{invoice_id}/send?sendTo={email}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                 "Content-Type": "application/octet-stream"}, timeout=60)
    return {"ok": r.ok, "error": None if r.ok else f"invoice: HTTP {r.status_code} {r.text[:150]}"}


def send_receipt_then_invoice(payment_id, invoice_id, email, access_token, realm_id,
                              invoice=True):
    """COMPOSITION over the two primitives above — kept so callers with the
    common both-sends case stay one line (same return shape as before). New
    callers wanting just one should call send_receipt / send_invoice directly.
    (Param renamed send_invoice -> invoice so it can't shadow the primitive.)"""
    out = {"receipt": False, "invoice": False, "errors": []}
    if not email:
        out["errors"].append("no email on file")
        return out
    if payment_id:
        r = send_receipt(payment_id, email, access_token, realm_id)
        out["receipt"] = r["ok"]
        if not r["ok"]:
            out["errors"].append(r["error"])
    if invoice:
        r = send_invoice(invoice_id, email, access_token, realm_id)
        out["invoice"] = r["ok"]
        if not r["ok"]:
            out["errors"].append(r["error"])
    return out


# ── QBO-generic compositions (2-3 calls; no billing policy, no domain noun) ─

def send_invoice_email(invoice_id, customer_id, access_token, realm_id):
    """POST /invoice/{id}/send to the customer's QBO primary email. Skips if
    EmailStatus is already EmailSent — exactly one customer-facing email per
    invoice, ever; something else having sent it makes this a no-op."""
    inv_resp = qbo_get(f"invoice/{invoice_id}", access_token, realm_id)
    if inv_resp.ok:
        inv = inv_resp.json().get("Invoice", {})
        if inv.get("EmailStatus") == "EmailSent":
            return {"success": True, "skipped": True, "reason": "Already sent"}

    email = fetch_qbo_customer_email(customer_id, access_token, realm_id)
    send_url = f"invoice/{invoice_id}/send"
    if email:
        send_url += f"?sendTo={email}"

    resp = requests.post(
        f"{_QBO_BASE}/{realm_id}/{send_url}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                 "Content-Type": "application/octet-stream"},
        timeout=30,
    )
    if not resp.ok:
        return {"success": False, "error": resp.text[:300], "email_attempted": email}
    return {"success": True, "sent_to": email}


def send_payment_receipt(payment_id, customer_id, access_token, realm_id):
    """Receipt to the customer's QBO primary email (fetches it first).
    send_receipt is the address-in-hand primitive underneath."""
    email = fetch_qbo_customer_email(customer_id, access_token, realm_id)
    if not email:
        return {"success": False, "error": "No customer email found"}
    r = send_receipt(payment_id, email, access_token, realm_id)
    if not r["ok"]:
        return {"success": False, "error": r["error"], "email_attempted": email}
    return {"success": True, "sent_to": email}


def bump_invoice_due_date_to_today(invoice_id, access_token, realm_id, max_retries=2):
    """Sparse-PATCH the invoice's DueDate to today so a long-parked invoice
    doesn't arrive showing OVERDUE in the QBO portal. No-ops when DueDate is
    already today/future. Retries stale SyncToken."""
    today_iso = date.today().isoformat()
    last_err = None
    for attempt in range(max_retries + 1):
        inv_resp = qbo_get(f"invoice/{invoice_id}", access_token, realm_id)
        if not inv_resp.ok:
            if attempt < max_retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            return {"success": False, "error": f"fetch failed: {inv_resp.status_code}"}
        inv = inv_resp.json().get("Invoice")
        if not inv:
            return {"success": False, "error": "QBO returned no Invoice"}
        current = inv.get("DueDate")
        if current and current >= today_iso:
            return {"success": True, "skipped": True, "current_due_date": current}
        resp = qbo_post("invoice", access_token, realm_id,
                        {"Id": inv["Id"], "SyncToken": inv["SyncToken"],
                         "sparse": True, "DueDate": today_iso})
        if resp.ok:
            return {"success": True, "old_due_date": current, "new_due_date": today_iso}
        text = resp.text[:400]
        last_err = f"HTTP {resp.status_code}: {text}"
        if "Stale Object" in text and attempt < max_retries:
            time.sleep(0.5 * (attempt + 1))
            continue
        break
    return {"success": False, "error": last_err}


# ── self-check: pure logic, NO network (run this to verify the extraction) ──

def _selfcheck():
    class R:
        def __init__(self, status_code, ok, body=None, text=""):
            self.status_code, self.ok, self._body, self.text = status_code, ok, body, text
        def json(self):
            if self._body is None:
                raise ValueError("no json")
            return self._body

    checks = []
    def ok(name, cond):
        checks.append((name, bool(cond)))

    ok("none->uncertain", _classify_charge_response(None, "card") == "uncertain")
    ok("500->uncertain", _classify_charge_response(R(503, False), "card") == "uncertain")
    ok("402->declined", _classify_charge_response(R(402, False), "card") == "declined")
    ok("card CAPTURED->success",
       _classify_charge_response(R(200, True, {"status": "CAPTURED"}), "card") == "success")
    ok("card PENDING->declined",
       _classify_charge_response(R(200, True, {"status": "PENDING"}), "card") == "declined")
    ok("ach PENDING->success",
       _classify_charge_response(R(200, True, {"status": "PENDING"}), "ach") == "success")
    ok("ach SUCCEEDED->success",
       _classify_charge_response(R(200, True, {"status": "SUCCEEDED"}), "ach") == "success")
    ok("error extracts message",
       "card expired" in extract_charge_error(
           R(402, False, {"errors": [{"message": "card expired", "code": "PMT-4000"}]})))
    ok("error handles html",
       "HTML" in extract_charge_error(R(502, False, None, "<html>bad gateway</html>")))
    note = build_payment_note("June Pool Maintenance | Inv# 456",
                              {"charge_id": "ch1", "auth_code": "A1",
                               "card_type": "Visa", "card_last4": "4242"})
    ok("payment note carries prefix + charge facts",
       note.startswith("June Pool Maintenance | Inv# 456 | Charge ID: ch1")
       and "Visa x4242" in note)

    failed = [n for n, p in checks if not p]
    return {"passed": len(checks) - len(failed), "total": len(checks), "failed": failed}


def main():
    """Run the no-network self-check (invocable as a Windmill job to verify the
    extraction). Charge/send/read functions are exercised live only through the
    engines that call them."""
    result = _selfcheck()
    result["ok"] = not result["failed"]
    return result


if __name__ == "__main__":
    print(main())
