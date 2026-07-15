import requests
import wmill


def main(start_date: str = "2026-06-01", end_date: str = "2026-06-30"):
    token = wmill.get_variable("f/gusto/personal_access_token")
    h = {
        "Authorization": f"Bearer {token}",
        "X-Gusto-API-Version": "2025-06-15",
    }
    info = requests.get("https://api.gusto.com/v1/token_info", headers=h)
    out = {"token_info_status": info.status_code}
    try:
        ti = info.json()
    except ValueError:
        ti = info.text
    out["token_info"] = ti

    company = None
    if isinstance(ti, dict):
        company = (ti.get("resource") or {}).get("uuid")
    def get(label, url, **params):
        r = requests.get(url, headers=h, params=params or None)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        # ponytail: cap list samples at 3, this is a scope probe not a fetch
        out[label] = {
            "status": r.status_code,
            "body": body[:3] if isinstance(body, list) else body,
        }
        return body if r.ok else None

    if company:
        get(
            "time_off_requests",
            f"https://api.gusto.com/v1/companies/{company}/time_off_requests",
            start_date=start_date,
            end_date=end_date,
        )
        get(
            "report_template_payroll_journal",
            f"https://api.gusto.com/v1/companies/{company}/report_templates/payroll_journal",
        )
        # scoped to the June maint-meeting roster (Brunswick/Saint Marys techs
        # from build_june.py) — the exact population the call-outs KPI covers
        ROSTER = {
            "jayden hinson", "jamie teston", "damian elmore", "aaron newbauer",
            "carlos vaquerano", "joshua carroll", "ernie stegall",
            "travis redmon", "korey felts", "joshua francis", "jackson morey",
            "emmanuel thornton",
        }
        emps, page = [], 1
        while True:
            r = requests.get(
                f"https://api.gusto.com/v1/companies/{company}/employees",
                headers=h,
                params={"page": page, "per": 100},
            )
            batch = r.json() if r.ok else []
            emps += batch
            if len(batch) < 100:
                break
            page += 1
        roster_emps = [
            e for e in emps
            if any(f"{e.get('first_name','')} {e.get('last_name','')}".lower().startswith(n)
                   for n in ROSTER)
        ]
        out["roster_matched"] = len(roster_emps)
        hits = {}
        for emp in roster_emps:
            for t in ("vacation", "sick"):
                r = requests.get(
                    f"https://api.gusto.com/v1/employees/{emp['uuid']}/time_off_activities",
                    headers=h,
                    params={"time_off_type": t},
                )
                body = r.json() if r.ok else []
                if body:
                    name = f"{emp.get('first_name')} {emp.get('last_name')}"
                    # dates + event types only — probing shape, not pulling records
                    hits.setdefault(name, {})[t] = [
                        {k: a.get(k) for k in ("effective_time", "event_type", "time_off_type")}
                        for a in body
                    ][:10]
        out["activity_hits"] = hits
    return out
