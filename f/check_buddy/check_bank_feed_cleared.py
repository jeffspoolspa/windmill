#extra_requirements:
#requests
#psycopg2-binary

import requests
import wmill
import psycopg2
import json
from datetime import datetime, timedelta, timezone


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


def get_db_conn():
    """Get psycopg2 connection using Windmill resource."""
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
    return conn


def read_qbo_deposit(base_url: str, headers: dict, qbo_deposit_id: str) -> dict:
    """Read a full QBO Deposit by ID. Returns deposit details with all line items."""
    response = requests.get(
        f"{base_url}/deposit/{qbo_deposit_id}",
        headers=headers,
    )
    
    if response.status_code in (400, 404):
        return {"exists": False, "error": f"Deposit {qbo_deposit_id} not found"}
    
    if not response.ok:
        raise Exception(f"QBO read failed: {response.status_code} - {response.text}")
    
    data = response.json()
    deposit = data.get("Deposit", {})
    
    if not deposit:
        return {"exists": False, "error": "No deposit in response"}
    
    deposit_total = float(deposit.get("TotalAmt", 0))
    deposit_account = deposit.get("DepositToAccountRef", {}).get("name", "")
    
    lines = []
    for line in deposit.get("Line", []):
        line_amount = float(line.get("Amount", 0))
        detail_type = line.get("DetailType", "")
        description = line.get("Description", "")
        
        linked_txn_type = None
        linked_txn_id = None
        linked_txns = line.get("LinkedTxn", [])
        if linked_txns:
            linked_txn_type = linked_txns[0].get("TxnType", None)
            linked_txn_id = linked_txns[0].get("TxnId", None)
        
        detail = {}
        if detail_type == "DepositLineDetail":
            dld = line.get("DepositLineDetail", {})
            detail = {
                "account_name": dld.get("AccountRef", {}).get("name", ""),
                "account_id": dld.get("AccountRef", {}).get("value", ""),
            }
        
        lines.append({
            "line_amount": line_amount,
            "linked_txn_type": linked_txn_type,
            "linked_txn_id": linked_txn_id,
            "detail_type": detail_type,
            "description": description,
            "detail": detail,
        })
    
    return {
        "exists": True,
        "deposit_id": deposit.get("Id", ""),
        "deposit_date": deposit.get("TxnDate", ""),
        "deposit_total": deposit_total,
        "deposit_account": deposit_account,
        "line_count": len(lines),
        "lines": lines,
    }


def get_cleared_deposit_ids(base_url: str, headers: dict, start_date: str, end_date: str) -> set:
    """
    Query QBO TransactionList report for cleared Deposit transactions.
    Returns a set of QBO Deposit IDs that have been cleared in the bank feed.
    """
    params = {
        "start_date": start_date,
        "end_date": end_date,
        "cleared": "Cleared",
        "transaction_type": "Deposit",
        "columns": "tx_date,txn_type,doc_num,memo,subt_nat_amount,account_name",
    }
    
    response = requests.get(
        f"{base_url}/reports/TransactionList",
        params=params,
        headers={
            "Authorization": headers["Authorization"],
            "Accept": "application/json",
        },
    )
    
    if not response.ok:
        raise Exception(f"TransactionList report failed: {response.status_code} - {response.text}")
    
    report = response.json()
    cleared_ids = set()
    
    rows = report.get("Rows", {}).get("Row", [])
    for row in rows:
        col_data = row.get("ColData", [])
        if len(col_data) >= 2:
            txn_type_col = col_data[1]
            deposit_id = txn_type_col.get("id", "")
            if deposit_id:
                cleared_ids.add(str(deposit_id))
    
    return cleared_ids


