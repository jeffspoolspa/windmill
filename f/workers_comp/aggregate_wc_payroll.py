import time, requests, wmill
from datetime import datetime, timedelta
from collections import defaultdict

GUSTO_API = "https://api.gusto.com"

DEPARTMENT_TO_CLASS_CODE = {
    "Back Office": "8810",
    "Maintenance": "9014",
    "Service":     "9014",
    "Slide Crew":  "9014",
    "Retail":      "8017",
}
OT_LABELS = {"Overtime", "Double overtime", "Double Overtime"}
KNOWN_EARNINGS = {"Regular Hours", "Regular", "Overtime", "Double overtime",
                  "Double Overtime", "Vacation Hours", "Sick Hours",
                  "Holiday Hours", "Bonus", "Commission"}
EXCLUDED = {"Reimbursement", "Reimbursements"}
PAGE_SIZE = 25   # endpoint hard-caps employee_compensations at 25, no body pagination meta


def gusto_get(url, headers, params=None, max_retries=5):
    for attempt in range(max_retries):
        resp = requests.get(url, headers=headers, params=params or {})
        if resp.status_code != 429:
            return resp
        time.sleep(int(resp.headers.get("Retry-After", "30")))
    return resp


def all_compensations(url, headers, max_pages=40):
    """Page by number; dedupe by employee_uuid. Stops when a page adds no new
    employee (covers both working-page and ignored-page behaviour) or returns a
    short/empty page. Prevents both undercount and infinite loop."""
    seen, comps, page = set(), [], 1
    while page <= max_pages:
        chunk = (gusto_get(url, headers, {"employee_compensations_page": page})
                 .json().get("employee_compensations") or [])
        new = [c for c in chunk if c.get("employee_uuid") not in seen]
        if not new:
            break
        for c in new:
            seen.add(c.get("employee_uuid"))
        comps.extend(new)
        if len(chunk) < PAGE_SIZE:
            break
        page += 1
    return comps


def main(check_start: str = "2026-05-01", check_end: str = "2026-05-31"):
    company_id = wmill.get_variable("f/gusto/company_id")
    token = wmill.get_variable("f/gusto/personal_access_token")
    headers = {"Authorization": f"Bearer {token}",
               "X-Gusto-API-Version": "2025-06-15", "Accept": "application/json"}

    cs = datetime.strptime(check_start, "%Y-%m-%d").date()
    ce = datetime.strptime(check_end, "%Y-%m-%d").date()

    dr = gusto_get(f"{GUSTO_API}/v1/companies/{company_id}/departments", headers)
    dr.raise_for_status()
    uuid_to_code, uuid_to_dept = {}, {}
    for d in dr.json():
        code = DEPARTMENT_TO_CLASS_CODE.get(d.get("title"))
        for e in d.get("employees", []):
            uuid_to_dept[e["uuid"]] = d.get("title")
            uuid_to_code[e["uuid"]] = code

    win_start = (cs - timedelta(days=45)).isoformat()
    pr = gusto_get(f"{GUSTO_API}/v1/companies/{company_id}/payrolls", headers,
                   {"processing_statuses": "processed", "payroll_types": "regular,off_cycle",
                    "start_date": win_start, "end_date": check_end, "per": 100})
    pr.raise_for_status()
    payrolls = []
    for p in pr.json():
        cd = p.get("check_date")
        if cd and cs <= datetime.strptime(cd, "%Y-%m-%d").date() <= ce and not p.get("external"):
            payrolls.append(p)

    by_code = defaultdict(lambda: {"gross": 0.0, "ot": 0.0})
    exceptions, payroll_gross, emps_paid = [], {}, set()

    for p in payrolls:
        puid = p["payroll_uuid"]
        url = f"{GUSTO_API}/v1/companies/{company_id}/payrolls/{puid}"
        pg = 0.0
        for comp in all_compensations(url, headers):
            emp = comp.get("employee_uuid")
            gross = float(comp.get("gross_pay") or 0)
            ot, unknown = 0.0, []
            for arr in ("hourly_compensations", "fixed_compensations", "paid_time_off"):
                for line in (comp.get(arr) or []):
                    nm, amt = line.get("name", ""), float(line.get("amount") or 0)
                    if amt == 0:
                        continue
                    if nm in OT_LABELS:
                        ot += amt
                    elif nm in KNOWN_EARNINGS or nm in EXCLUDED:
                        pass
                    else:
                        unknown.append(nm)
            if unknown:
                exceptions.append({"type": "unknown_earning", "employee_uuid": emp,
                                   "payroll": puid, "names": sorted(set(unknown))})
            if gross == 0 and ot == 0:
                continue
            pg += gross
            if uuid_to_dept.get(emp) is None:
                exceptions.append({"type": "missing_department", "employee_uuid": emp,
                                   "payroll": puid, "gross_pay": round(gross, 2)})
                continue
            code = uuid_to_code.get(emp)
            if code is None:
                exceptions.append({"type": "unmapped_department", "employee_uuid": emp,
                                   "department": uuid_to_dept.get(emp), "gross_pay": round(gross, 2)})
                continue
            by_code[code]["gross"] += gross
            by_code[code]["ot"] += ot
            emps_paid.add(emp)
        payroll_gross[p.get("check_date")] = round(pg, 2)
        time.sleep(0.1)

    report = {c: {"gross_wages": round(v["gross"], 2), "overtime_pay": round(v["ot"], 2)}
              for c, v in sorted(by_code.items())}
    return {
        "period_basis": "check_date_in_month",
        "check_window": [check_start, check_end],
        "payrolls_used": [{"check_date": p.get("check_date"), "off_cycle": p.get("off_cycle"),
                           "uuid": p["payroll_uuid"]} for p in payrolls],
        "portal_inputs_by_class_code": report,
        "grand_total_gross_wages": round(sum(v["gross"] for v in by_code.values()), 2),
        "grand_total_overtime_pay": round(sum(v["ot"] for v in by_code.values()), 2),
        "per_payroll_gross_check": payroll_gross,
        "distinct_employees_paid": len(emps_paid),
        "exceptions": exceptions,
        "exceptions_count": len(exceptions),
        "ready_to_submit": len(exceptions) == 0,
    }
