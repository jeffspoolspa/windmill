# f/service_billing/record_cc_payment
#
# Record an ALREADY-SETTLED Intuit merchant card transaction as a QBO Payment
# on a given customer, carrying its CCTransId.
#
# Module: docs/modules/service/billing.md
# Status: [active]
# Concurrency key: qbo_writer
#
# Triggered by:
#   - manual only (a human repairing a misapplied merchant payment)
#
# Tables touched:
#   (none — QBO is the only system of record touched; billing.customer_payments
#    picks the new Payment up on the next f/service_billing/refresh_payment run,
#    which regenerates cc_trans_id from the raw payload)
#
# External APIs:
#   - QBO: GET /query (duplicate guard), POST /payment (create)
#
# Why this exists:
#   Merchant charges keyed by hand in QBO land on whatever customer the keyer
#   picked, and near-identical names get picked wrong. 2026-08-07: CCTransId
#   13ad2rtuzp02, $1,250, AVS zip 31520 — booked to MCKINNEY, CARLA (6559,
#   zip 31525) when the AVS zip matches MCKENNIE, THERESA (2780, zip 31520).
#   QBO cannot move a Payment between customers, so the repair is delete +
#   recreate, and the recreate must carry the original CCTransId or the payment
#   no longer ties to its merchant deposit.
#
#   Neither existing script could do it: f/check_buddy/create_qbo_payment
#   hardcodes PaymentMethodRef 6 (Check) with no CreditCardPayment block, and
#   f/billing/_lib/qbo.record_qbo_payment is a library call that stamps today's
#   date and requires invoice lines.
#
#   ProcessPayment is the load-bearing flag, learned the hard way on Payment
#   71804 (2026-08-14): sent WITHOUT it, QBO stored the payment, normalized
#   ProcessPayment to false, and silently DROPPED the whole CreditChargeResponse
#   block — no CCTransId, no AuthCode, no merchant linkage. Every payment our
#   own pipeline writes carries CreditChargeInfo.ProcessPayment = true alongside
#   CreditChargeResponse (see 71769 / 71768 / 71760), and those all persist their
#   CCTransId. The flag is what makes QBO honour a supplied response.
#
#   It does not cause a second charge: the payload carries no card number, token
#   or CardEntityRef, so QBO has nothing to charge, and f/billing/_lib/qbo
#   .record_qbo_payment has sent exactly this shape on every live autopay for
#   months — the charge always happened first at Intuit and QBO recorded the
#   supplied response rather than running one. It still defaults to False here,
#   because "probably cannot charge" is not a default worth taking on someone
#   else's card.

import requests
import wmill

TOKEN_PROVIDER = "f/qbo/get_access_token"


def _qbo(access_token):
    return {"Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json"}


