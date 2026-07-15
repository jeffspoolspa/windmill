import wmill, requests

def main():
    out = {}

    # ---- SAMSARA: try US + EU, stripped token ----
    stok = (wmill.get_variable("f/samsara/api_token") or "").strip()
    out["samsara_token_len"] = len(stok)
    for label, base in [("us", "https://api.samsara.com"), ("eu", "https://api.eu.samsara.com")]:
        try:
            r = requests.get(f"{base}/fleet/drivers",
                             headers={"Authorization": f"Bearer {stok}"}, params={"limit": 3}, timeout=20)
            info = {"status": r.status_code}
            if r.status_code == 200:
                j = r.json()
                data = j.get("data", [])
                info["driver_sample"] = [{k: d.get(k) for k in ("id", "name")} for d in data[:3]]
                info["has_more"] = (j.get("pagination") or {}).get("hasNextPage")
            else:
                info["body"] = r.text[:250]
            out[f"samsara_{label}"] = info
        except Exception as e:
            out[f"samsara_{label}"] = {"error": str(e)[:200]}

    # ---- GUSTO time_off (call-outs) — corrected API version ----
    gtok = wmill.get_variable("f/gusto/personal_access_token")
    gco = wmill.get_variable("f/gusto/company_id")
    H = {"Authorization": f"Bearer {gtok}", "X-Gusto-API-Version": "2025-06-15", "Accept": "application/json"}
    r2 = requests.get(f"https://api.gusto.com/v1/companies/{gco}/time_off_requests", headers=H, timeout=30)
    out["gusto_timeoff_status"] = r2.status_code
    if r2.status_code == 200:
        d = r2.json()
        out["gusto_timeoff_count"] = len(d)
        if d:
            out["gusto_timeoff_keys"] = list(d[0].keys())
            emp = d[0].get("employee") or {}
            out["gusto_emp_keys"] = list(emp.keys()) if isinstance(emp, dict) else str(type(emp))
            out["gusto_sample"] = {k: v for k, v in d[0].items() if k not in ("employee",)}
    else:
        out["gusto_timeoff_body"] = r2.text[:300]
    return out
