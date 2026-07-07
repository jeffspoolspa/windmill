"""
f/service_billing/set_customer_company

Set (or clear) a QBO customer's CompanyName. Company-filled is the system's
commercial marker (tasks v_task_class / billing peer groups derive from the
Customers.company cache, QBO is the source of truth), so this is THE lever
for relabeling a customer residential <-> commercial.

Sparse-updates the QBO Customer, then re-syncs our cache row via
refresh_customer so the segment flips everywhere at once. Follow with a
preprocess re-run (and the peer-group snapshot refresh) for any month whose
gates should re-evaluate under the new peer group.

Concurrency: qbo_api (shared registry).
"""

import requests
from f.service_billing.refresh_customer import main as refresh_customer
from f.service_billing.refresh_customer import refresh_qbo_token

QBO_BASE = "https://quickbooks.api.intuit.com/v3/company"


def main(qbo_customer_id: str, company_name: str, dry_run: bool = True):
    access_token, realm_id = refresh_qbo_token()
    r = requests.get(
        f"{QBO_BASE}/{realm_id}/query",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        params={"query": f"SELECT * FROM Customer WHERE Id = '{qbo_customer_id}'"},
        timeout=60,
    )
    if not r.ok:
        raise Exception(f"QBO query failed: {r.text[:300]}")
    custs = r.json().get("QueryResponse", {}).get("Customer", [])
    if not custs:
        raise Exception(f"customer {qbo_customer_id} not found in QBO")
    cust = custs[0]

    body = {
        "Id": cust["Id"],
        "SyncToken": cust["SyncToken"],
        "sparse": True,
        "CompanyName": company_name,
    }
    if dry_run:
        return {"dry_run": True, "customer": cust.get("DisplayName"),
                "company_before": cust.get("CompanyName"), "company_after": company_name}

    resp = requests.post(
        f"{QBO_BASE}/{realm_id}/customer",
        headers={"Authorization": f"Bearer {access_token}",
                 "Accept": "application/json", "Content-Type": "application/json"},
        json=body, timeout=60,
    )
    if not resp.ok:
        raise Exception(f"customer update failed: {resp.text[:400]}")
    updated = resp.json().get("Customer", {})

    refresh = refresh_customer(qbo_customer_id)
    return {
        "customer": updated.get("DisplayName"),
        "company_before": cust.get("CompanyName"),
        "company_after": updated.get("CompanyName"),
        "cache_refresh": refresh if isinstance(refresh, (str, int, dict)) else "ok",
    }