def main(
    customer_id: str,
    amount: float,
    cc_trans_id: str,
    txn_date: str,                      # YYYY-MM-DD — the ORIGINAL charge date
    auth_code: str = None,
    payment_ref: str = None,            # PaymentRefNum from the original
    payment_method_id: str = "29",      # 29 = the CC method the original used
    deposit_to_account_id: str = None,  # omit -> Undeposited Funds
    cc_expiry_year: int = None,
    cc_expiry_month: int = None,
    txn_authorization_time: str = None,
    memo: str = None,
    invoices: list[dict] = None,        # [{"id": "123", "amount_applied": 100.0}]
    process_payment: bool = False,      # REQUIRED for QBO to keep CCTransId
    dry_run: bool = True,
) -> dict:
    """Recreate a settled merchant CC payment on a customer, keeping CCTransId.

    Defaults to dry_run: returns the exact payload it WOULD post so a human can
    read it before any money-side write happens.
    """
    if not (customer_id and cc_trans_id and txn_date):
        raise Exception("customer_id, cc_trans_id and txn_date are required")
    if amount is None or float(amount) <= 0:
        raise Exception("amount must be positive")

    amount = round(float(amount), 2)

    charge_info = {"Amount": amount}
    if process_payment:
        charge_info["ProcessPayment"] = True

    payment = {
        "CustomerRef": {"value": str(customer_id)},
        "TotalAmt": amount,
        "TxnDate": txn_date,
        "PaymentMethodRef": {"value": str(payment_method_id)},
        "CreditCardPayment": {
            "CreditChargeInfo": charge_info,
            "CreditChargeResponse": {
                "Status": "Completed",
                "CCTransId": cc_trans_id,
            },
        },
        "TxnSource": "IntuitPayment",
    }
    if payment_ref:
        payment["PaymentRefNum"] = str(payment_ref)[:21]
    if memo:
        payment["PrivateNote"] = memo
    if deposit_to_account_id:
        payment["DepositToAccountRef"] = {"value": str(deposit_to_account_id)}
    if auth_code:
        payment["CreditCardPayment"]["CreditChargeResponse"]["AuthCode"] = auth_code
    if txn_authorization_time:
        payment["CreditCardPayment"]["CreditChargeResponse"]["TxnAuthorizationTime"] = \
            txn_authorization_time
    if cc_expiry_year and cc_expiry_month:
        charge_info["CcExpiryYear"] = int(cc_expiry_year)
        charge_info["CcExpiryMonth"] = int(cc_expiry_month)
    if invoices:
        payment["Line"] = [
            {"Amount": round(float(inv["amount_applied"]), 2),
             "LinkedTxn": [{"TxnId": str(inv["id"]), "TxnType": "Invoice"}]}
            for inv in invoices
        ]

    if dry_run:
        return {"dry_run": True, "would_post": payment,
                "note": "re-run with dry_run=false to create this payment",
                "warning": (None if process_payment else
                            "process_payment is false — QBO will drop "
                            "CreditChargeResponse and the payment will carry NO "
                            "CCTransId (observed on Payment 71804)")}

    token = wmill.run_script_by_path(TOKEN_PROVIDER, args={})
    access_token, realm_id = token["access_token"], token["realm_id"]
    base = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}"
    headers = _qbo(access_token)

    # Duplicate guard. QBO cannot filter on CCTransId, so match the natural key
    # a re-run would collide on: same customer, same date, same amount. Without
    # this, running twice records $2,500 against one $1,250 settlement.
    probe = requests.get(
        f"{base}/query", headers=headers, timeout=60,
        params={"query": ("SELECT * FROM Payment WHERE CustomerRef = "
                          f"'{customer_id}' AND TxnDate = '{txn_date}'")},
    )
    if not probe.ok:
        raise Exception(f"Duplicate check failed: {probe.status_code} - {probe.text}")
    for existing in (probe.json().get("QueryResponse") or {}).get("Payment") or []:
        if round(float(existing.get("TotalAmt") or 0), 2) == amount:
            return {"success": False, "already_exists": True,
                    "payment_id": existing.get("Id"),
                    "error": (f"Payment {existing.get('Id')} already records "
                              f"${amount} for customer {customer_id} on {txn_date}")}

    resp = requests.post(f"{base}/payment", headers=headers, json=payment, timeout=60)
    if not resp.ok:
        raise Exception(f"Create payment failed: {resp.status_code} - {resp.text}")

    created = resp.json().get("Payment", {})
    written = ((created.get("CreditCardPayment") or {})
               .get("CreditChargeResponse") or {}).get("CCTransId")
    return {
        "success": True,
        "payment_id": created.get("Id"),
        "customer_name": (created.get("CustomerRef") or {}).get("name"),
        "total": float(created.get("TotalAmt") or 0),
        "txn_date": created.get("TxnDate"),
        "unapplied": float(created.get("UnappliedAmt") or 0),
        "cc_trans_id": written,
        # Proof the linkage survived the write, not an assumption that it did.
        "cc_trans_id_ok": written == cc_trans_id,
    }
