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
    os.makedirs("./shared", exist_ok=True)
    open("./shared/wr.html", "w").write(r2.text)
    parsed = ion_parser.parse("./shared/wr.html", "service_log")
    norm = ion_normalize.normalize_rows(parsed, sb)
    rows = norm["canonical_rows"]
    sample_keys = list(rows[0].keys()) if rows else []
    wr = []
    for r in rows:
        blob = " ".join(str(v) for v in r.values()).upper()
        if "WINDING RIVER" in blob or "MEANDERING" in blob:
            wr.append(r)
    by_day = {}
    for r in wr:
        d = str(r.get("visit_date") or r.get("scheduled_date") or "?")
        by_day[d] = by_day.get(d, 0) + 1
    multi_days = {d: n for d, n in by_day.items() if n > 1}
    return {"total_canonical_rows": len(rows), "winding_river_rows": len(wr),
            "wr_distinct_days": len(by_day), "wr_days_with_multiple_rows": len(multi_days),
            "multi_days": dict(sorted(multi_days.items())), "sample_keys": sample_keys,
            "sample_wr_rows": wr[:5]}
