#extra_requirements:
#requests

import requests
import wmill


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
    qbo_deposit_id: str,
) -> dict:
    """
    Read a QBO Deposit by ID and return all line items with full detail.
    
    Args:
        qbo_deposit_id: The QBO Deposit entity ID
    
    Returns:
        deposit_id: QBO Deposit ID
        deposit_date: Transaction date
        deposit_total: Total amount
        deposit_account: Bank account name
        line_count: Number of lines
        lines: List of line items with linked transaction info
    """
    
    if not qbo_deposit_id:
        return {"error": "qbo_deposit_id is required", "exists": False}
    
    access_token, realm_id = refresh_qbo_token()
    
    base_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    # Read the deposit by ID
    response = requests.get(
        f"{base_url}/deposit/{qbo_deposit_id}",
        headers=headers,
    )
    
    if response.status_code == 400 or response.status_code == 404:
        return {"exists": False, "error": f"Deposit {qbo_deposit_id} not found"}
    
    if not response.ok:
        raise Exception(f"QBO read failed: {response.status_code} - {response.text}")
    
    data = response.json()
    deposit = data.get("Deposit", {})
    
    if not deposit:
        return {"exists": False, "error": "No deposit in response"}
    
    # Extract deposit header info
    deposit_id = deposit.get("Id", "")
    deposit_date = deposit.get("TxnDate", "")
    deposit_total = float(deposit.get("TotalAmt", 0))
    
    # Get the bank account name
    deposit_account = ""
    deposit_to_ref = deposit.get("DepositToAccountRef", {})
    if deposit_to_ref:
        deposit_account = deposit_to_ref.get("name", "")
    
    # Extract all line items with full detail
    lines = []
    for line in deposit.get("Line", []):
        line_amount = float(line.get("Amount", 0))
        detail_type = line.get("DetailType", "")
        description = line.get("Description", "")
        
        # Extract linked transactions
        linked_txn_type = None
        linked_txn_id = None
        
        linked_txns = line.get("LinkedTxn", [])
        if linked_txns:
            # Usually just one linked txn per line
            linked_txn_type = linked_txns[0].get("TxnType", None)
            linked_txn_id = linked_txns[0].get("TxnId", None)
        
        # Extract detail object (varies by DetailType)
        detail = {}
        if detail_type == "DepositLineDetail":
            deposit_line_detail = line.get("DepositLineDetail", {})
            detail = {
                "account_name": deposit_line_detail.get("AccountRef", {}).get("name", ""),
                "account_id": deposit_line_detail.get("AccountRef", {}).get("value", ""),
                "payment_method": deposit_line_detail.get("PaymentMethodRef", {}).get("name", ""),
                "entity_name": deposit_line_detail.get("Entity", {}).get("name", ""),
                "entity_id": deposit_line_detail.get("Entity", {}).get("value", ""),
                "entity_type": deposit_line_detail.get("Entity", {}).get("type", ""),
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
        "deposit_id": deposit_id,
        "deposit_date": deposit_date,
        "deposit_total": deposit_total,
        "deposit_account": deposit_account,
        "line_count": len(lines),
        "lines": lines,
    }
