# f/service_billing/apply_credit_manual
#
# Apply a single open credit to a specific invoice — triggered from the UI
# (triage mode or detail page "Apply" button).
#
# Returns an AUTHORITATIVE outcome:
#   - Fetches invoice balance BEFORE the apply
#   - POSTs the apply to QBO
#   - Fetches invoice balance AFTER the apply
#   - Returns success ONLY IF balance decreased by the applied amount
#   - Also runs billing.recheck_invoice_status so billing_status +
#     needs_review_reason reflect the new state in the same round-trip
#     (the UI patches its cache from the reconciled row).
#
# Closed-period Payments: QBO can silently reject sparse updates (returns
# 200 but the balance doesn't move). Detected via post-balance verification.

import json
from datetime import date, datetime
from decimal import Decimal

import requests
import wmill
import psycopg2
import psycopg2.extras

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"

# How close post-balance needs to be to (pre - applied) to count as success.
# Accounts for QBO rounding on fractional cents.
BALANCE_TOLERANCE = 0.01


def _json_safe(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(x) for x in obj]
    return obj


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


def qbo_get(path, access_token, realm_id, params=None):
    return requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/{path}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        params=params, timeout=30,
    )


def qbo_post(path, access_token, realm_id, body):
    return requests.post(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/{path}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json",
                 "Content-Type": "application/json"},
        json=body, timeout=30,
    )


def fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id):
    resp = qbo_get(f"invoice/{qbo_invoice_id}", access_token, realm_id)
    if not resp.ok:
        return None
    return resp.json().get("Invoice")


def apply_credit_memo(credit_id, customer_ref, invoice_qbo_id, amount, access_token, realm_id):
    """CreditMemo application: create a zero-amount Payment (current period)
    that links both the CreditMemo and the Invoice. Works even if the
    CreditMemo itself is in a locked period because the new Payment is current."""
    cm_id = credit_id.replace("CM-", "") if credit_id.startswith("CM-") else credit_id
    resp = qbo_post("payment", access_token, realm_id, {
        "CustomerRef": customer_ref,
        "TotalAmt": 0,
        "Line": [{
            "Amount": amount,
            "LinkedTxn": [
                {"TxnId": cm_id, "TxnType": "CreditMemo"},
                {"TxnId": invoice_qbo_id, "TxnType": "Invoice"},
            ],
        }],
    })
    return resp


def apply_payment_credit(qbo_payment_id, invoice_qbo_id, amount, access_token, realm_id):
    """Unapplied Payment: sparse-update the existing Payment to append a
    new Line linking to the invoice.

    CAVEAT: if the Payment's TxnDate is in a locked period (e.g., previous
    year with closed books), QBO can return 200 while silently rejecting
    the update. The caller MUST verify via post-balance check."""
    pmt_resp = qbo_get(f"payment/{qbo_payment_id}", access_token, realm_id)
    if not pmt_resp.ok:
        return pmt_resp
    payment = pmt_resp.json().get("Payment", {})
    payment.setdefault("Line", []).append({
        "Amount": amount,
        "LinkedTxn": [{"TxnId": invoice_qbo_id, "TxnType": "Invoice"}],
    })
    payment["sparse"] = True
    return qbo_post("payment", access_token, realm_id, payment)


