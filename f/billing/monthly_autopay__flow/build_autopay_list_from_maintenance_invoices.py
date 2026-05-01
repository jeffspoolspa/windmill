import psycopg2
import wmill
from datetime import datetime

def main(
    billing_month: str,
    billing_run_id: str,
    dry_run: bool = True,
    test_mode: bool = False,
    test_qbo_customer_id: str = None
):
    """
    Invoice-driven autopay list builder.
    Pulls ALL unpaid maintenance invoices (current + prior months) for each
    autopay customer.  Only maintenance invoices are included (sourced from
    billing_audit.maintenance_invoices).
    """
    month_name = datetime.strptime(billing_month, "%Y-%m").strftime("%B %Y")
    billing_month_date = f"{billing_month}-01"

    db = wmill.get_resource("u/carter/supabase")
    conn = psycopg2.connect(
        host=db["host"], port=db["port"], dbname=db["dbname"],
        user=db["user"], password=db["password"]
    )

    TERMINAL_STATUSES = (
        'charge_success', 'payment_created', 'awaiting_verification',
        'completed', 'verified', 'needs_review'
    )

    try:
        cur = conn.cursor()

        # Pull ALL unpaid maintenance invoices across every billing month
        base_query = """
            SELECT mi.qbo_customer_id, mi.customer_name,
                ac.payment_method, ac.card_type, ac.last_four, ac.email,
                ac.payment_status, ac.consecutive_declines,
                mi.qbo_invoice_id, mi.doc_number, mi.invoice_total, mi.balance_due,
                mi.billing_month
            FROM billing_audit.maintenance_invoices mi
            JOIN billing.autopay_customers ac ON mi.qbo_customer_id = ac.qbo_customer_id
            WHERE COALESCE(mi.balance_due, mi.invoice_total) > 0
        """

        if test_mode and test_qbo_customer_id:
            cur.execute(base_query + " AND mi.qbo_customer_id = %s ORDER BY mi.billing_month, mi.customer_name",
                       (str(test_qbo_customer_id),))
        else:
            cur.execute(base_query + " ORDER BY mi.billing_month, mi.customer_name")

        rows = cur.fetchall()

        customer_map = {}
        for row in rows:
            qbo_id = row[0]
            if qbo_id not in customer_map:
                customer_map[qbo_id] = {
                    "qbo_customer_id": qbo_id, "name": row[1],
                    "payment_method": row[2], "card_type": row[3],
                    "last_four": row[4], "email": row[5],
                    "payment_status": row[6], "consecutive_declines": row[7],
                    "maint_invoices": []
                }
            inv_billing_month = str(row[12])  # e.g. '2026-02-01'
            inv_month_str = inv_billing_month[:7]  # e.g. '2026-02'
            customer_map[qbo_id]["maint_invoices"].append({
                "qbo_invoice_id": row[8], "doc_number": row[9],
                "invoice_total": float(row[10]) if row[10] else 0,
                "balance_due": float(row[11]) if row[11] else 0,
                "billing_month": inv_month_str
            })

        customers = []
        skipped_terminal = []

        for qbo_id, cust in customer_map.items():
            cur.execute("""
                SELECT id, status FROM billing.autopay_transactions
                WHERE qbo_customer_id = %s AND billing_month = %s
            """, (qbo_id, billing_month))
            existing = cur.fetchone()

            if existing:
                existing_id, existing_status = str(existing[0]), existing[1]
                if existing_status in TERMINAL_STATUSES:
                    skipped_terminal.append({
                        "name": cust["name"], "qbo_customer_id": qbo_id,
                        "existing_status": existing_status
                    })
                    continue
                else:
                    cur.execute("""
                        UPDATE billing.autopay_transactions
                        SET status = 'pending', dry_run = %s, billing_run_id = %s,
                            error_step = NULL, error_message = NULL, charge_error = NULL,
                            updated_at = now()
                        WHERE id = %s::uuid RETURNING id
                    """, (dry_run, billing_run_id, existing_id))
                    txn_id = existing_id
            else:
                maint_total = sum(inv["balance_due"] for inv in cust["maint_invoices"])
                # Separate current month vs outstanding for tracking
                current_month_total = sum(inv["balance_due"] for inv in cust["maint_invoices"] if inv["billing_month"] == billing_month)
                outstanding_total = sum(inv["balance_due"] for inv in cust["maint_invoices"] if inv["billing_month"] != billing_month)
                outstanding_count = sum(1 for inv in cust["maint_invoices"] if inv["billing_month"] != billing_month)
                cur.execute("""
                    INSERT INTO billing.autopay_transactions
                    (billing_month, qbo_customer_id, customer_name, payment_method,
                     card_type, last_four, email_address, status, dry_run, billing_run_id,
                     maint_amount, outstanding_amount, outstanding_invoice_count, has_outstanding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (billing_month, qbo_id, cust["name"], cust["payment_method"],
                      cust["card_type"], cust["last_four"], cust["email"],
                      dry_run, billing_run_id, current_month_total,
                      outstanding_total, outstanding_count, outstanding_count > 0))
                txn_id = str(cur.fetchone()[0])

            customers.append({
                "qbo_customer_id": qbo_id, "name": cust["name"],
                "payment_method": cust["payment_method"], "card_type": cust["card_type"],
                "last_four": cust["last_four"], "email": cust["email"],
                "payment_status": cust["payment_status"],
                "consecutive_declines": cust["consecutive_declines"],
                "transaction_id": txn_id,
                "maint_invoices": cust["maint_invoices"]
            })

        conn.commit()
    finally:
        conn.close()

    good_count = len([c for c in customers if c["payment_status"] == "good"])
    issue_count = len([c for c in customers if c["payment_status"] != "good"])
    customers_with_outstanding = len([c for c in customers if any(inv["billing_month"] != billing_month for inv in c["maint_invoices"])])

    return {
        "billing_month": billing_month, "month_display": month_name,
        "test_mode": test_mode, "total_customers": len(customers),
        "good_standing": good_count, "payment_issue_customers": issue_count,
        "customers_with_outstanding_maint": customers_with_outstanding,
        "skipped_already_processed": len(skipped_terminal),
        "skipped_terminal_details": skipped_terminal[:10],
        "customers": customers
    }
