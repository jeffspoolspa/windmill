import requests, wmill

GUSTO_API = "https://api.gusto.com"
PAYROLL = "6e9fb975-fcbe-4ccc-8eea-d4fa3d8776ef"  # 5/22, ~38 employees, gross 32143.22


def probe(url, headers, params, label):
    r = requests.get(url, headers=headers, params=params)
    j = r.json()
    comps = j.get("employee_compensations") or []
    uuids = [c.get("employee_uuid") for c in comps]
    interesting = {k: v for k, v in r.headers.items()
                   if k.lower() in ("link", "x-total-count", "x-page", "x-per-page",
                                    "x-next-page", "x-total-pages")}
    return {"label": label, "status": r.status_code, "n": len(comps),
            "first_uuid": uuids[0] if uuids else None,
            "last_uuid": uuids[-1] if uuids else None,
            "pagination_headers": interesting}


def main():
    company_id = wmill.get_variable("f/gusto/company_id")
    token = wmill.get_variable("f/gusto/personal_access_token")
    headers = {"Authorization": f"Bearer {token}",
               "X-Gusto-API-Version": "2025-06-15", "Accept": "application/json"}
    url = f"{GUSTO_API}/v1/companies/{company_id}/payrolls/{PAYROLL}"
    return {"probes": [
        probe(url, headers, {}, "default"),
        probe(url, headers, {"page": 1, "per": 100}, "page1_per100"),
        probe(url, headers, {"page": 2, "per": 100}, "page2_per100"),
        probe(url, headers, {"page": 2}, "page2_only"),
        probe(url, headers, {"per": 100}, "per100_only"),
    ]}