def main(qbo_invoice_id: str, credit_id: str, amount: float = None):
    """
    Returns on success:
      {"status": "success", "qbo_invoice_id": ..., "credit_id": ...,
       "amount_applied": X, "pre_balance": P, "post_balance": Q,
       "invoice": {...reconciled row...}, "recheck": {...summary...}, ...}

    Returns on failure:
      {"status": "error", "error": "<reason>", ...}
    """
    if not qbo_invoice_id or not credit_id:
        return {"status": "error", "error": "qbo_invoice_id and credit_id required"}

    print(f"=== apply_credit_manual invoice={qbo_invoice_id} credit={credit_id} ===")
    conn = get_db_conn()
    try:
        access_token, realm_id = refresh_qbo_token()

        # Load the credit from our cache
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT qbo_payment_id, qbo_customer_id, type, unapplied_amt,
                   ref_num, memo, txn_date
            FROM billing.customer_payments WHERE qbo_payment_id = %s
        """, (credit_id,))
        credit = cur.fetchone()
        if not credit:
            return {"status": "error",
                    "error": f"credit {credit_id} not found in billing.customer_payments"}
        credit = dict(credit)
        cur.close()

        # Fetch current QBO invoice state — BEFORE the apply
        qbo_inv = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
        if not qbo_inv:
            return {"status": "error", "error": "QBO invoice fetch failed"}
        pre_balance = float(qbo_inv.get("Balance", 0) or 0)
        customer_ref = qbo_inv.get("CustomerRef")

        # Amount capped at min(credit unapplied, invoice balance)
        unapplied = float(credit["unapplied_amt"] or 0)
        chosen = amount if amount is not None else min(unapplied, pre_balance)
        chosen = round(float(chosen), 2)
        if chosen <= 0:
            return {"status": "error",
                    "error": f"nothing to apply (unapplied={unapplied}, balance={pre_balance})",
                    "pre_balance": pre_balance}

        # Dispatch by credit type
        if credit["type"] == "credit_memo":
            resp = apply_credit_memo(credit_id, customer_ref, qbo_inv["Id"], chosen,
                                      access_token, realm_id)
        else:
            resp = apply_payment_credit(credit_id, qbo_inv["Id"], chosen,
                                         access_token, realm_id)

        # Hard error — QBO explicitly rejected
        if not resp.ok:
            err_text = resp.text[:400]
            qbo_code = None
            closed_period = False
            try:
                body = resp.json()
                faults = (body.get("Fault") or {}).get("Error") or []
                if faults:
                    qbo_code = faults[0].get("code")
                    err_msgs = [f.get("Message") or f.get("Detail") or str(f) for f in faults]
                    err_text = "; ".join(err_msgs)[:400]
                    if qbo_code == "6210" or "Account Period Closed" in err_text:
                        closed_period = True
            except Exception:
                pass

            if closed_period:
                err_text = (
                    f"This credit (txn_date {credit.get('txn_date')}) is in a closed "
                    f"accounting period. QBO's API cannot modify closed-period records "
                    f"(error 6210). Options: (1) apply directly in the QBO website's "
                    f"Receive Payment screen, or (2) use Override Credit Review if "
                    f"these credits are not applicable to this invoice."
                )

            return {"status": "error",
                    "error": err_text,
                    "credit_id": credit_id,
                    "amount_attempted": chosen,
                    "pre_balance": pre_balance,
                    "post_balance": pre_balance,
                    "qbo_status_code": resp.status_code,
                    "qbo_error_code": qbo_code,
                    "closed_period": closed_period}

        # QBO returned 2xx — verify via post-balance check (closed-period
        # Payments can return 200 but silently no-op).
        qbo_inv_after = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
        post_balance = float(qbo_inv_after.get("Balance", 0) or 0) if qbo_inv_after else pre_balance
        balance_change = pre_balance - post_balance
        expected_change = chosen

        if abs(balance_change - expected_change) < BALANCE_TOLERANCE:
            verified = True
            verify_note = None
        elif balance_change >= expected_change - BALANCE_TOLERANCE:
            verified = True
            verify_note = (f"balance dropped {balance_change:.2f} which is more than "
                           f"the {expected_change:.2f} we applied — another apply may have run")
        else:
            verified = False
            verify_note = (
                f"QBO returned 2xx but invoice balance only changed by "
                f"${balance_change:.2f} (expected ${expected_change:.2f}). "
                f"Likely cause: credit is in a locked accounting period — "
                f"QBO silently ignored the sparse update. "
                f"Workaround: create a current-period CreditMemo and apply THAT."
            )

        if not verified:
            return {"status": "error",
                    "error": verify_note,
                    "credit_id": credit_id,
                    "amount_attempted": chosen,
                    "pre_balance": pre_balance,
                    "post_balance": post_balance,
                    "qbo_response_ok": True,
                    "silent_reject": True,
                    "credit_txn_date": str(credit.get("txn_date")) if credit.get("txn_date") else None}

        # Verified success — update cache atomically:
        #   1. Decrement the credit's unapplied_amt
        #   2. Write the manual apply link
        #   3. Update billing.invoices.balance (so the triage snapshot /
        #      future reads see the fresh number without a separate
        #      refresh round-trip)
        #   4. Run recheck_invoice_status to reconcile billing_status +
        #      needs_review_reason (clears credit_review if no applicable
        #      credits remain for the customer)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            UPDATE billing.customer_payments
            SET unapplied_amt = GREATEST(unapplied_amt - %s, 0)
            WHERE qbo_payment_id = %s
        """, (chosen, credit_id))
        cur.execute("""
            INSERT INTO billing.payment_invoice_links
              (payment_id, invoice_id, amount, applied_via)
            VALUES (%s, %s, %s, 'manual')
            ON CONFLICT (payment_id, invoice_id) DO UPDATE SET
              amount = billing.payment_invoice_links.amount + EXCLUDED.amount,
              applied_via = 'manual'
        """, (credit_id, qbo_invoice_id, chosen))
        cur.execute("""
            UPDATE billing.invoices
            SET balance = %s, fetched_at = now()
            WHERE qbo_invoice_id = %s
        """, (post_balance, qbo_invoice_id))
        cur.execute("SELECT billing.recheck_invoice_status(%s) AS r", (qbo_invoice_id,))
        recheck = cur.fetchone()["r"]
        conn.commit()
        cur.close()

        print(f"  applied ${chosen:.2f} — pre={pre_balance:.2f} post={post_balance:.2f}")
        print(f"  recheck: {recheck.get('prev_billing_status')} → "
              f"{recheck.get('new_billing_status')} (changed={recheck.get('changed')})")

        # Track expected QBO webhook for the Payment (or CreditMemo) we
        # modified. This is the canonical event QBO fires when LinkedTxn
        # changes — confirms QBO accepted our write.
        #
        # We INTENTIONALLY do not track an Invoice webhook expectation
        # here, even though the invoice's balance changed: QBO often does
        # not fire the derived Invoice.update webhook for credit applies,
        # and we've already updated billing.invoices.balance directly from
        # the post-apply QBO read above. Tracking it would surface
        # spurious 'missing' alerts for invoices whose cache state is in
        # fact correct.
        try:
            _exp_cur = conn.cursor()
            _cm_match = credit_id.startswith("CM-")
            _entity_type = "CreditMemo" if _cm_match else "Payment"
            _entity_id = credit_id[3:] if _cm_match else credit_id
            _exp_cur.execute("""
                INSERT INTO billing.webhook_expectations
                  (entity_type, entity_id, expected_by, source)
                VALUES (%s, %s, now() + interval '5 minutes', 'self_initiated')
            """, (_entity_type, _entity_id))
            conn.commit()
            _exp_cur.close()
        except Exception as e:
            print(f"  (webhook_expectation insert failed: {e})")

        # NOTE: previously this script chained an async pre_process_invoice
        # run to re-derive subtotal + memo. With the new reactive
        # architecture, that's redundant + wasteful:
        #   - subtotal_ok is recomputed by recheck (called above) AND by
        #     the reactive triggers we'll add later
        #   - credit_review is recomputed by trg_recheck_credits_on_payment_change
        #     which fires on the customer_payments UPDATE we just did
        #   - memo doesn't depend on credits and shouldn't be regenerated
        #     (would burn an OpenAI call + re-PATCH QBO)
        # Manual "Re-run pre-process" remains the escape hatch when memo
        # actually needs regeneration.

        return {
            "status": "success",
            "qbo_invoice_id": qbo_invoice_id,
            "credit_id": credit_id,
            "amount_applied": chosen,
            "pre_balance": pre_balance,
            "post_balance": post_balance,
            "verify_note": verify_note,
            "invoice": _json_safe(recheck.get("invoice")) if recheck.get("status") == "ok" else None,
            "recheck": _json_safe({
                "changed": recheck.get("changed"),
                "prev_billing_status": recheck.get("prev_billing_status"),
                "new_billing_status": recheck.get("new_billing_status"),
                "prev_reason": recheck.get("prev_reason"),
                "new_reason": recheck.get("new_reason"),
            }),
            "chained_pre_process": False,
        }
    finally:
        conn.close()
