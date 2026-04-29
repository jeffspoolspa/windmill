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


def main(qbo_payment_id: str) -> dict:
    """
    Read a single QBO Payment by ID and return its full current state.
    
    Returns payment details including amount, applied invoices with DocNumbers,
    customer info, and unapplied amount. If the payment has been deleted,
    returns {exists: false, deleted: true}.
    
    Args:
        qbo_payment_id: The QBO Payment ID to read
    
    Returns:
        dict with payment state or deleted indicator
    """
    
    if not qbo_payment_id:
        raise Exception("qbo_payment_id is required")
    
    access_token, realm_id = refresh_qbo_token()
    
    base_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    # Read the payment
    response = requests.get(
        f"{base_url}/payment/{qbo_payment_id}",
        headers=headers
    )
    
    # Handle deleted/not found
    if response.status_code in (400, 404):
        return {
            "exists": False,
            "deleted": True,
            "payment_id": qbo_payment_id,
        }
    
    if not response.ok:
        raise Exception(f"QBO API error: {response.status_code} - {response.text}")
    
    result = response.json()
    payment_data = result.get("Payment", {})
    
    if not payment_data:
        return {
            "exists": False,
            "deleted": True,
            "payment_id": qbo_payment_id,
        }
    
    # Extract applied invoices from Line items
    applied_invoices = []
    for line in payment_data.get("Line", []):
        for linked_txn in line.get("LinkedTxn", []):
            if linked_txn.get("TxnType") == "Invoice":
                applied_invoices.append({
                    "invoice_id": linked_txn.get("TxnId", ""),
                    "amount_applied": float(line.get("Amount", 0)),
                })
    
    # Fetch DocNumbers for each linked invoice
    for inv in applied_invoices:
        try:
            inv_response = requests.get(
                f"{base_url}/invoice/{inv['invoice_id']}",
                headers=headers
            )
            if inv_response.ok:
                inv_data = inv_response.json().get("Invoice", {})
                inv["invoice_number"] = inv_data.get("DocNumber", "")
            else:
                inv["invoice_number"] = ""
        except Exception:
            inv["invoice_number"] = ""
    
    # Check deposit status - if DepositToAccountRef is NOT Undeposited Funds (typically "87"),
    # the payment has been moved to a bank account via a deposit
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
