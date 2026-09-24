#extra_requirements:
#requests
#psycopg2-binary

import requests
import wmill
import psycopg2
import json
from datetime import datetime, timedelta


def refresh_qbo_token() -> tuple[str, str]:
    """Refresh QBO token and return (access_token, realm_id)."""
    resource_path = "u/carter/quickbooks_api"
    resource = wmill.get_resource(resource_path)
    
    response = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": resource["refresh_token"]
        },
        auth=(resource["client_id"], resource["client_secret"])
    )
    
    if not response.ok:
        raise Exception(f"Token refresh failed: {response.status_code} - {response.text}")
    
    tokens = response.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(path=resource_path, value=resource)
    
    return tokens["access_token"], resource["realm_id"]


def is_object_not_found(response) -> bool:
    """True only for QBO's Object Not Found fault (code 610): the payment is really gone."""
    try:
        errors = response.json().get("Fault", {}).get("Error", [])
    except ValueError:
        return False
    return any(str(e.get("code")) == "610" for e in errors)


def read_qbo_payment(base_url: str, headers: dict, payment_id: str) -> dict:
    """Read a single QBO payment. Returns payment state or deleted indicator."""
    response = requests.get(
        f"{base_url}/payment/{payment_id}",
        headers=headers
    )
    
    # Only a 610 fault means deleted; any other 400 or odd reply raises, so the payment is skipped and logged.
    if is_object_not_found(response):
        return {"exists": False, "deleted": True, "payment_id": payment_id}
    
    if not response.ok:
        raise Exception(f"QBO API error for payment {payment_id}: {response.status_code} - {response.text}")
    
    result = response.json()
    payment_data = result.get("Payment", {})
    
    if not payment_data:
        raise Exception(f"QBO returned no Payment for {payment_id}: {response.text[:500]}")
    
    applied_invoices = []
    for line in payment_data.get("Line", []):
        for linked_txn in line.get("LinkedTxn", []):
            if linked_txn.get("TxnType") == "Invoice":
                applied_invoices.append({
                    "invoice_id": linked_txn.get("TxnId", ""),
                    "amount_applied": float(line.get("Amount", 0)),
                })
    
    for inv in applied_invoices:
        try:
            inv_resp = requests.get(
                f"{base_url}/invoice/{inv['invoice_id']}",
                headers=headers
            )
            if inv_resp.ok:
                inv_data = inv_resp.json().get("Invoice", {})
                inv["invoice_number"] = inv_data.get("DocNumber", "")
            else:
                inv["invoice_number"] = ""
        except Exception:
            inv["invoice_number"] = ""
    
    deposit_to_account = payment_data.get("DepositToAccountRef", {}).get("value", "")
    
    return {
        "exists": True,
        "deleted": False,
        "payment_id": payment_data.get("Id", ""),
        "total_amount": float(payment_data.get("TotalAmt", 0)),
        "payment_ref": payment_data.get("PaymentRefNum", ""),
        "txn_date": payment_data.get("TxnDate", ""),
        "customer_id": payment_data.get("CustomerRef", {}).get("value", ""),
        "customer_name": payment_data.get("CustomerRef", {}).get("name", ""),
        "unapplied_amount": float(payment_data.get("UnappliedAmt", 0)),
        "deposit_to_account": deposit_to_account,
        "applied_invoices": applied_invoices,
    }


def check_deposits_for_payments(base_url: str, headers: dict, payment_ids: list[str], lookback_days: int = 90) -> dict:
    """Check which payments are in QBO deposits. Returns payment_id -> {deposit_id, deposit_date}."""
    cutoff_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    page_size = 1000  # QBO's max; without paging QBO returns only the first 100
    deposits = []
    start = 1
    while True:
        query = f"SELECT * FROM Deposit WHERE TxnDate >= '{cutoff_date}' STARTPOSITION {start} MAXRESULTS {page_size}"
        response = requests.get(f"{base_url}/query", headers=headers, params={"query": query})
        if not response.ok:
            raise Exception(f"QBO deposit query failed: {response.status_code} - {response.text}")
        page = response.json().get("QueryResponse", {}).get("Deposit", [])
        deposits += page
        if len(page) < page_size:
            break
        start += page_size
    
    payment_to_deposit = {}
    for deposit in deposits:
        deposit_id = deposit.get("Id", "")
        deposit_date = deposit.get("TxnDate", "")
        for line in deposit.get("Line", []):
            for linked_txn in line.get("LinkedTxn", []):
                if linked_txn.get("TxnType") == "Payment":
                    txn_id = linked_txn.get("TxnId", "")
                    if txn_id in payment_ids:
                        payment_to_deposit[txn_id] = {
                            "deposit_id": deposit_id,
                            "deposit_date": deposit_date,
                        }
    
    return payment_to_deposit


