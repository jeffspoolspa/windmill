import requests
import wmill

def get_access_token():
    client_id = wmill.get_variable("u/ZOHO/CLIENT_ID")
    client_secret = wmill.get_variable("u/ZOHO/CLIENT_SECRET")
    refresh_token = wmill.get_variable("u/ZOHO/REFRESH_TOKEN")
    token_url = "https://accounts.zoho.com/oauth/v2/token"
    data = {
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token"
    }
    token_response = requests.post(token_url, data=data)
    return token_response.json().get("access_token")


def main(
    bill_id: str,
    bill_number: str,
    po_type: str,
    work_order_number: str,
    po_id: str,
    po_number: str
):
    # Only create invoice for work orders
    if po_type != "Work Order":
        print(f"⏭️ Skipped: PO type is '{po_type}', not 'Work Order'")
        return {
            "success": True,
            "skipped": True,
            "reason": f"PO type is {po_type}"
        }
    
    ORGANIZATION_ID = "870657839"
    CUSTOMER_ID = "5727383000000176743"
    ACCESS_TOKEN = get_access_token()
    
    base_url = "https://www.zohoapis.com/inventory/v1"
    headers = {
        "Authorization": f"Zoho-oauthtoken {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    params = {"organization_id": ORGANIZATION_ID}
    
    # 1. Fetch the bill
    bill_resp = requests.get(
        f"{base_url}/bills/{bill_id}",
        headers=headers,
        params=params
    )
    bill_data = bill_resp.json().get("bill", {})
    
    # 2. Build invoice line items
    line_items = [
        {
            "item_id": line["item_id"],
            "quantity": line["quantity"],
        }
        for line in bill_data.get("line_items", [])
    ]
    
    # 3. Create invoice
    invoice_payload = {
        "customer_id": CUSTOMER_ID,
        "line_items": line_items,
        "reference_number": work_order_number,
    }
    
    invoice_resp = requests.post(
        f"{base_url}/invoices",
        headers=headers,
        params=params,
        json=invoice_payload
    )
    invoice_data = invoice_resp.json()
    
    if invoice_data.get("code") != 0:
        error_msg = invoice_data.get("message", "Unknown error")
        # Raise exception - Windmill will handle notification
        raise Exception(f"Invoice failed for Bill {bill_number}, WO {work_order_number}: {error_msg}")
    
    invoice = invoice_data.get("invoice", {})
    invoice_id = invoice.get("invoice_id")
    invoice_number = invoice.get("invoice_number")
    
    print(f"✅ Created invoice: {invoice_number}")
    
    # 4. Mark invoice as sent
    sent_resp = requests.post(
        f"{base_url}/invoices/{invoice_id}/status/sent",
        headers=headers,
        params=params
    )
    
    if sent_resp.json().get("code") == 0:
        print(f"📤 Marked as sent")
    else:
        raise Exception(f"⚠️ Warning: Failed to mark as sent")
    
    return {
        "success": True,
        "invoice_id": invoice_id,
        "invoice_number": invoice_number,
        "bill_number": bill_number,
        "work_order_number": work_order_number
    }