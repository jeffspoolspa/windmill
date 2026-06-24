import time, requests, wmill
from datetime import datetime, timedelta

GUSTO_API = "https://api.gusto.com"


def gusto_get(url, headers, params=None, max_retries=5):
    for attempt in range(max_retries):
        resp = requests.get(url, headers=headers, params=params or {})
        if resp.status_code != 429:
            return resp
        time.sleep(int(resp.headers.get("Retry-After", "30")))
    return resp


def main(check_start: str = "2026-05-01", check_end: str = "2026-05-31"):
    company_id = wmill.get_variable("f/gusto/company_id")
    token = wmill.get_variable("f/gusto/personal_access_token")
    headers = {"Authorization": f"Bearer {token}",
               "X-Gusto-API-Version": "2025-06-15", "Accept": "application/json"}

    cs = datetime.strptime(check_start, "%Y-%m-%d").date()
    ce = datetime.strptime(check_end, "%Y-%m-%d").date()
    win_start = (cs - timedelta(days=45)).isoformat()
    pr = gusto_get(f"{GUSTO_API}/v1/companies/{company_id}/payrolls", headers,
                   {"processing_statuses": "processed", "payroll_types": "regular,off_cycle",
                    "start_date": win_start, "end_date": check_end, "per": 100})
    pr.raise_for_status()
    diag = []
    for p in pr.json():
        cd = p.get("check_date")
        if not (cd and cs <= datetime.strptime(cd, "%Y-%m-%d").date() <= ce and not p.get("external")):
            continue
        puid = p["payroll_uuid"]
        url = f"{GUSTO_API}/v1/companies/{company_id}/payrolls/{puid}"

        # Page 1 with per=100
        r1 = gusto_get(url, headers, {"employee_compensations_per": 100,
                                      "employee_compensations_page": 1})
        f1 = r1.json()
        comps1 = f1.get("employee_compensations") or []
        # also try WITHOUT pagination params (default call)
        r0 = gusto_get(url, headers)
        f0 = r0.json()
        comps0 = f0.get("employee_compensations") or []
        diag.append({
            "check_date": cd,
            "status_per100": r1.status_code,
            "comps_with_per100": len(comps1),
            "comps_default_call": len(comps0),
            "payroll_employee_count_field": f1.get("employee_count"),
            "totals_gross_pay": (f1.get("totals") or {}).get("gross_pay"),
            "pagination_meta": f1.get("employee_compensations_pagination"),
            "top_level_keys_sample": sorted(list(f1.keys()))[:25],
        })
    return {"diag": diag}
