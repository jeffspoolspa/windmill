import psycopg2
import requests
import wmill
import json

def main(
    billing_run_id: str,
    credits_result: dict,
    customers_result: dict,
    processing_results: list,
    verification_results: list,
    billing_month: str,
    access_token: str,
    realm_id: str,
    dry_run: bool = True
):
    """
    Finalize billing run stats and sync invoice balances.

    After autopay, updates billing_audit.maintenance_invoices.balance_due
    for any invoices that were paid, so the audit system reflects current state.
    """
    completed = []
    no_invoice = []
    no_payment_method = []
    charge_declined = []
    charge_api_failures = []
    payment_failed = []
    errors = []
    dry_runs = []

    for r in processing_results:
        status = r.get("status")
        s = {
            "name": r.get("customer_name"),
            "amount": r.get("amount_charged"),
            "invoices": r.get("invoices_paid", []),
            "has_outstanding": r.get("has_outstanding", False),
            "outstanding_invoices": r.get("outstanding_invoices", []),
            "notes": r.get("notes", [])
        }
        if status in ("completed", "awaiting_verification"):
            completed.append(s)
        elif status == "dry_run_success":
            dry_runs.append(s)
        elif status == "no_invoice":
            no_invoice.append(s)
        elif status == "no_payment_method":
            no_payment_method.append(s)
        elif status == "charge_declined":
            charge_declined.append(s)
        elif status == "charge_api_failure":
            charge_api_failures.append(s)
        elif status == "payment_failed":
            payment_failed.append(s)
        elif status == "error":
            errors.append(s)

    verified_count = sum(1 for v in (verification_results or []) if v.get("verified"))
    emails_sent_count = sum(1 for v in (verification_results or []) if v.get("email_sent"))
    decline_emails_sent = sum(1 for v in (verification_results or []) if v.get("decline_email_sent"))
    review_count = sum(1 for v in (verification_results or []) if v.get("original_status") == "awaiting_verification" and not v.get("verified"))

    total_charged = sum(
        (r.get("amount_charged", 0) or 0)
        for r in processing_results
        if r.get("status") in ("completed", "awaiting_verification", "dry_run_success")
    )
    outstanding_swept = sum(
        (r.get("outstanding_total", 0) or 0)
        for r in processing_results
        if r.get("has_outstanding") and r.get("status") in ("completed", "awaiting_verification", "dry_run_success")
    )

    balance_sync_count = 0
    balance_sync_errors = []

    if not dry_run and (completed or verified_count > 0):
        try:
            headers_qbo = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
            db = wmill.get_resource("u/carter/supabase")
            conn_sync = psycopg2.connect(
                host=db["host"], port=db["port"], dbname=db["dbname"],
                user=db["user"], password=db["password"]
            )
            cur_sync = conn_sync.cursor()

            all_charged_invoice_ids = []
            for r in processing_results:
                if r.get("status") in ("completed", "awaiting_verification", "payment_created", "charge_success"):
                    txn_id = r.get("transaction_id")
                    if txn_id:
                        cur_sync.execute(
                            "SELECT qbo_invoice_ids FROM billing.autopay_transactions WHERE id = %s::uuid",
                            (txn_id,)
                        )
                        row = cur_sync.fetchone()
                        if row and row[0]:
                            all_charged_invoice_ids.extend(row[0])

            for inv_id in all_charged_invoice_ids:
                try:
                    inv_resp = requests.get(
                        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/invoice/{inv_id}",
                        headers=headers_qbo, timeout=10
                    )
                    if inv_resp.ok:
                        inv_data = inv_resp.json().get("Invoice", {})
                        new_balance = float(inv_data.get("Balance", 0))
                        qbo_inv_id = inv_data.get("Id")

                        cur_sync.execute("""
                            UPDATE billing_audit.maintenance_invoices
                            SET balance_due = %s, balance_synced_at = now()
                            WHERE qbo_invoice_id = %s
                        """, (new_balance, str(qbo_inv_id)))
                        balance_sync_count += 1
                except Exception as e:
                    balance_sync_errors.append(f"Invoice {inv_id}: {str(e)[:100]}")

            conn_sync.commit()
            conn_sync.close()
        except Exception as e:
            balance_sync_errors.append(f"Sync setup error: {str(e)[:200]}")

    if billing_run_id:
        try:
            db = wmill.get_resource("u/carter/supabase")
            conn = psycopg2.connect(
                host=db["host"], port=db["port"], dbname=db["dbname"],
                user=db["user"], password=db["password"]
            )
            cur = conn.cursor()
            run_status = "autopay_complete" if not dry_run else "pending"
            notes = (
                f"Credits: {credits_result.get('total_maint_payments_found', 0)} found, "
                f"{credits_result.get('would_apply', 0)} applied. "
                f"Verified: {verified_count}. Emails: {emails_sent_count}. "
                f"Decline emails: {decline_emails_sent}. Reviews: {review_count}. "
                f"Declines: {len(charge_declined)}. API failures: {len(charge_api_failures)}. "
                f"Outstanding swept: ${outstanding_swept:.2f}. "
                f"Balance sync: {balance_sync_count} invoices updated. "
                f"Errors: {len(errors)}."
            )
            cur.execute("""
                UPDATE billing.billing_runs
                SET status = %s,
                    autopay_customers_total = %s,
                    autopay_charged_ok = %s,
                    autopay_charged_amount = %s,
                    autopay_failed = %s,
                    autopay_no_invoice = %s,
                    autopay_no_payment_method = %s,
                    completed_at = now(),
                    updated_at = now(),
                    notes = %s
                WHERE id = %s::uuid
            """, (
                run_status,
                len(processing_results),
                len(completed) + len(dry_runs),
                total_charged,
                len(charge_declined) + len(charge_api_failures) + len(payment_failed) + len(errors),
                len(no_invoice),
                len(no_payment_method),
                dry_run,
                notes,
                billing_run_id
            ))
            conn.commit()
            conn.close()
        except:
            pass

    return {
        "billing_month": billing_month,
        "dry_run": dry_run,
        "summary": {
            "total_processed": len(processing_results),
            "completed": len(completed),
            "dry_run_success": len(dry_runs),
            "no_invoice": len(no_invoice),
            "no_payment_method": len(no_payment_method),
            "charge_declined": len(charge_declined),
            "charge_api_failures": len(charge_api_failures),
            "payment_failed": len(payment_failed),
            "errors": len(errors),
            "total_amount": total_charged,
            "outstanding_swept": outstanding_swept,
            "verified": verified_count,
            "emails_sent": emails_sent_count,
            "decline_emails_sent": decline_emails_sent,
            "needs_review": review_count
        },
        "balance_sync": {
            "invoices_synced": balance_sync_count,
            "errors": balance_sync_errors[:5]
        },
        "credits_applied": {
            "total_found": credits_result.get("total_maint_payments_found", 0),
            "applied": credits_result.get("would_apply", 0)
        },
        "skipped_already_processed": customers_result.get("skipped_already_processed", 0),
        "details": {
            "completed": completed,
            "dry_run_success": dry_runs,
            "no_invoice": no_invoice,
            "no_payment_method": no_payment_method,
            "charge_declined": charge_declined,
            "charge_api_failures": charge_api_failures,
            "payment_failed": payment_failed,
            "errors": errors
        }
    }
