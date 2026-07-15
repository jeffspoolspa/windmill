import wmill, requests

def main():
    tok = (wmill.get_variable("f/samsara/api_token") or "").strip()
    BASE = "https://api.samsara.com"; H = {"Authorization": f"Bearer {tok}"}
    P = {"startDate": "2026-06-01T00:00:00Z", "endDate": "2026-06-30T23:59:59Z"}
    out = {}
    for nm, path in [("drv_fuel", "/fleet/reports/drivers/fuel-energy"),
                     ("veh_fuel", "/fleet/reports/vehicles/fuel-energy")]:
        rr = requests.get(f"{BASE}{path}", headers=H, params=P, timeout=60)
        out[nm] = {"status": rr.status_code, "body": rr.text[:700]}
    return out