def validate_deposit_lines(qbo_deposit: dict, payments: list, retail_checks: list, cash_entries: list) -> dict:
    """
    Top-down validation: match QBO deposit line items to our records.
    Returns reconciliation_details JSON.
    """
    now = datetime.now(timezone.utc).isoformat()
    qbo_lines = qbo_deposit.get("lines", [])
    
    payment_by_qbo_id = {}
    for p in payments:
        if p.get("qbo_txn_id"):
            payment_by_qbo_id[p["qbo_txn_id"]] = p
    
    unmatched_payments = set(str(p["id"]) for p in payments)
    unmatched_retail = set(str(rc["id"]) for rc in retail_checks)
    unmatched_cash = set(str(ce["id"]) for ce in cash_entries)
    
    matched_lines = []
    unmatched_qbo_lines = []
    matched_details = []
    amount_mismatch_warnings = []
    
    for qbo_line in qbo_lines:
        line_amount = qbo_line.get("line_amount", 0)
        linked_txn_id = qbo_line.get("linked_txn_id")
        matched = False
        
        # Strategy 1: Auto-match voided $0 lines
        if abs(line_amount) < 0.01 and "void" in (qbo_line.get("description") or "").lower():
            matched_lines.append(qbo_line)
            matched_details.append({"match_type": "voided", "qbo_line_amount": line_amount})
            continue
        
        # Strategy 2: Match by linked QBO txn ID (for Payment lines)
        if linked_txn_id and linked_txn_id in payment_by_qbo_id:
            pmt = payment_by_qbo_id[linked_txn_id]
            pmt_id_str = str(pmt["id"])
            if pmt_id_str in unmatched_payments:
                unmatched_payments.discard(pmt_id_str)
                matched_lines.append(qbo_line)
                pmt_amount = float(pmt.get("amount", 0))
                if abs(pmt_amount - line_amount) > 0.01:
                    amount_mismatch_warnings.append({
                        "qbo_line_amount": line_amount,
                        "matched_amount": pmt_amount,
                        "difference": round(line_amount - pmt_amount, 2),
                    })
                matched_details.append({
                    "match_type": "payment_id",
                    "qbo_line_amount": line_amount,
                    "matched_amount": pmt_amount,
                    "matched_payment_id": pmt_id_str,
                })
                matched = True
                continue
        
        # Strategy 3: Match retail checks by amount
        if not matched:
            for rc in retail_checks:
                rc_id_str = str(rc["id"])
                if rc_id_str in unmatched_retail:
                    rc_amount = float(rc.get("check_amount", 0))
                    if abs(rc_amount - line_amount) < 0.01:
                        unmatched_retail.discard(rc_id_str)
                        matched_lines.append(qbo_line)
                        matched_details.append({
                            "match_type": "amount_check",
                            "qbo_line_amount": line_amount,
                            "matched_check_id": rc_id_str,
                        })
                        matched = True
                        break
        
        # Strategy 4: Match cash entries by amount
        if not matched:
            for ce in cash_entries:
                ce_id_str = str(ce["id"])
                if ce_id_str in unmatched_cash:
                    ce_amount = float(ce.get("amount", 0))
                    if abs(ce_amount - line_amount) < 0.01:
                        unmatched_cash.discard(ce_id_str)
                        matched_lines.append(qbo_line)
                        matched_details.append({
                            "match_type": "amount_cash",
                            "qbo_line_amount": line_amount,
                            "matched_cash_entry_id": ce_id_str,
                        })
                        matched = True
                        break
        
        if not matched:
            unmatched_qbo_lines.append(qbo_line)
    
    # Build unmatched our items
    unmatched_our_items = []
    for pid in unmatched_payments:
        pmt = next(p for p in payments if str(p["id"]) == pid)
        unmatched_our_items.append({"type": "payment", "id": pid, "amount": float(pmt.get("amount", 0))})
    for rcid in unmatched_retail:
        rc = next(c for c in retail_checks if str(c["id"]) == rcid)
        unmatched_our_items.append({"type": "check", "id": rcid, "amount": float(rc.get("check_amount", 0))})
    for ceid in unmatched_cash:
        ce = next(c for c in cash_entries if str(c["id"]) == ceid)
        unmatched_our_items.append({"type": "cash", "id": ceid, "amount": float(ce.get("amount", 0))})
    
    our_total = (
        sum(float(p.get("amount", 0)) for p in payments)
        + sum(float(rc.get("check_amount", 0)) for rc in retail_checks)
        + sum(float(ce.get("amount", 0)) for ce in cash_entries)
    )
    
    qbo_total = qbo_deposit.get("deposit_total", 0)
    amount_match = abs(qbo_total - our_total) < 0.01
    
    fully_reconciled = (
        len(unmatched_qbo_lines) == 0
        and len(unmatched_our_items) == 0
        and amount_match
    )
    
    return {
        "fully_reconciled": fully_reconciled,
        "reconciliation_details": {
            "qbo_total": qbo_total,
            "our_total": round(our_total, 2),
            "qbo_deposit_date": qbo_deposit.get("deposit_date"),
            "qbo_account": qbo_deposit.get("deposit_account"),
            "qbo_line_count": qbo_deposit.get("line_count", 0),
            "our_payment_count": len(payments),
            "our_check_count": len(retail_checks),
            "our_cash_count": len(cash_entries),
            "matched_lines": len(matched_lines),
            "unmatched_lines": len(unmatched_qbo_lines),
            "unmatched_details": unmatched_qbo_lines,
            "matched_details": matched_details,
            "unmatched_our_items": unmatched_our_items,
            "amount_mismatch_warnings": amount_mismatch_warnings,
            "amount_match": amount_match,
            "checked_at": now,
        }
    }


