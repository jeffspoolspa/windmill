# f/maintenance/pull_gusto_payweek — Gusto payrolls → maintenance.gusto_payweek
# Weekly processed payrolls: per-employee Regular/OT/DoubleOT + PTO hours.
# adj_min = reg + 1.5*OT + 2*DOT (cost-equivalent, matches parse_gusto.py).
# Defaults cover the last 45 days so the nightly run heals late-processed
# payrolls; idempotent upsert on (employee_id, payroll_uuid).
import time
from datetime import date, datetime, timedelta, timezone

import requests
import wmill
from supabase import create_client

API = "https://api.gusto.com"
HOURLY = {"Regular Hours": "reg_min", "Overtime": "ot_min",
          "Double overtime": "dot_min", "Double Overtime": "dot_min"}


def gget(url, h, params=None, tries=5):
    for i in range(tries):
        r = requests.get(url, headers=h, params=params, timeout=60)
        if r.status_code != 429:
            return r
        time.sleep(int(r.headers.get("Retry-After", "15")))
    return r


def main(p_start: str = "", p_end: str = ""):
    company_id = wmill.get_variable("f/gusto/company_id")
    token = wmill.get_variable("f/gusto/personal_access_token")
    h = {"Authorization": f"Bearer {token}", "X-Gusto-API-Version": "2025-06-15",
         "Accept": "application/json"}
    sb = create_client(wmill.get_variable("f/SUPABASE/URL"),
                       wmill.get_variable("f/SUPABASE/SERVICE_ROLE_KEY"))
    emp = {e["gusto_uuid"]: e["id"]
           for e in sb.table("employees").select("id,gusto_uuid")
                      .not_.is_("gusto_uuid", "null").execute().data}

    today = datetime.now(timezone(timedelta(hours=-4))).date()
    start = p_start or str(today - timedelta(days=45))
    end = p_end or str(today)

    r = gget(f"{API}/v1/companies/{company_id}/payrolls", h,
             {"start_date": start, "end_date": end, "processing_statuses": "processed"})
    r.raise_for_status()
    payrolls = r.json()

    rows, unmatched = [], set()
    for p in payrolls:
        puuid = p.get("payroll_uuid") or p.get("uuid")
        pp = p.get("pay_period", {})
        d = gget(f"{API}/v1/companies/{company_id}/payrolls/{puuid}", h)
        if d.status_code != 200:
            print(f"payroll {puuid}: {d.status_code}")
            continue
        for c in d.json().get("employee_compensations", []):
            eid = emp.get(c.get("employee_uuid"))
            if eid is None:
                unmatched.add(c.get("employee_uuid"))
                continue
            mins = {"reg_min": 0, "ot_min": 0, "dot_min": 0}
            for hc in c.get("hourly_compensations", []):
                col = HOURLY.get(hc.get("name"))
                if col:
                    mins[col] += round(float(hc.get("hours") or 0) * 60)
            pto = sum(round(float(t.get("hours") or 0) * 60)
                      for t in c.get("paid_time_off", []))
            if not any(mins.values()) and not pto:
                continue  # salaried/no-hours rows add noise, skip
            rows.append({"employee_id": eid, "payroll_uuid": puuid,
                         "period_start": pp.get("start_date"), "period_end": pp.get("end_date"),
                         **mins,
                         "adj_min": round(mins["reg_min"] + mins["ot_min"] * 1.5 + mins["dot_min"] * 2.0),
                         "pto_min": pto,
                         "updated_at": datetime.now(timezone.utc).isoformat()})
        time.sleep(0.15)

    for i in range(0, len(rows), 400):
        sb.schema("maintenance").table("gusto_payweek").upsert(rows[i:i + 400]).execute()
    return {"payrolls": len(payrolls), "rows": len(rows),
            "window": [start, end], "unmatched_gusto_uuids": len(unmatched)}
