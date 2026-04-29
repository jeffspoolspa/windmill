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
    wmill.set_resource(path=resource_path, value=resource)
    
    return tokens["access_token"], resource["realm_id"]


def qbo_query(access_token: str, realm_id: str, query: str) -> dict:
    """Execute a QBO query"""
    response = requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json"
        },
        params={"query": query}
    )
    if not response.ok:
        raise Exception(f"QBO query failed: {response.status_code} - {response.text}")
    return response.json()


def main(invoice_numbers: list[str]) -> dict:
    """
    Search QBO for invoices by number using a single batched query.
    Returns invoice + customer info with multi-customer support.
    """
    
    access_token, realm_id = refresh_qbo_token()
    
    # Clean invoice numbers (remove # prefix if present)
    cleaned_numbers = [num.lstrip('#') for num in invoice_numbers]
    
    found_invoices = []
    matched_numbers = []
    unmatched_numbers = list(cleaned_numbers)  # Start with all unmatched, remove as found
    customer_ids = set()
    invoices_by_customer = {}
    
    # Single batched query: WHERE DocNumber IN ('num1', 'num2', ...)
    # QBO supports IN queries on DocNumber
    if cleaned_numbers:
        escaped = [n.replace("'", "\\'") for n in cleaned_numbers]
        in_clause = ", ".join(f"'{n}'" for n in escaped)
        query = f"SELECT * FROM Invoice WHERE DocNumber IN ({in_clause})"
        
        try:
            result = qbo_query(access_token, realm_id, query)
            invoices = result.get('QueryResponse', {}).get('Invoice', [])
            
            for inv in invoices:
                doc_number = inv.get("DocNumber", "")
                customer_ref = inv.get("CustomerRef", {})
                customer_id = customer_ref.get("value")
                customer_name = customer_ref.get("name")
                
                invoice_data = {
                    "id": inv.get("Id"),
                    "number": doc_number,
                    "customer_id": customer_id,
                    "customer_name": customer_name,
                    "total": float(inv.get("TotalAmt", 0)),
                    "balance": float(inv.get("Balance", 0)),
                    "due_date": inv.get("DueDate"),
                    "txn_date": inv.get("TxnDate")
                }
                found_invoices.append(invoice_data)
                
                # Track matched numbers
                if doc_number in unmatched_numbers:
                    unmatched_numbers.remove(doc_number)
                    matched_numbers.append(doc_number)
                
                if customer_id:
                    customer_ids.add((customer_id, customer_name))
                    if customer_id not in invoices_by_customer:
                        invoices_by_customer[customer_id] = {
                            "customer": {"id": customer_id, "name": customer_name},
                            "invoices": []
                        }
                    invoices_by_customer[customer_id]["invoices"].append(invoice_data)
                    
        except Exception as e:
            print(f"Error querying invoices: {e}")
            # On error, all numbers stay unmatched
    
    # Build customer info
    customer = None
    if len(customer_ids) == 1:
        cust_id, cust_name = list(customer_ids)[0]
        customer = {"id": cust_id, "name": cust_name}
    
    customers = []
    if len(customer_ids) > 1:
        for cust_id, cust_data in invoices_by_customer.items():
            customers.append(cust_data)
    
    return {
        "success": len(found_invoices) > 0,
        "invoices": found_invoices,
        "customer": customer,
        "customers": customers,
        "matched_numbers": matched_numbers,
        "unmatched_numbers": unmatched_numbers,
        "multi_customer": len(customer_ids) > 1
    }
