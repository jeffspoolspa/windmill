
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
    
    # CRITICAL: Save new refresh token immediately
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(resource_path, resource)
    
    return tokens["access_token"], resource["realm_id"]


def main(
    customer_id: str,
    amount: float,
    check_number: str,
    check_date: str,  # YYYY-MM-DD format
    invoices: list[dict] = None,  # [{"id": "123", "amount_applied": 150.00}, ...]
    is_unapplied: bool = False,  # True if no invoices (deposit/credit on account)
    memo: str = None,  # PrivateNote on the payment
    send_receipt: bool = False,  # Send payment receipt email to customer
    customer_email: str = None,  # Optional email override for receipt
) -> dict:
    """
    Create a check payment in QBO.
    
    - If invoices provided: creates payment applied to those invoices
    - If is_unapplied=True: creates unapplied payment (credit on account)
    - If memo provided: sets PrivateNote on the payment
    - If send_receipt=True: sends payment receipt email via QBO after creation
    
    Payment goes to Undeposited Funds by default.
    """
    
    # Step 1: Refresh token
    access_token, realm_id = refresh_qbo_token()
    
    base_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    # Step 2: Build payment object
    payment = {
        "CustomerRef": {"value": customer_id},
        "TotalAmt": amount,
        "PaymentRefNum": check_number,
        "TxnDate": check_date,
        "PaymentMethodRef": {"value": "6"}  # 6 = Check
        # Undeposited Funds is the default, no need to specify DepositToAccountRef
    }
    
    # Add memo as PrivateNote
    if memo:
        payment["PrivateNote"] = memo
    
    # Step 3: Add invoice line items if applying to invoices
    if invoices and not is_unapplied:
        lines = []
        for inv in invoices:
            lines.append({
                "Amount": inv["amount_applied"],
                "LinkedTxn": [{
                    "TxnId": inv["id"],
                    "TxnType": "Invoice"
                }]
            })
        payment["Line"] = lines
    
    # Step 4: Create payment in QBO
    response = requests.post(
        f"{base_url}/payment",
        headers=headers,
        json=payment
    )
    
    if not response.ok:
        error_text = response.text
        raise Exception(f"Failed to create payment: {response.status_code} - {error_text}")
    
    result = response.json()
    payment_data = result.get("Payment", {})
    payment_id = payment_data.get("Id")
    
    # Step 5: Send receipt email if requested (best-effort)
    receipt_sent = False
    receipt_error = None
    if send_receipt and payment_id:
        try:
            send_url = f"{base_url}/payment/{payment_id}/send"
            if customer_email:
                send_url += f"?sendTo={customer_email}"
            
            send_response = requests.post(send_url, headers=headers)
            
            if send_response.ok:
                receipt_sent = True
            else:
                receipt_error = f"Receipt send failed: {send_response.status_code} - {send_response.text}"
                print(receipt_error)
        except Exception as e:
            receipt_error = f"Receipt send error: {str(e)}"
            print(receipt_error)
    
    return {
        "success": True,
        "payment_id": payment_id,
        "payment_ref": payment_data.get("PaymentRefNum"),
        "total": float(payment_data.get("TotalAmt", 0)),
        "customer_name": payment_data.get("CustomerRef", {}).get("name"),
        "txn_date": payment_data.get("TxnDate"),
        "receipt_sent": receipt_sent,
        "receipt_error": receipt_error,
    }
