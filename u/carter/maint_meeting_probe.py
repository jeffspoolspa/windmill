import wmill, requests

def main():
    out = {"scope_ok": True}  # running at all proves jobs:run via runScriptByPath

    # ---- SAMSARA ----
    try:
        stok = wmill.get_variable("f/samsara/api_token")
        out["samsara_token_len"] = len(stok) if stok else 0
        r = requests.get("https://api.samsara.com/fleet/drivers",
                         headers={"Authorization": f"Bearer {stok}"}, params={"limit": 3})
        out["samsara_drivers_status"] = r.status_code
        try:
            j = r.json()
            data = j.get("data", j if isinstance(j, list) else [])
            out["samsara_driver_sample"] = [{k: d.get(k) for k in ("id", "name")} for d in data[:3]]
            out["samsara_pagination"] = j.get("pagination")
        except Exception:
            out["samsara_body"] = r.text[:400]
    except Exception as e:
        out["samsara_error"] = str(e)

    # ---- GUSTO time_off (call-outs) ----
    try:
        gtok = wmill.get_variable("f/gusto/personal_access_token")
        gco = wmill.get_variable("f/gusto/company_id")
        H = {"Authorization": f"Bearer {gtok}", "X-Gusto-API-Version": "2024-04-01", "Accept": "application/json"}
        r2 = requests.get(f"https://api.gusto.com/v1/companies/{gco}/time_off_requests", headers=H)
        out["gusto_timeoff_status"] = r2.status_code
        if r2.status_code == 200:
            d = r2.json()
            out["gusto_timeoff_count"] = len(d)
            if d:
                out["gusto_timeoff_keys"] = list(d[0].keys())
                emp = d[0].get("employee") or {}
                out["gusto_emp_keys"] = list(emp.keys()) if isinstance(emp, dict) else str(type(emp))
        else:
            out["gusto_timeoff_body"] = r2.text[:300]
    except Exception as e:
        out["gusto_error"] = str(e)

    return out
