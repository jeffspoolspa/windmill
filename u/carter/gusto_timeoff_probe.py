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
    if company:
        r = requests.get(
            f"https://api.gusto.com/v1/companies/{company}/time_off_requests",
            headers=h,
            params={"start_date": start_date, "end_date": end_date},
        )
        out["time_off_status"] = r.status_code
        try:
            body = r.json()
        except ValueError:
            body = r.text
        # ponytail: cap sample at 3 requests, this is a scope probe not a fetch
        out["time_off_body"] = body[:3] if isinstance(body, list) else body
    return out
