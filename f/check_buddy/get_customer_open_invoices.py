# Get Customer Open Invoices
# Returns open invoices for a customer from QBO

import requests
import wmill

QBO_RESOURCE = "u/carter/quickbooks_api"
REQUEST_TIMEOUT = 30


def refresh_qbo_token() -> tuple[str, str]:
    """Refresh QBO token and return (access_token, realm_id)."""
    resource = wmill.get_resource(QBO_RESOURCE)
    
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
        auth=(resource["client_id"], resource["client_secret"]),
        timeout=REQUEST_TIMEOUT
    )
    
    if not response.ok:
        raise Exception(f"Token refresh failed: {response.status_code} - {response.text}")
    
    tokens = response.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(QBO_RESOURCE, resource)
    
    return tokens["access_token"], resource["realm_id"]


def qbo_query(access_token: str, realm_id: str, query: str) -> dict:
    """Execute a QBO query."""
    response = requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json"
        },
        params={"query": query},
        timeout=REQUEST_TIMEOUT
    )
    if not response.ok:
        raise Exception(f"QBO query failed: {response.status_code} - {response.text}")
    return response.json()


def main(customer_id: str) -> dict:
    """Fetch open invoices for a customer from QBO."""
    access_token, realm_id = refresh_qbo_token()
    
    query = f"SELECT * FROM Invoice WHERE CustomerRef = '{customer_id}' AND Balance > '0'"
    result = qbo_query(access_token, realm_id, query)
    
    invoices = result.get('QueryResponse', {}).get('Invoice', [])
    
    return {
        "invoices": [
            {
                "id": inv.get('Id'),
                "number": inv.get('DocNumber'),
                "date": inv.get('TxnDate'),
                "due_date": inv.get('DueDate'),
                "total": float(inv.get('TotalAmt', 0)),
                "balance": float(inv.get('Balance', 0)),
            }
            for inv in invoices
        ],
        "count": len(invoices)
    }
