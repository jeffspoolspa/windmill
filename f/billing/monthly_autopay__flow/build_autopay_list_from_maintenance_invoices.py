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
    ACTIVE autopay customer (billing.autopay_customers.is_active = true).
    The charge method is the one LINKED on the autopay record
    (autopay_customers.payment_method_id -> customer_payment_methods). It is
    NOT resolved at run time: whatever is linked is what gets charged, until it
    is changed manually. Customers with no linked method (payment_method_id IS
    NULL) fall through to a live QBO lookup in the charge step (module d1).
    """
    month_name = datetime.strptime(billing_month, "%Y-%m").strftime("%B %Y")

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

        base_query = """
            SELECT mi.qbo_customer_id, mi.customer_name,
                linked.qbo_payment_method_id, linked.type, linked.card_brand, linked.last_four,
                ac.email, ac.payment_status, ac.consecutive_declines,
                mi.qbo_invoice_id, mi.doc_number, mi.invoice_total, mi.balance_due,
                mi.billing_month, linked.id
            FROM billing_audit.maintenance_invoices mi
            JOIN billing.autopay_customers ac ON mi.qbo_customer_id = ac.qbo_customer_id
            LEFT JOIN billing.customer_payment_methods linked
                ON linked.id = ac.payment_method_id
            WHERE ac.is_active = true
              AND COALESCE(mi.balance_due, mi.invoice_total) > 0
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
                pm_qbo_id = row[2]
                pm_type = row[3]
                pm_kind = "ach" if pm_type == "ach" else "card"
                resolved_method = None
                if pm_qbo_id:
                    resolved_method = {
                        "pm_row_id": str(row[14]) if row[14] else None,
                        "qbo_payment_method_id": pm_qbo_id,
                        "kind": pm_kind,
                        "card_brand": row[4],
                        "last_four": row[5],
                    }
                customer_map[qbo_id] = {
                    "qbo_customer_id": qbo_id, "name": row[1],
                    "resolved_method": resolved_method,
                    "payment_method": (pm_kind if pm_qbo_id else None),
                    "card_type": row[4], "last_four": row[5], "email": row[6],
                    "payment_status": row[7], "consecutive_declines": row[8],
                    "maint_invoices": []
                }
            inv_billing_month = str(row[13])
            inv_month_str = inv_billing_month[:7]
            customer_map[qbo_id]["maint_invoices"].append({
                "qbo_invoice_id": row[9], "doc_number": row[10],
                "invoice_total": float(row[11]) if row[11] else 0,
                "balance_due": float(row[12]) if row[12] else 0,
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
                            payment_method = %s, card_type = %s, last_four = %s,
                            error_step = NULL, error_message = NULL, charge_error = NULL,
                            updated_at = now()
                        WHERE id = %s::uuid RETURNING id
                    """, (dry_run, billing_run_id, cust["payment_method"],
                          cust["card_type"], cust["last_four"], existing_id))
                    txn_id = existing_id
            else:
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
                "resolved_method": cust["resolved_method"],
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
    no_method_count = len([c for c in customers if not c["resolved_method"]])

    return {
        "billing_month": billing_month, "month_display": month_name,
        "test_mode": test_mode, "total_customers": len(customers),
        "good_standing": good_count, "payment_issue_customers": issue_count,
        "customers_with_outstanding_maint": customers_with_outstanding,
        "customers_without_linked_method": no_method_count,
        "skipped_already_processed": len(skipped_terminal),
        "skipped_terminal_details": skipped_terminal[:10],
        "customers": customers
    }
