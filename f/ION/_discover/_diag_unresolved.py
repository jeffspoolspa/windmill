# requirements:
# requests
# beautifulsoup4
# psycopg2-binary
# wmill
import json, os
import requests, wmill
import f.ION._lib.parser as P
import f.ION._lib.normalize as N
import f.ION._lib.upsert as U


def main(start_date: str = "2026-05-01", end_date: str = "2026-06-01"):
    sb = wmill.get_resource("u/carter/supabase")
    sess = wmill.get_variable("f/ION/session_cache")
    if isinstance(sess, str):
        sess = json.loads(sess)
    origin = sess["ionOrigin"]
    host = origin.split("//")[1].split("/")[0]
    parts = []
    for c in sess["cookies"]:
        dmn = (c.get("domain") or "").lstrip(".")
        if host == dmn or host.endswith("." + dmn):
            parts.append(f"{c['name']}={c['value']}")
    H = {"Cookie": "; ".join(parts), "User-Agent": "Mozilla/5.0", "Accept": "text/html, */*"}

    requests.get(f"{origin}/reports/serviceLogs.cfm", params={
        "office": "", "tech": "", "Start": start_date, "end": end_date, "set": "1",
        "_cf_containerId": "rptDetail", "_cf_nodebug": "true", "_cf_nocache": "true",
        "_cf_clientid": sess.get("cfClientId", ""), "_cf_rc": "1"}, headers=H, allow_redirects=False, timeout=60)
    r = requests.get(f"{origin}/reports/_xls/CompletedLogDetail.cfm", headers=H, allow_redirects=False, timeout=180)
    os.makedirs("./shared", exist_ok=True)
    with open("./shared/m.html", "w") as f:
        f.write(r.text)

    parsed = P.parse("./shared/m.html", "service_log")
    norm = N.normalize_rows(parsed, sb)
    conn = U._connect(sb)
    res = U.build_resolvers(conn)

    # active sls grouped by normalized address -> [(sl_id, display_name)]
    addr_names = {}
    with conn.cursor() as cur:
        cur.execute("""SELECT sl.id, sl.street, c.display_name
                       FROM public.service_locations sl JOIN public."Customers" c ON c.id=sl.account_id
                       WHERE sl.is_active""")
        for sl_id, street, dn in cur.fetchall():
            na = U.normalize_address(street or "")
            if na:
                addr_names.setdefault(na, []).append((sl_id, dn))

    unresolved = {}
    resolved = 0
    for row in norm["canonical_rows"]:
        v = row.get("visits", {}) or {}
        primary = v.get("_address2") or v.get("_address1")
        sl = U.resolve_service_location_id(res, primary, v.get("_customer_name"))
        if sl is None and v.get("_address1") and primary == v.get("_address2"):
            sl = U.resolve_service_location_id(res, v.get("_address1"), v.get("_customer_name"))
        if sl is not None:
            resolved += 1
            continue
        na = U.normalize_address(primary or "")
        key = (v.get("_customer_name"), primary)
        if key not in unresolved:
            cands = addr_names.get(na, [])
            unresolved[key] = {
                "customer": v.get("_customer_name"), "addr1": v.get("_address1"),
                "addr2": v.get("_address2"), "city": v.get("_city"),
                "n_addr": na, "n_name": U.normalize_customer_name(v.get("_customer_name") or ""),
                "active_sls_at_addr": [{"sl": s, "name": d, "n_name": U.normalize_customer_name(d or "")} for s, d in cands],
                "count": 0,
            }
        unresolved[key]["count"] += 1
    conn.close()
    rows = sorted(unresolved.values(), key=lambda x: -x["count"])
    return {"total_rows": len(norm["canonical_rows"]), "resolved": resolved,
            "distinct_unresolved": len(rows), "unresolved": rows}
