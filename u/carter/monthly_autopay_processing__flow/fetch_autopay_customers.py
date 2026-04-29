import requests
import wmill
from datetime import datetime

def main(billing_month: str, test_mode: bool = False, test_qbo_customer_id: str = None):
    """
    Fetch autopay customers from Airtable who haven't been processed yet.
    In test_mode, returns only the specified customer.
    """
    resource = wmill.get_resource("u/carter/airtable")
    api_key = resource.get("apiKey") if isinstance(resource, dict) else resource
    if isinstance(api_key, str) and api_key.startswith("$var:"):
        api_key = wmill.get_variable(api_key.replace("$var:", ""))
    
    base_id = "apppQeFQh1Mi6Mv3p"
    table_id = "tbl5l8R6on9W0uiIN"
    
    month_name = datetime.strptime(billing_month, "%Y-%m").strftime("%B %Y")
    
    # Fetch all records
    all_customers = []
    offset = None
    
    while True:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
            
        response = requests.get(
            f"https://api.airtable.com/v0/{base_id}/{table_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params
        )
        
        if not response.ok:
            raise Exception(f"Airtable error: {response.status_code} - {response.text}")
        
        data = response.json()
        records = data.get("records", [])
        
        for record in records:
            fields = record.get("fields", {})
            
            # Skip if already completed
            if fields.get("Completed"):
                continue
            
            # Skip if no QBO ID
            qbo_id = fields.get("QBO ID")
            if not qbo_id:
                continue
            
            customer = {
                "airtable_record_id": record["id"],
                "qbo_customer_id": str(int(qbo_id)),
                "name": fields.get("Name", "Unknown"),
                "existing_notes": fields.get("Notes", "")
            }
            
            # If test mode, only include the specified customer
            if test_mode:
                if customer["qbo_customer_id"] == str(test_qbo_customer_id):
                    all_customers.append(customer)
            else:
                all_customers.append(customer)
        
        offset = data.get("offset")
        if not offset:
            break
    
    return {
        "billing_month": billing_month,
        "month_display": month_name,
        "test_mode": test_mode,
        "total_customers": len(all_customers),
        "customers": all_customers
    }
