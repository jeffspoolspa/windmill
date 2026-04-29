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
    customer_id: str,
    check_number: str = None,
    check_amount: float = None,
    check_date: str = None,
) -> dict:
    """
    Search QBO for existing payments matching check criteria.
    
    Queries all payments for a customer, then filters for:
    - Same check number + amount match
    - Same date + amount match (within $0.01)
    
    Returns matching payments with applied invoices.
    """
    
    access_token, realm_id = refresh_qbo_token()
    
    base_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    query = f"SELECT * FROM Payment WHERE CustomerRef = '{customer_id}'"
    response = requests.get(
        f"{base_url}/query",
        headers=headers,
        params={"query": query}
    )
    
    if not response.ok:
        raise Exception(f"QBO query failed: {response.status_code} - {response.text}")
    
    result = response.json()
    payments = result.get("QueryResponse", {}).get("Payment", [])
    
    if not payments:
        return {"payments": [], "customer_name": None}
    
    customer_name = payments[0].get("CustomerRef", {}).get("name", "Unknown")
    
    matches = []
    
    for pmt in payments:
        pmt_ref = pmt.get("PaymentRefNum", "")
        pmt_amount = float(pmt.get("TotalAmt", 0))
        pmt_date = pmt.get("TxnDate", "")
        pmt_id = pmt.get("Id", "")
        
        is_match = False
        
        if check_number and pmt_ref:
            norm_check = check_number.lstrip("0")
            norm_ref = pmt_ref.lstrip("0")
            if norm_check == norm_ref:
                if check_amount is None or abs(pmt_amount - check_amount) < 0.01:
                    is_match = True
        
        if not is_match and check_date and check_amount is not None:
            if pmt_date == check_date and abs(pmt_amount - check_amount) < 0.01:
                is_match = True
        
        if is_match:
            applied_invoices = []
            for line in pmt.get("Line", []):
                for linked in line.get("LinkedTxn", []):
                    if linked.get("TxnType") == "Invoice":
                        applied_invoices.append({
                            "invoice_id": linked.get("TxnId"),
                            "invoice_number": None,
                            "amount_applied": float(line.get("Amount", 0))
                        })
            
            for inv in applied_invoices:
                try:
                    inv_response = requests.get(
                        f"{base_url}/invoice/{inv['invoice_id']}",
                        headers=headers
                    )
                    if inv_response.ok:
                        inv_data = inv_response.json().get("Invoice", {})
                        inv["invoice_number"] = inv_data.get("DocNumber", inv["invoice_id"])
                except Exception:
                    inv["invoice_number"] = inv["invoice_id"]
            
            matches.append({
                "payment_id": pmt_id,
                "payment_ref": pmt_ref,
                "amount": pmt_amount,
                "date": pmt_date,
                "check_number": pmt_ref or None,
                "customer_name": customer_name,
                "applied_invoices": applied_invoices
            })
    
    return {
        "payments": matches,
        "customer_name": customer_name
    }