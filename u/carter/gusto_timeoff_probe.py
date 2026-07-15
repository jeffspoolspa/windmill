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
        emps = get(
            "employees",
            f"https://api.gusto.com/v1/companies/{company}/employees",
            per=3,
        )
        if emps:
            e = emps[0]["uuid"]
            out["probe_employee"] = f"{emps[0].get('first_name')} {emps[0].get('last_name')}"
            for t in ("vacation", "sick"):
                get(
                    f"time_off_activities_{t}",
                    f"https://api.gusto.com/v1/employees/{e}/time_off_activities",
                    time_off_type=t,
                )
    return out
