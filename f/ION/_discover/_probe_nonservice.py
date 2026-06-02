# requirements:
# wmill
# requests
# beautifulsoup4
# psycopg2-binary

import wmill, json, os
import requests
import f.ION._lib.parser as ion_parser


def _cookie_header(cookies, ion_origin):
    host = ion_origin.replace("https://", "").replace("http://", "").split("/")[0]
    parts = []
    for c in cookies:
        d = (c.get("domain") or "").lstrip(".")
        if host == d or host.endswith("." + d):
            parts.append(f"{c['name']}={c['value']}")
    return "; ".join(parts)


def main():
    s = json.loads(wmill.get_variable("f/ION/session_cache"))
    o = s["ionOrigin"]; cid = s.get("cfClientId") or ""
    h = {"Cookie": _cookie_header(s["cookies"], o), "User-Agent": "Mozilla/5.0", "Accept": "text/html, */*"}
    requests.get(f"{o}/reports/serviceLogs.cfm",
                 params={"office": "", "tech": "", "Start": "2026-05-24", "end": "2026-05-26", "set": "1",
                         "_cf_containerId": "rptDetail", "_cf_nodebug": "true", "_cf_nocache": "true",
                         "_cf_clientid": cid, "_cf_rc": "1"},
                 headers=h, allow_redirects=False, timeout=60)
    r2 = requests.get(f"{o}/reports/_xls/CompletedLogDetail.cfm", headers=h, allow_redirects=False, timeout=120)
    if "<table" not in r2.text.lower():
        return {"ok": False, "note": "session stale / no tables", "status": r2.status_code}
    os.makedirs("./shared", exist_ok=True)
    open("./shared/ns.html", "w").write(r2.text)
    parsed = ion_parser.parse("./shared/ns.html", "service_log")
    rows = parsed.get("rows", [])
    # columns present
    cols = list(rows[0].keys()) if rows else []
    # find any row whose any value mentions SERVICEABLE / HOLIDAY
    flagged = []
    wr = []
    for r in rows:
        blob = " ".join(str(v) for v in r.values()).upper()
        if "SERVICEABLE" in blob or "NOT SERVIC" in blob:
            flagged.append(r)
        if "WINDING RIVER" in blob or "MEANDERING" in blob:
            wr.append(r)
    return {"ok": True, "row_count": len(rows), "columns": cols,
            "n_with_serviceable_text": len(flagged),
            "sample_serviceable_rows": flagged[:3],
            "sample_winding_river_rows": wr[:4]}
