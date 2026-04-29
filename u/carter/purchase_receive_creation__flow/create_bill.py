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


def main(purchase_receipt: dict):
    ORGANIZATION_ID = "870657839"
    ACCESS_TOKEN = get_access_token()
    
    base_url = "https://www.zohoapis.com/inventory/v1"
    headers = {
        "Authorization": f"Zoho-oauthtoken {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    params = {"organization_id": ORGANIZATION_ID}
    
    # Extract from webhook payload
    receive_data = purchase_receipt.get("purchase_receive", purchase_receipt)
    
    po_id = receive_data.get("purchaseorder_id")
    vendor_id = receive_data.get("vendor_id")
    location_id = receive_data.get("location_id")
    receive_date = receive_data.get("date")
    po_number = receive_data.get("purchaseorder_number")
    
    # Get bill number from custom field on receive
    receive_custom_fields = receive_data.get("custom_fields", [])
    bill_number = next(
        (f["value"] for f in receive_custom_fields if f["api_name"] == "cf_invoice_number"),
        None
    )
    
    # Still need to fetch PO for type, work order number, and rates
    po_resp = requests.get(
        f"{base_url}/purchaseorders/{po_id}",
        headers=headers,
        params=params
    )
    po_data = po_resp.json().get("purchaseorder", {})
    
    po_custom_fields = po_data.get("custom_fields", [])
    po_type = next(
        (f["value"] for f in po_custom_fields if f["api_name"] == "cf_order_type"),
        None
    )
    work_order_number = next(
        (f["value"] for f in po_custom_fields if f["api_name"] == "cf_work_order_number"),
        None
    )
    
    # Build PO line item lookup: item_id -> {line_item_id, rate}
    po_line_lookup = {
        line["item_id"]: {
            "line_item_id": line["line_item_id"],
            "rate": line["rate"]
        }
        for line in po_data.get("line_items", [])
    }
    
    # Build bill line items
    bill_line_items = []
    for line in receive_data.get("line_items", []):
        item_id = line["item_id"]
        po_line = po_line_lookup.get(item_id, {})
        
        bill_line_items.append({
            "item_id": item_id,
            "purchaseorder_item_id": po_line.get("line_item_id"),
            "receive_item_id": line["line_item_id"],
            "location_id": line.get("location_id"),
            "quantity": line["quantity"],
            "rate": po_line.get("rate", 0)
        })
    
    # Create bill
    bill_payload = {
        "vendor_id": vendor_id,
        "status": "open",
        "date": receive_date,
        "bill_number": bill_number,
        "reference_number": po_number,
        "location_id": location_id,
        "line_items": bill_line_items
    }
    
    bill_resp = requests.post(
        f"{base_url}/bills",
        headers=headers,
        params=params,
        json=bill_payload
    )
    bill_data = bill_resp.json()
    
    if bill_data.get("code") != 0:
        raise Exception(f"Failed to create bill: {bill_data.get('message')}")
    
    bill = bill_data.get("bill", {})
    bill_id = bill.get("bill_id")
    
    print(f"✅ Created bill: {bill_number}")
    print(f"📋 PO Type: {po_type}")
    
    return {
        "success": True,
        "bill_id": bill_id,
        "bill_number": bill_number,
        "po_type": po_type,
        "work_order_number": work_order_number,
        "po_id": po_id,
        "po_number": po_number
    }