def main(supabase: dict = None, deposited_lookback_days: int = 90) -> dict:
    """
    Daily sync of check_payments against QBO: all undeposited ones, plus deposited ones
    whose payment_date is within deposited_lookback_days (so a payment deleted or edited
    in QBO after it was deposited is still seen).
    Only syncs Payment-type entities (SalesReceipt/JournalEntry/Transfer are linked
    during reconciliation and don't need ongoing sync).
    Bottom-up deposit detection only applies to manual deposits.
    API deposits are handled by check_bank_feed_cleared on its own schedule.
    """
    
    if supabase is None:
        supabase = wmill.get_resource("u/carter/supabase")
    
    conn = psycopg2.connect(
        host=supabase.get("host"),
        port=supabase.get("port", 6543),
        dbname=supabase.get("dbname", "postgres"),
        user=supabase.get("user"),
        password=supabase.get("password"),
        sslmode=supabase.get("sslmode", "require"),
    )
    conn.autocommit = True
    cur = conn.cursor()
    
    # Step 1: Query unreconciled Payment-type check_payments with deposit_source
    cur.execute("""
        SELECT cp.id, cp.check_id, cp.qbo_txn_id, cp.amount, cp.qbo_customer_id, COALESCE(d.deposit_source, 'manual') as deposit_source,
               cp.qbo_deposit_id
        FROM app_checks.check_payments cp
        LEFT JOIN app_checks.scanned_checks sc ON sc.id = cp.check_id
        LEFT JOIN app_checks.deposits d ON d.id = sc.deposit_id
        WHERE cp.qbo_txn_id IS NOT NULL
          AND (cp.qbo_deposit_id IS NULL
               OR cp.payment_date::date >= CURRENT_DATE - %s)
          AND cp.qbo_entity_type = 'Payment'
        ORDER BY cp.created_at ASC
    """, (deposited_lookback_days,))
    payments_to_sync = cur.fetchall()
    
    if not payments_to_sync:
        cur.close()
        conn.close()
        return {
            "total_processed": 0,
            "unchanged": 0,
            "updated": 0,
            "deleted": 0,
            "reconciled": 0,
            "errors": 0,
            "details": [],
        }
    
    access_token, realm_id = refresh_qbo_token()
    base_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    results = {
        "total_processed": len(payments_to_sync),
        "unchanged": 0,
        "updated": 0,
        "deleted": 0,
        "reconciled": 0,
        "errors": 0,
        "details": [],
    }
    
    qbo_states = {}
    existing_payment_ids = []
    manual_existing_payment_ids = []
    for row in payments_to_sync:
        cp_id, check_id, qbo_txn_id, local_amount, qbo_customer_id, deposit_source, local_deposit_id = row
        try:
            qbo_state = read_qbo_payment(base_url, headers, qbo_txn_id)
            qbo_states[cp_id] = {
                "cp_id": cp_id,
                "check_id": check_id,
                "qbo_txn_id": qbo_txn_id,
                "local_amount": float(local_amount),
                "qbo_customer_id": qbo_customer_id,
                "deposit_source": deposit_source,
                "local_deposit_id": local_deposit_id,
                "qbo_state": qbo_state,
            }
            if qbo_state.get("exists"):
                existing_payment_ids.append(qbo_txn_id)
                # Only track manual deposit payments for bottom-up deposit detection
                if deposit_source != 'api':
                    manual_existing_payment_ids.append(qbo_txn_id)
        except Exception as e:
            results["errors"] += 1
            results["details"].append(f"Error reading payment {qbo_txn_id}: {str(e)}")
    
    # Step 4: Bottom-up deposit detection - ONLY for manual deposit payments
    deposit_map = {}
    if manual_existing_payment_ids:
        try:
            deposit_map = check_deposits_for_payments(base_url, headers, manual_existing_payment_ids)
        except Exception as e:
            results["details"].append(f"Error checking deposits: {str(e)}")
    
    now = datetime.utcnow().isoformat() + "Z"
    affected_deposits = set()
    
    for cp_id, info in qbo_states.items():
        check_id = info["check_id"]
        qbo_txn_id = info["qbo_txn_id"]
        local_amount = info["local_amount"]
        qbo_customer_id = info["qbo_customer_id"]
        qbo_state = info["qbo_state"]
        local_deposit_id = info["local_deposit_id"]
        
        try:
            if not qbo_state.get("exists") or qbo_state.get("deleted"):
                # Read the deposit link before the check leaves it (autocommit: each statement commits on its own).
                cur.execute("SELECT deposit_id FROM app_checks.scanned_checks WHERE id = %s", (check_id,))
                dep_row = cur.fetchone()
                if dep_row and dep_row[0]:
                    affected_deposits.add(dep_row[0])

                cur.execute(
                    "SELECT COUNT(*) FROM app_checks.check_payments WHERE check_id = %s AND id != %s",
                    (check_id, cp_id),
                )
                remaining = cur.fetchone()[0]

                # Revert the check before deleting its last payment, both in one transaction.
                # psycopg2 only opens a transaction with autocommit off; `with conn` commits or rolls back.
                conn.autocommit = False
                try:
                    with conn:
                        if remaining == 0:
                            # QBO only lets a payment be deleted once it is off its deposit, so the check leaves ours too.
                            # Clearing deposit_id here is what lets guard_check_status_downgrade allow status='review'.
                            cur.execute("""
                                UPDATE app_checks.scanned_checks
                                SET status = 'review', processed_at = NULL,
                                    deposit_id = NULL, qbo_payment_id = NULL,
                                    validation_flags = CASE
                                        WHEN COALESCE(validation_flags, '[]'::jsonb) ? 'payment_deleted'
                                            THEN validation_flags
                                        ELSE COALESCE(validation_flags, '[]'::jsonb) || '["payment_deleted"]'::jsonb
                                    END,
                                    updated_at = %s
                                WHERE id = %s
                            """, (now, check_id))
                        cur.execute("DELETE FROM app_checks.check_payments WHERE id = %s", (cp_id,))
                finally:
                    conn.autocommit = True

                results["deleted"] += 1
                results["details"].append(f"Payment {qbo_txn_id} deleted in QBO, check_payment {cp_id} removed")
                continue
            
            actions = []
            
            qbo_amount = qbo_state.get("total_amount", 0)
            if abs(qbo_amount - local_amount) > 0.01:
                cur.execute("""
                    UPDATE app_checks.check_payments 
                    SET amount = %s, updated_at = %s
                    WHERE id = %s
                """, (qbo_amount, now, cp_id))
                
                cur.execute("""
                    SELECT SUM(cp.amount), sc.check_amount
                    FROM app_checks.check_payments cp
                    JOIN app_checks.scanned_checks sc ON sc.id = cp.check_id
                    WHERE cp.check_id = %s
                    GROUP BY sc.check_amount
                """, (check_id,))
                totals = cur.fetchone()
                if totals:
                    total_payments = float(totals[0]) - local_amount + qbo_amount
                    check_amount = float(totals[1]) if totals[1] else 0
                    if abs(total_payments - check_amount) > 0.01:
                        cur.execute("""
                            UPDATE app_checks.scanned_checks
                            SET validation_flags = COALESCE(validation_flags, '[]'::jsonb) || '["amount_mismatch"]'::jsonb,
                                updated_at = %s
                            WHERE id = %s AND NOT (COALESCE(validation_flags, '[]'::jsonb) ? 'amount_mismatch')
                        """, (now, check_id))
                
                actions.append("amount_updated")
            
            qbo_invoices = qbo_state.get("applied_invoices", [])
            cur.execute("""
                SELECT qbo_invoice_id, amount_applied
                FROM app_checks.check_invoices
                WHERE check_payment_id = %s
            """, (cp_id,))
            local_invoices = cur.fetchall()
            
            qbo_inv_set = {(i["invoice_id"], round(i["amount_applied"], 2)) for i in qbo_invoices}
            local_inv_set = {(row[0], round(float(row[1] or 0), 2)) for row in local_invoices}
            
            if qbo_inv_set != local_inv_set:
                cur.execute("DELETE FROM app_checks.check_invoices WHERE check_payment_id = %s", (cp_id,))
                for inv in qbo_invoices:
                    cur.execute("""
                        INSERT INTO app_checks.check_invoices 
                        (check_id, check_payment_id, qbo_invoice_id, qbo_invoice_number, amount_applied, qbo_customer_id)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (check_id, cp_id, inv["invoice_id"], inv.get("invoice_number", ""), inv["amount_applied"], qbo_customer_id))
                actions.append("invoices_rebuilt")
            
            # Already-deposited payments are found again every run; only act when QBO shows a new deposit.
            if qbo_txn_id in deposit_map and deposit_map[qbo_txn_id]["deposit_id"] != str(local_deposit_id or ""):
                dep_info = deposit_map[qbo_txn_id]
                cur.execute("""
                    UPDATE app_checks.check_payments
                    SET qbo_deposit_id = %s, qbo_deposit_date = %s, reconciled_at = %s, updated_at = %s
                    WHERE id = %s
                """, (dep_info["deposit_id"], dep_info["deposit_date"], now, now, cp_id))
                
                cur.execute("""
                    SELECT COUNT(*) FROM app_checks.check_payments 
                    WHERE check_id = %s AND qbo_deposit_id IS NULL
                """, (check_id,))
                unreconciled = cur.fetchone()[0]
                
                if unreconciled == 0:
                    cur.execute("""
                        UPDATE app_checks.scanned_checks
                        SET status = 'reconciled', reconciled_at = %s, updated_at = %s
                        WHERE id = %s AND status = 'deposited'
                    """, (now, now, check_id))
                
                cur.execute("SELECT deposit_id FROM app_checks.scanned_checks WHERE id = %s", (check_id,))
                dep_row = cur.fetchone()
                if dep_row and dep_row[0]:
                    affected_deposits.add(dep_row[0])
                
                actions.append("reconciled")
                results["reconciled"] += 1
            
            if actions:
                sync_status = "amount_mismatch" if "amount_updated" in actions else (
                    "invoices_changed" if "invoices_rebuilt" in actions else "synced"
                )
                results["updated"] += 1
                results["details"].append(f"Payment {qbo_txn_id}: {', '.join(actions)}")
            else:
                sync_status = "synced"
                results["unchanged"] += 1
            
            cur.execute("""
                UPDATE app_checks.check_payments
                SET sync_status = %s, last_synced_at = %s, updated_at = %s
                WHERE id = %s
            """, (sync_status, now, now, cp_id))
        
        except Exception as e:
            results["errors"] += 1
            results["details"].append(f"Error processing payment {qbo_txn_id}: {str(e)}")
    
    for deposit_id in affected_deposits:
        try:
            cur.execute("""
                SELECT 
                    COUNT(*) FILTER (WHERE cp.qbo_deposit_id IS NOT NULL) as reconciled_count,
                    COUNT(*) FILTER (WHERE cp.qbo_deposit_id IS NULL) as unreconciled_count
                FROM app_checks.scanned_checks sc
                JOIN app_checks.check_payments cp ON cp.check_id = sc.id
                WHERE sc.deposit_id = %s
            """, (deposit_id,))
            counts = cur.fetchone()
            
            if counts:
                reconciled_count, unreconciled_count = counts
                update_data = {
                    "reconciled_count": reconciled_count,
                    "unreconciled_count": unreconciled_count,
                    "last_reconciliation_check": now,
                }
                
                # A deleted payment can pull its check off this deposit; keep totals in step (same math as the client).
                cur.execute("""
                    SELECT COUNT(*), COALESCE(SUM(check_amount), 0)
                        + (SELECT COALESCE(SUM(amount), 0) FROM app_checks.cash_entries WHERE deposit_id = %s)
                    FROM app_checks.scanned_checks WHERE deposit_id = %s
                """, (deposit_id, deposit_id))
                update_data["check_count"], update_data["total_amount"] = cur.fetchone()

                if unreconciled_count == 0 and reconciled_count > 0:
                    update_data["status"] = "reconciled"
                    update_data["reconciled_at"] = now
                
                set_clause = ", ".join(f"{k} = %s" for k in update_data.keys())
                cur.execute(
                    f"UPDATE app_checks.deposits SET {set_clause}, updated_at = %s WHERE id = %s",
                    (*update_data.values(), now, deposit_id)
                )
        except Exception as e:
            results["details"].append(f"Error updating deposit {deposit_id}: {str(e)}")
    
    cur.close()
    conn.close()
    
    return results
