#extra_requirements:
#requests
#psycopg2-binary

import requests
import wmill
import psycopg2
import json
from datetime import datetime, timezone


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


def read_qbo_payment(base_url: str, headers: dict, payment_id: str) -> dict:
    """Read a payment from QBO. Returns payment state or deleted indicator."""
    try:
        response = requests.get(
            f"{base_url}/payment/{payment_id}",
            headers=headers,
        )
        if response.status_code in (400, 404):
            return {"exists": False, "deleted": True}
        response.raise_for_status()
        data = response.json()
        payment = data.get("Payment", {})
        return {
            "exists": True,
            "deleted": False,
            "payment_id": payment.get("Id"),
            "total_amount": float(payment.get("TotalAmt", 0)),
        }
    except Exception as e:
        return {"exists": False, "deleted": True, "error": str(e)}


def process_single_deposit(cur, base_url: str, headers: dict, deposit_id: str, bank_account_id: str, deposit_date: str) -> dict:
    """Process a single deposit: pre-flight check, create QBO deposit, update records."""
    now = datetime.now(timezone.utc).isoformat()
    
    # Get the deposit
    cur.execute("SELECT * FROM app_checks.deposits WHERE id = %s::uuid", (deposit_id,))
    deposit = cur.fetchone()
    if not deposit:
        return {"success": False, "deposit_id": deposit_id, "error": f"Deposit {deposit_id} not found"}
    
    col_names = [desc[0] for desc in cur.description]
    deposit = dict(zip(col_names, deposit))
    
    if deposit.get("qbo_deposit_id"):
        return {"success": False, "deposit_id": deposit_id, "error": f"Already has QBO deposit ID: {deposit['qbo_deposit_id']}"}
    
    # Get checks on this deposit
    cur.execute("SELECT * FROM app_checks.scanned_checks WHERE deposit_id = %s::uuid", (deposit_id,))
    check_cols = [desc[0] for desc in cur.description]
    checks = [dict(zip(check_cols, row)) for row in cur.fetchall()]
    check_ids = [str(c["id"]) for c in checks]
    
    # Get check_payments (Payment type with qbo_txn_id)
    payments = []
    if check_ids:
        cur.execute("""
            SELECT * FROM app_checks.check_payments 
            WHERE check_id = ANY(%s::uuid[]) AND qbo_entity_type = 'Payment' AND qbo_txn_id IS NOT NULL
        """, (check_ids,))
        pmt_cols = [desc[0] for desc in cur.description]
        payments = [dict(zip(pmt_cols, row)) for row in cur.fetchall()]
    
    # Get retail checks
    retail_checks = [c for c in checks if c.get("is_retail")]
    
    # Get cash entries
    cur.execute("SELECT * FROM app_checks.cash_entries WHERE deposit_id = %s::uuid", (deposit_id,))
    ce_cols = [desc[0] for desc in cur.description]
    cash_entries = [dict(zip(ce_cols, row)) for row in cur.fetchall()]
    
    # Validate we have at least one item
    if not payments and not retail_checks and not cash_entries:
        return {"success": False, "deposit_id": deposit_id, "error": "Deposit has no items"}
    
    # Validate retail checks have income accounts
    for rc in retail_checks:
        if not rc.get("retail_qbo_account_id"):
            return {"success": False, "deposit_id": deposit_id, "error": f"Retail check {rc.get('check_number', rc['id'])} missing QBO income account"}
    
    # Validate cash entries have income accounts
    for ce in cash_entries:
        if not ce.get("qbo_account_id"):
            return {"success": False, "deposit_id": deposit_id, "error": f"Cash entry {ce['id']} missing QBO income account"}
    
    # Pre-flight: verify each payment exists in QBO with correct amount
    sync_issues = []
    for pmt in payments:
        qbo_state = read_qbo_payment(base_url, headers, pmt["qbo_txn_id"])
        
        if not qbo_state.get("exists") or qbo_state.get("deleted"):
            sync_issues.append({
                "type": "deleted",
                "payment_id": str(pmt["id"]),
                "qbo_txn_id": pmt["qbo_txn_id"],
                "check_id": str(pmt["check_id"]),
                "message": f"Payment {pmt['qbo_txn_id']} no longer exists in QBO"
            })
            continue
        
        qbo_amount = qbo_state.get("total_amount", 0)
        local_amount = float(pmt.get("amount", 0))
        if abs(qbo_amount - local_amount) > 0.01:
            sync_issues.append({
                "type": "amount_mismatch",
                "payment_id": str(pmt["id"]),
                "qbo_txn_id": pmt["qbo_txn_id"],
                "check_id": str(pmt["check_id"]),
                "local_amount": local_amount,
                "qbo_amount": qbo_amount,
                "message": f"Payment {pmt['qbo_txn_id']} amount mismatch: local={local_amount}, QBO={qbo_amount}"
            })
    
    if sync_issues:
        deleted = [i for i in sync_issues if i["type"] == "deleted"]
        mismatches = [i for i in sync_issues if i["type"] == "amount_mismatch"]
        critical_issues = list(deleted)
        
        for mismatch in mismatches:
            check = next((c for c in checks if str(c["id"]) == mismatch["check_id"]), None)
            if check:
                check_amount = float(check.get("check_amount", 0))
                check_pmts = [p for p in payments if str(p["check_id"]) == mismatch["check_id"]]
                total_with_qbo = sum(
                    mismatch["qbo_amount"] if str(p["id"]) == mismatch["payment_id"] else float(p.get("amount", 0))
                    for p in check_pmts
                )
                if abs(total_with_qbo - check_amount) > 0.01:
                    mismatch["critical"] = True
                    critical_issues.append(mismatch)
        
        return {
            "success": False,
            "deposit_id": deposit_id,
            "error": "pre_flight_sync_failed",
            "sync_issues": sync_issues,
            "critical_issues": critical_issues,
            "message": f"Pre-flight found {len(sync_issues)} issue(s), {len(critical_issues)} critical",
        }
    
    # Build QBO Deposit JSON
    lines = []
    
    # Payment lines — LinkedTxn requires TxnLineId
    for pmt in payments:
        lines.append({
            "Amount": float(pmt["amount"]),
            "LinkedTxn": [{
                "TxnId": pmt["qbo_txn_id"],
                "TxnType": "Payment",
                "TxnLineId": "0"
            }]
        })
    
    # Retail check lines — DepositLineDetail (no LinkedTxn)
    for rc in retail_checks:
        description = "Retail check"
        if rc.get("check_number"):
            description += f" #{rc['check_number']}"
        if rc.get("check_name"):
            description += f" - {rc['check_name']}"
        lines.append({
            "Amount": float(rc.get("check_amount", 0)),
            "DetailType": "DepositLineDetail",
            "DepositLineDetail": {"AccountRef": {"value": rc["retail_qbo_account_id"]}},
            "Description": description
        })
    
    # Cash entry lines — DepositLineDetail (no LinkedTxn)
    for ce in cash_entries:
        description = ce.get("description") or "Cash deposit"
        if ce.get("office"):
            description = f"{ce['office']} - {description}"
        lines.append({
            "Amount": float(ce["amount"]),
            "DetailType": "DepositLineDetail",
            "DepositLineDetail": {"AccountRef": {"value": ce["qbo_account_id"]}},
            "Description": description
        })
    
    qbo_deposit = {
        "DepositToAccountRef": {"value": bank_account_id},
        "TxnDate": deposit_date,
        "Line": lines
    }
    
    # POST to QBO
    response = requests.post(
        f"{base_url}/deposit",
        headers=headers,
        json=qbo_deposit
    )
    
    if not response.ok:
        return {"success": False, "deposit_id": deposit_id, "error": f"QBO API error: {response.status_code} - {response.text}"}
    
    result = response.json()
    deposit_data = result.get("Deposit", {})
    qbo_deposit_id = deposit_data.get("Id")
    qbo_total = float(deposit_data.get("TotalAmt", 0))
    qbo_line_count = len(deposit_data.get("Line", []))
    
    # Update Supabase (bookkeeping only — NOT marking as reconciled)
    cur.execute("""
        UPDATE app_checks.deposits SET
            qbo_deposit_id = %s, qbo_deposit_created_at = %s,
            deposit_source = 'api', bank_account_id = %s, updated_at = %s
        WHERE id = %s::uuid
    """, (qbo_deposit_id, now, bank_account_id, now, deposit_id))
    
    payment_ids = [str(p["id"]) for p in payments]
    if payment_ids:
        cur.execute("""
            UPDATE app_checks.check_payments SET
                qbo_deposit_id = %s, qbo_deposit_date = %s, updated_at = %s
            WHERE id = ANY(%s::uuid[])
        """, (qbo_deposit_id, deposit_date, now, payment_ids))
    
    return {
        "success": True,
        "deposit_id": deposit_id,
        "qbo_deposit_id": qbo_deposit_id,
        "total_amount": qbo_total,
        "line_count": qbo_line_count,
        "our_line_count": len(lines),
        "payment_count": len(payments),
        "retail_count": len(retail_checks),
        "cash_count": len(cash_entries),
    }


