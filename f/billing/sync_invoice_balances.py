
import requests
import wmill
import psycopg2
from datetime import datetime

def main():
    """
    Sync invoice balances from QBO → billing_audit.maintenance_invoices.balance_due.
    
    Approach:
    1. Fetch ALL open invoices from QBO (Balance > 0) with pagination
    2. Build lookup by QBO invoice ID
    3. For each maintenance invoice in our table:
       - If found in QBO open list → set balance_due = QBO balance
       - If NOT found → set balance_due = 0 (paid in full)
    4. Set balance_synced_at = now() for all updated rows
    
    This is efficient: a few paginated QBO calls instead of 7000 individual lookups.
    """
    
    # =========================================
    # 1. QBO Token Refresh
    # =========================================
    resource_path = "u/carter/quickbooks_api"
    resource = wmill.get_resource(resource_path)
    
    response = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"])
    )
    
    if not response.ok:
        raise Exception(f"Token refresh failed: {response.text}")
    
    tokens = response.json()
    access_token = tokens["access_token"]
    
    # CRITICAL: Save new refresh token
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(resource_path, resource)
    
    realm_id = resource["realm_id"]
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    
    # =========================================
    # 2. Fetch all open invoices from QBO
    # =========================================
    open_invoices = {}  # qbo_invoice_id → balance
    start_position = 1
    page_size = 1000
    
    while True:
        query = f"SELECT Id, Balance FROM Invoice WHERE Balance > '0' STARTPOSITION {start_position} MAXRESULTS {page_size}"
        
        resp = requests.get(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
            headers=headers,
            params={"query": query}
        )
        
        if not resp.ok:
            raise Exception(f"QBO query failed at pos {start_position}: {resp.text}")
        
        invoices = resp.json().get("QueryResponse", {}).get("Invoice", [])
        
        if not invoices:
            break
        
        for inv in invoices:
            open_invoices[inv["Id"]] = float(inv.get("Balance", 0))
        
        start_position += page_size
        if start_position > 50000:
            break
    
    # =========================================
    # 3. Connect to Supabase and update
    # =========================================
    supabase = wmill.get_resource("u/carter/supabase")
    conn = psycopg2.connect(
        host=supabase['host'], port=supabase['port'],
        dbname=supabase['dbname'], user=supabase['user'],
        password=supabase['password'], sslmode='require'
    )
    cur = conn.cursor()
    now = datetime.utcnow()
    
    # Get all our maintenance invoice QBO IDs
    cur.execute("SELECT id, qbo_invoice_id FROM billing_audit.maintenance_invoices")
    our_invoices = cur.fetchall()
    
    updated_open = 0
    updated_paid = 0
    
    for row_id, qbo_inv_id in our_invoices:
        qbo_id_str = str(qbo_inv_id)
        
        if qbo_id_str in open_invoices:
            # Still has a balance in QBO
            balance = open_invoices[qbo_id_str]
            cur.execute("""
                UPDATE billing_audit.maintenance_invoices
                SET balance_due = %s, balance_synced_at = %s
                WHERE id = %s
            """, (balance, now, row_id))
            updated_open += 1
        else:
            # Not in QBO open list = paid (or voided)
            cur.execute("""
                UPDATE billing_audit.maintenance_invoices
                SET balance_due = 0, balance_synced_at = %s
                WHERE id = %s
            """, (now, row_id))
            updated_paid += 1
    
    conn.commit()
    
    # =========================================
    # 4. Summary
    # =========================================
    cur.execute("""
        SELECT 
            billing_month,
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE balance_due > 0) AS unpaid,
            COUNT(*) FILTER (WHERE balance_due = 0) AS paid,
            COALESCE(SUM(balance_due) FILTER (WHERE balance_due > 0), 0) AS outstanding
        FROM billing_audit.maintenance_invoices
        WHERE balance_synced_at IS NOT NULL
        GROUP BY billing_month
        ORDER BY billing_month DESC
    """)
    
    by_month = []
    for row in cur.fetchall():
        by_month.append({
            "month": str(row[0]),
            "total": row[1],
            "unpaid": row[2],
            "paid": row[3],
            "outstanding": float(row[4])
        })
    
    cur.close()
    conn.close()
    
    return {
        "qbo_open_invoices_found": len(open_invoices),
        "our_invoices_total": len(our_invoices),
        "updated_with_balance": updated_open,
        "marked_as_paid": updated_paid,
        "by_month": by_month
    }
