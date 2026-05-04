import requests
import calendar

def main(billing_month: str, access_token: str, realm_id: str, dry_run: bool = True):
    """Apply unapplied maint payments AND maint credit memos to current-month invoices."""
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    year, month = map(int, billing_month.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    target_date = f"{year}-{month:02d}-{last_day:02d}"
    page_size = 1000

    def qbo_query(q):
        r = requests.get(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
            headers=headers, params={"query": q},
        )
        if not r.ok:
            raise Exception(f"QBO query failed: {r.text[:300]}")
        return r.json().get("QueryResponse", {})

    def get_open_invoices_for_customer(customer_id):
        data = qbo_query(f"SELECT * FROM Invoice WHERE CustomerRef = '{customer_id}' AND Balance > '0'")
        invs = data.get("Invoice", [])
        return ([i for i in invs if i.get("TxnDate") == target_date],
                [i for i in invs if i.get("TxnDate") != target_date])

    # ---------- Pass 1: unapplied Payments ----------
    maint_payments = []
    start = 1
    while True:
        data = qbo_query(f"SELECT * FROM Payment STARTPOSITION {start} MAXRESULTS {page_size}")
        payments = data.get("Payment", [])
        if not payments:
            break
        for p in payments:
            if float(p.get("UnappliedAmt", 0) or 0) <= 0:
                continue
            if "maint" in (p.get("PrivateNote", "") or "").lower():
                maint_payments.append(p)
        if len(payments) < page_size:
            break
        start += page_size
        if start > 10000:
            break

    payment_results = []
    for payment in maint_payments:
        customer_id = payment.get("CustomerRef", {}).get("value")
        customer_name = payment.get("CustomerRef", {}).get("name")
        payment_id = payment.get("Id")
        unapplied_amt = float(payment.get("UnappliedAmt", 0))
        memo = payment.get("PrivateNote", "")
        maint_invs, other_invs = get_open_invoices_for_customer(customer_id)
        if not maint_invs:
            payment_results.append({
                "customer": customer_name, "customer_id": customer_id,
                "payment_id": payment_id, "unapplied_amt": unapplied_amt, "memo": memo,
                "action": "SKIPPED - No maintenance invoices found",
                "other_open_invoices": len(other_invs),
            })
            continue
        total_balance = sum(float(i.get("Balance", 0)) for i in maint_invs)
        amount_to_apply = min(unapplied_amt, total_balance)
        # Preserve already-linked lines from the existing payment.
        lines = [ln for ln in payment.get("Line", []) if ln.get("LinkedTxn")]
        invoices_applied = []
        remaining = amount_to_apply
        for inv in maint_invs:
            if remaining <= 0:
                break
            inv_balance = float(inv.get("Balance", 0))
            apply_amt = min(remaining, inv_balance)
            lines.append({
                "Amount": apply_amt,
                "LinkedTxn": [{"TxnId": inv.get("Id"), "TxnType": "Invoice"}],
            })
            invoices_applied.append({
                "invoice_id": inv.get("Id"), "doc_number": inv.get("DocNumber"),
                "balance_before": inv_balance, "amount_applied": apply_amt,
            })
            remaining -= apply_amt
        result = {
            "customer": customer_name, "customer_id": customer_id,
            "payment_id": payment_id, "unapplied_amt": unapplied_amt, "memo": memo,
            "amount_to_apply": amount_to_apply, "invoices_to_apply": invoices_applied,
            "other_open_invoices": len(other_invs),
        }
        if dry_run:
            result["action"] = "DRY RUN - Would apply payment"
        else:
            update_resp = requests.post(
                f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "Id": payment_id, "SyncToken": payment.get("SyncToken"),
                    "CustomerRef": {"value": customer_id},
                    "TotalAmt": payment.get("TotalAmt"), "sparse": True, "Line": lines,
                },
            )
            if update_resp.ok:
                result["action"] = "SUCCESS - Payment applied"
                result["new_unapplied"] = update_resp.json().get("Payment", {}).get("UnappliedAmt")
                emailed = []
                for inv in invoices_applied:
                    if inv["amount_applied"] >= inv["balance_before"]:
                        try:
                            er = requests.post(
                                f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/invoice/{inv['invoice_id']}/send",
                                headers={**headers, "Content-Type": "application/octet-stream"},
                            )
                            if er.ok:
                                emailed.append(inv["doc_number"])
                        except Exception:
                            pass
                if emailed:
                    result["invoices_emailed"] = emailed
            else:
                result["action"] = "FAILED"
                result["error"] = update_resp.text[:500]
        payment_results.append(result)

    # ---------- Pass 2: maint-tagged CreditMemos with remaining credit ----------
    maint_credit_memos = []
    start = 1
    while True:
        data = qbo_query(f"SELECT * FROM CreditMemo WHERE Balance > '0' STARTPOSITION {start} MAXRESULTS {page_size}")
        cms = data.get("CreditMemo", [])
        if not cms:
            break
        for cm in cms:
            if float(cm.get("RemainingCredit", 0) or 0) <= 0:
                continue
            if "maint" in (cm.get("PrivateNote", "") or "").lower():
                maint_credit_memos.append(cm)
        if len(cms) < page_size:
            break
        start += page_size
        if start > 10000:
            break

    credit_memo_results = []
    for cm in maint_credit_memos:
        customer_id = cm.get("CustomerRef", {}).get("value")
        customer_name = cm.get("CustomerRef", {}).get("name")
        cm_id = cm.get("Id")
        cm_doc = cm.get("DocNumber")
        remaining = float(cm.get("RemainingCredit", 0) or 0)
        memo = cm.get("PrivateNote", "")
        maint_invs, other_invs = get_open_invoices_for_customer(customer_id)
        if not maint_invs:
            credit_memo_results.append({
                "customer": customer_name, "customer_id": customer_id,
                "credit_memo_id": cm_id, "credit_memo_doc": cm_doc,
                "remaining_credit": remaining, "memo": memo,
                "action": "SKIPPED - No maintenance invoices found",
                "other_open_invoices": len(other_invs),
            })
            continue
        invoices_to_apply = []
        rem = remaining
        for inv in maint_invs:
            if rem <= 0:
                break
            inv_balance = float(inv.get("Balance", 0))
            apply_amt = min(rem, inv_balance)
            invoices_to_apply.append({
                "invoice_id": inv.get("Id"), "doc_number": inv.get("DocNumber"),
                "balance_before": inv_balance, "amount_applied": apply_amt,
            })
            rem -= apply_amt
        amount_to_apply = remaining - rem
        result = {
            "customer": customer_name, "customer_id": customer_id,
            "credit_memo_id": cm_id, "credit_memo_doc": cm_doc,
            "remaining_credit": remaining, "memo": memo,
            "amount_to_apply": amount_to_apply, "invoices_to_apply": invoices_to_apply,
            "other_open_invoices": len(other_invs),
        }
        if dry_run:
            result["action"] = "DRY RUN - Would apply credit memo"
        else:
            payment_lines = [
                {
                    "Amount": inv["amount_applied"],
                    "LinkedTxn": [
                        {"TxnId": inv["invoice_id"], "TxnType": "Invoice"},
                        {"TxnId": cm_id, "TxnType": "CreditMemo"},
                    ],
                }
                for inv in invoices_to_apply
            ]
            create_resp = requests.post(
                f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "TotalAmt": 0,
                    "CustomerRef": {"value": customer_id},
                    "Line": payment_lines,
                    "PrivateNote": f"Auto-applied maint credit memo {cm_doc}",
                },
            )
            if create_resp.ok:
                result["action"] = "SUCCESS - Credit memo applied"
                result["created_payment_id"] = create_resp.json().get("Payment", {}).get("Id")
            else:
                result["action"] = "FAILED"
                result["error"] = create_resp.text[:500]
        credit_memo_results.append(result)

    return {
        "billing_month": billing_month, "target_invoice_date": target_date,
        "dry_run": dry_run,
        "total_maint_payments_found": len(maint_payments),
        "would_apply": len([r for r in payment_results if "Would apply" in r.get("action", "") or "SUCCESS" in r.get("action", "")]),
        "skipped": len([r for r in payment_results if "SKIPPED" in r.get("action", "")]),
        "results": payment_results,
        "total_maint_credit_memos_found": len(maint_credit_memos),
        "credit_memos_would_apply": len([r for r in credit_memo_results if "Would apply" in r.get("action", "") or "SUCCESS" in r.get("action", "")]),
        "credit_memos_skipped": len([r for r in credit_memo_results if "SKIPPED" in r.get("action", "")]),
        "credit_memo_results": credit_memo_results,
    }