def main(
    deposit_id: str = None,
    deposit_ids: list = None,
    bank_account_id: str = "74",
    deposit_date: str = None,
) -> dict:
    """
    Create QBO Deposit(s) for app deposit(s).
    
    Accepts either a single deposit_id or an array of deposit_ids.
    Token is refreshed once and reused for all deposits in the batch.
    
    Args:
        deposit_id: Single deposit UUID (for backwards compatibility)
        deposit_ids: Array of deposit UUIDs (for batch processing)
        bank_account_id: QBO bank account ID (default: 74 = United Checking *9303)
        deposit_date: Date for the QBO deposit (YYYY-MM-DD). Defaults to today.
    """
    
    # Normalize inputs
    ids_to_process = []
    if deposit_ids and isinstance(deposit_ids, list):
        ids_to_process = deposit_ids
    elif deposit_id:
        ids_to_process = [deposit_id]
    else:
        raise Exception("Either deposit_id or deposit_ids is required")
    
    if not deposit_date:
        deposit_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    # Single token refresh for entire batch
    access_token, realm_id = refresh_qbo_token()
    base_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    # Single DB connection for entire batch
    conn = get_db_conn()
    cur = conn.cursor()
    
    results = []
    for did in ids_to_process:
        try:
            result = process_single_deposit(cur, base_url, headers, str(did), bank_account_id, deposit_date)
            results.append(result)
        except Exception as e:
            results.append({"success": False, "deposit_id": str(did), "error": str(e)})
    
    cur.close()
    conn.close()
    
    succeeded = [r for r in results if r.get("success")]
    failed = [r for r in results if not r.get("success")]
    
    # For single deposit calls, return the single result directly for backwards compat
    if len(ids_to_process) == 1:
        return results[0]
    
    return {
        "total": len(ids_to_process),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "results": results,
    }
