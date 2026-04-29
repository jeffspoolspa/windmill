import requests
import calendar

def main(billing_month: str, access_token: str, realm_id: str, dry_run: bool = True):
    """
    Find all unapplied payments with 'maint' in memo and apply them to
    maintenance invoices (dated last day of the billing month).
    Uses token from init step - no refresh here.
    """
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    year, month = map(int, billing_month.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    target_date = f"{year}-{month:02d}-{last_day:02d}"

    maint_payments = []
    start_position = 1
    page_size = 1000

    while True:
        query = f"SELECT * FROM Payment STARTPOSITION {start_position} MAXRESULTS {page_size}"
        resp = requests.get(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
            headers=headers, params={"query": query}
        )
        if not resp.ok:
            raise Exception(f"Query failed: {resp.text}")

        payments = resp.json().get("QueryResponse", {}).get("Payment", [])
        if not payments:
            break

        for p in payments:
            unapplied = float(p.get("UnappliedAmt", 0) or 0)
            if unapplied <= 0:
                continue
            memo = p.get("PrivateNote", "") or ""
            if "maint" in memo.lower():
                maint_payments.append(p)

        start_position += page_size
        if start_position > 10000:
            break

    results = []

    for payment in maint_payments:
        customer_id = payment.get("CustomerRef", {}).get("value")
        customer_name = payment.get("CustomerRef", {}).get("name")
        payment_id = payment.get("Id")
        unapplied_amt = float(payment.get("UnappliedAmt", 0))
        memo = payment.get("PrivateNote", "")

        inv_query = f"SELECT * FROM Invoice WHERE CustomerRef = '{customer_id}' AND Balance > '0'"
        inv_resp = requests.get(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
            headers=headers, params={"query": inv_query}
        )
        invoices = inv_resp.json().get("QueryResponse", {}).get("Invoice", [])

        maint_invoices = [inv for inv in invoices if inv.get("TxnDate") == target_date]
        other_invoices = [inv for inv in invoices if inv.get("TxnDate") != target_date]

        if not maint_invoices:
            results.append({
                "customer": customer_name, "customer_id": customer_id,
                "payment_id": payment_id, "unapplied_amt": unapplied_amt,
                "memo": memo, "action": "SKIPPED - No maintenance invoices found",
                "other_open_invoices": len(other_invoices)
            })
            continue

        total_maint_balance = sum(float(inv.get("Balance", 0)) for inv in maint_invoices)
        amount_to_apply = min(unapplied_amt, total_maint_balance)

        lines = []
        existing_lines = payment.get("Line", [])
        for line in existing_lines:
            if line.get("LinkedTxn"):
                lines.append(line)

        invoices_applied = []
        remaining_to_apply = amount_to_apply
        for inv in maint_invoices:
            if remaining_to_apply <= 0:
                break
            inv_balance = float(inv.get("Balance", 0))
            apply_amt = min(remaining_to_apply, inv_balance)
            lines.append({
                "Amount": apply_amt,
                "LinkedTxn": [{"TxnId": inv.get("Id"), "TxnType": "Invoice"}]
            })
            invoices_applied.append({
                "invoice_id": inv.get("Id"), "doc_number": inv.get("DocNumber"),
                "balance_before": inv_balance, "amount_applied": apply_amt
            })
            remaining_to_apply -= apply_amt

        result = {
            "customer": customer_name, "customer_id": customer_id,
            "payment_id": payment_id, "unapplied_amt": unapplied_amt,
            "memo": memo, "amount_to_apply": amount_to_apply,
            "invoices_to_apply": invoices_applied,
            "other_open_invoices": len(other_invoices)
        }

        if dry_run:
            result["action"] = "DRY RUN - Would apply payment"
        else:
            update_payload = {
                "Id": payment_id, "SyncToken": payment.get("SyncToken"),
                "CustomerRef": {"value": customer_id},
                "TotalAmt": payment.get("TotalAmt"), "sparse": True, "Line": lines
            }
            update_resp = requests.post(
                f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment",
                headers={**headers, "Content-Type": "application/json"},
                json=update_payload
            )
            if update_resp.ok:
                result["action"] = "SUCCESS - Payment applied"
                result["new_unapplied"] = update_resp.json().get("Payment", {}).get("UnappliedAmt")
                emails_sent = []
                for inv in invoices_applied:
                    if inv['amount_applied'] >= inv['balance_before']:
                        try:
                            email_resp = requests.post(
                                f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/invoice/{inv['invoice_id']}/send",
                                headers={**headers, "Content-Type": "application/octet-stream"}
                            )
                            if email_resp.ok:
                                emails_sent.append(inv['doc_number'])
                        except:
                            pass
                if emails_sent:
                    result["invoices_emailed"] = emails_sent
            else:
                result["action"] = "FAILED"
                result["error"] = update_resp.text

        results.append(result)

    applied_count = len([r for r in results if "Would apply" in r.get("action", "") or "SUCCESS" in r.get("action", "")])
    skipped_count = len([r for r in results if "SKIPPED" in r.get("action", "")])

    return {
        "billing_month": billing_month, "target_invoice_date": target_date,
        "dry_run": dry_run, "total_maint_payments_found": len(maint_payments),
        "would_apply": applied_count, "skipped": skipped_count, "results": results
    }