def main(
    deposit_ids: list = None,
) -> dict:
    """
    Check if API-created QBO deposits have been cleared in the bank feed.
    For cleared deposits, run top-down validation and mark as reconciled if everything matches.
    """
    
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db_conn()
    cur = conn.cursor()
    
    # Step 1: Get deposits to check
    if deposit_ids:
        # Convert to strings for uuid cast
        str_ids = [str(d) for d in deposit_ids]
        cur.execute("SELECT * FROM app_checks.deposits WHERE id = ANY(%s::uuid[])", (str_ids,))
    else:
        cur.execute("""
            SELECT * FROM app_checks.deposits
            WHERE deposit_source = 'api'
              AND qbo_deposit_id IS NOT NULL
              AND bank_feed_cleared = false
        """)
    
    if cur.description:
        dep_cols = [desc[0] for desc in cur.description]
        deposits = [dict(zip(dep_cols, row)) for row in cur.fetchall()]
    else:
        deposits = []
    
    if not deposits:
        cur.close()
        conn.close()
        return {"checked": 0, "newly_cleared": 0, "newly_reconciled": 0, "errors": [], "message": "No deposits to check"}
    
    # Step 2: Refresh QBO token
    access_token, realm_id = refresh_qbo_token()
    base_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    # Step 3: Get cleared deposit IDs from TransactionList report
    deposit_dates = []
    for d in deposits:
        if d.get("deposited_at"):
            try:
                dt_str = str(d["deposited_at"])
                if "+" in dt_str or dt_str.endswith("Z"):
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                else:
                    dt = datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)
                deposit_dates.append(dt)
            except:
                pass
    
    if deposit_dates:
        earliest = min(deposit_dates) - timedelta(days=7)
    else:
        earliest = datetime.now(timezone.utc) - timedelta(days=90)
    
    start_date = earliest.strftime("%Y-%m-%d")
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    cleared_ids = get_cleared_deposit_ids(base_url, headers, start_date, end_date)
    
    # Step 4: Process each deposit
    newly_cleared = 0
    newly_reconciled = 0
    errors = []
    results = []
    
    for deposit in deposits:
        deposit_id = str(deposit["id"])
        qbo_deposit_id = deposit.get("qbo_deposit_id")
        
        if not qbo_deposit_id:
            continue
        
        is_cleared = str(qbo_deposit_id) in cleared_ids
        
        if not is_cleared:
            results.append({"deposit_id": deposit_id, "qbo_deposit_id": qbo_deposit_id, "cleared": False, "reconciled": False})
            continue
        
        newly_cleared += 1
        
        try:
            # Mark bank feed cleared
            cur.execute("""
                UPDATE app_checks.deposits SET
                    bank_feed_cleared = true, bank_feed_cleared_at = %s, updated_at = %s
                WHERE id = %s::uuid
            """, (now, now, deposit_id))
            
            # Run top-down validation
            qbo_deposit = read_qbo_deposit(base_url, headers, qbo_deposit_id)
            
            if not qbo_deposit.get("exists"):
                cur.execute("""
                    UPDATE app_checks.deposits SET
                        qbo_deposit_id = NULL, qbo_deposit_created_at = NULL,
                        bank_feed_cleared = false, bank_feed_cleared_at = NULL,
                        reconciliation_details = NULL, updated_at = %s
                    WHERE id = %s::uuid
                """, (now, deposit_id))
                errors.append({"deposit_id": deposit_id, "error": f"QBO deposit {qbo_deposit_id} no longer exists"})
                continue
            
            # Get our records for this deposit
            cur.execute("SELECT * FROM app_checks.scanned_checks WHERE deposit_id = %s::uuid", (deposit_id,))
            check_cols = [desc[0] for desc in cur.description]
            checks = [dict(zip(check_cols, row)) for row in cur.fetchall()]
            check_ids = [str(c["id"]) for c in checks]
            
            payments = []
            if check_ids:
                cur.execute("""
                    SELECT * FROM app_checks.check_payments
                    WHERE check_id = ANY(%s::uuid[]) AND qbo_entity_type = 'Payment' AND qbo_txn_id IS NOT NULL
                """, (check_ids,))
                pmt_cols = [desc[0] for desc in cur.description]
                payments = [dict(zip(pmt_cols, row)) for row in cur.fetchall()]
            
            retail_checks = [c for c in checks if c.get("is_retail")]
            
            cur.execute("SELECT * FROM app_checks.cash_entries WHERE deposit_id = %s::uuid", (deposit_id,))
            ce_cols = [desc[0] for desc in cur.description]
            cash_entries = [dict(zip(ce_cols, row)) for row in cur.fetchall()]
            
            # Run validation
            validation = validate_deposit_lines(qbo_deposit, payments, retail_checks, cash_entries)
            recon_details = validation["reconciliation_details"]
            fully_reconciled = validation["fully_reconciled"]
            
            recon_details_json = json.dumps(recon_details)
            
            if fully_reconciled:
                newly_reconciled += 1
                total_items = len(payments) + len(retail_checks) + len(cash_entries)
                
                cur.execute("""
                    UPDATE app_checks.deposits SET
                        status = 'reconciled', reconciled_at = %s,
                        reconciled_count = %s, unreconciled_count = 0,
                        reconciliation_details = %s, last_reconciliation_check = %s, updated_at = %s
                    WHERE id = %s::uuid
                """, (now, total_items, recon_details_json, now, now, deposit_id))
                
                if payments:
                    payment_ids = [str(p["id"]) for p in payments]
                    cur.execute("""
                        UPDATE app_checks.check_payments SET reconciled_at = %s, updated_at = %s
                        WHERE id = ANY(%s::uuid[])
                    """, (now, now, payment_ids))
                
                if check_ids:
                    cur.execute("""
                        UPDATE app_checks.scanned_checks SET
                            status = 'reconciled', reconciled_at = %s, updated_at = %s
                        WHERE id = ANY(%s::uuid[])
                    """, (now, now, check_ids))
                
                if cash_entries:
                    cash_ids = [str(ce["id"]) for ce in cash_entries]
                    cur.execute("""
                        UPDATE app_checks.cash_entries SET reconciled_at = %s, updated_at = %s
                        WHERE id = ANY(%s::uuid[])
                    """, (now, now, cash_ids))
            else:
                matched = recon_details.get("matched_lines", 0)
                total_items = len(payments) + len(retail_checks) + len(cash_entries)
                
                cur.execute("""
                    UPDATE app_checks.deposits SET
                        reconciled_count = %s, unreconciled_count = %s,
                        reconciliation_details = %s, last_reconciliation_check = %s, updated_at = %s
                    WHERE id = %s::uuid
                """, (matched, total_items - matched, recon_details_json, now, now, deposit_id))
            
            results.append({
                "deposit_id": deposit_id,
                "qbo_deposit_id": qbo_deposit_id,
                "cleared": True,
                "reconciled": fully_reconciled,
                "matched_lines": recon_details.get("matched_lines", 0),
                "unmatched_lines": recon_details.get("unmatched_lines", 0),
                "amount_match": recon_details.get("amount_match", False),
            })
            
        except Exception as e:
            errors.append({"deposit_id": deposit_id, "error": str(e)})
    
    cur.close()
    conn.close()
    
    return {
        "checked": len(deposits),
        "newly_cleared": newly_cleared,
        "newly_reconciled": newly_reconciled,
        "results": results,
        "errors": errors,
    }
