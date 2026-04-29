#extra_requirements:
#requests

import requests
import wmill
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
    wmill.set_resource(resource_path, resource)
    
    return tokens["access_token"], resource["realm_id"]


def main(
    payment_ids: list[str],
    lookback_days: int = 90,
) -> dict:
    """
    Check if QBO payments have been included in a QBO Deposit (reconciled).
    
    Queries QBO Deposit entities and scans their Line[].LinkedTxn for
    TxnType="Payment" matching the input payment IDs.
    
    Args:
        payment_ids: List of QBO Payment IDs to check
        lookback_days: How far back to search for deposits (default 90 days)
    
    Returns:
        results: List of {payment_id, is_cleared, deposit_id, deposit_date}
        cleared_count: Number of payments found in deposits
        uncleared_count: Number of payments not found in deposits
    """
    
    if not payment_ids:
        return {
            "results": [],
            "cleared_count": 0,
            "uncleared_count": 0,
        }
    
    access_token, realm_id = refresh_qbo_token()
    
    base_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    # Query QBO Deposits within the lookback window
    cutoff_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    query = f"SELECT * FROM Deposit WHERE TxnDate >= '{cutoff_date}'"
    
    response = requests.get(
        f"{base_url}/query",
        headers=headers,
        params={"query": query}
    )
    
    if not response.ok:
        raise Exception(f"QBO query failed: {response.status_code} - {response.text}")
    
    result = response.json()
    deposits = result.get("QueryResponse", {}).get("Deposit", [])
    
    # Build a map: payment_id -> (deposit_id, deposit_date)
    payment_to_deposit = {}
    
    for deposit in deposits:
        deposit_id = deposit.get("Id", "")
        deposit_date = deposit.get("TxnDate", "")
        
        for line in deposit.get("Line", []):
            for linked_txn in line.get("LinkedTxn", []):
                if linked_txn.get("TxnType") == "Payment":
                    txn_id = linked_txn.get("TxnId", "")
                    if txn_id:
                        payment_to_deposit[txn_id] = {
                            "deposit_id": deposit_id,
                            "deposit_date": deposit_date,
                        }
    
    # Check each input payment_id against the map
    results = []
    cleared_count = 0
    uncleared_count = 0
    
    for pmt_id in payment_ids:
        if pmt_id in payment_to_deposit:
            info = payment_to_deposit[pmt_id]
            results.append({
                "payment_id": pmt_id,
                "is_cleared": True,
                "deposit_id": info["deposit_id"],
                "deposit_date": info["deposit_date"],
            })
            cleared_count += 1
        else:
            results.append({
                "payment_id": pmt_id,
                "is_cleared": False,
                "deposit_id": None,
                "deposit_date": None,
            })
            uncleared_count += 1
    
    return {
        "results": results,
        "cleared_count": cleared_count,
        "uncleared_count": uncleared_count,
    }
