# requirements:
# wmill
# requests
# beautifulsoup4
# psycopg2-binary

import wmill, json, os
import requests
import f.ION._lib.parser as ion_parser
import f.ION._lib.normalize as ion_normalize


def _cookie_header(cookies, ion_origin):
    host = ion_origin.replace("https://", "").replace("http://", "").split("/")[0]
    parts = []
    for c in cookies:
        d = (c.get("domain") or "").lstrip(".")
        if host == d or host.endswith("." + d):
            parts.append(f"{c['name']}={c['value']}")
    return "; ".join(parts)


def main():
    sb = wmill.get_resource("u/carter/supabase")
    s = json.loads(wmill.get_variable("f/ION/session_cache"))
    o = s["ionOrigin"]; cid = s.get("cfClientId") or ""
    h = {"Cookie": _cookie_header(s["cookies"], o), "User-Agent": "Mozilla/5.0", "Accept": "text/html, */*"}
    requests.get(f"{o}/reports/serviceLogs.cfm",
                 params={"office": "", "tech": "", "Start": "2026-05-01", "end": "2026-06-01", "set": "1",
                         "_cf_containerId": "rptDetail", "_cf_nodebug": "true", "_cf_nocache": "true",
                         "_cf_clientid": cid, "_cf_rc": "1"},
                 headers=h, allow_redirects=False, timeout=60)
    r2 = requests.get(f"{o}/reports/_xls/CompletedLogDetail.cfm", headers=h, allow_redirects=False, timeout=180)
    if r2.status_code != 200 or "<table" not in r2.text.lower():
        return {"ok": False, "status": r2.status_code, "note": "session likely stale (no report tables)"}
    os.makedirs("./shared", exist_ok=True)
    open("./shared/wr2.html", "w").write(r2.text)
    parsed = ion_parser.parse("./shared/wr2.html", "service_log")
    norm = ion_normalize.normalize_rows(parsed, sb)
    rows = norm["canonical_rows"]
    tally = {}
    raw = []
    for r in rows:
        v = r.get("visits", {}) or {}
        cust = (v.get("_customer_name") or "")
        if "WINDING RIVER" not in cust.upper():
            continue
        st = v.get("_service_type")
        price = v.get("price_cents")
        start = v.get("_start_time_str") or ""
        pool = (r.get("pools", {}) or {}).get("_pool_name")
        # AM/PM bucket from start time string
        half = "AM" if ("AM" in start.upper()) else ("PM" if "PM" in start.upper() else "?")
        key = f"{st} | ${ (price or 0)/100 } | {half}"
        tally[key] = tally.get(key, 0) + 1
        if len(raw) < 60:
            raw.append({"svc": st, "price": price, "start": start, "pool": pool, "date": v.get("visit_date")})
    return {"ok": True, "wr_rows": sum(tally.values()), "tally_by_service_price_half": dict(sorted(tally.items())), "sample": raw}
