# requirements:
# requests
# beautifulsoup4
# psycopg2-binary

"""
f/ION/_discover/diagnose_unresolved

For each ION row that didn't resolve to a service_location_id, run nearest-
neighbor lookups against public."Customers" and public.service_locations to
classify why and surface the closest matches.

Output (per unresolved row):
    {
      ion: { customer, address1, address2, city },
      classification: "address_typo" | "name_typo" | "multi_tenant" | "truly_missing",
      candidates: [
        { sl_id, db_customer, db_street, db_city, score_explanation }, ...
      ]
    }

Usage: pass an IonSession + supabase_connection.
"""

import re
from collections import defaultdict
from datetime import date

import psycopg2
import psycopg2.extras
import requests

import f.ION._lib.parser as ion_parser
import f.ION._lib.normalize as ion_normalize
from f.ION._lib.upsert import (
    normalize_address,
    normalize_customer_name,
    resolve_service_location_id,
    build_resolvers,
)


def _cookie_header(cookies, ion_origin):
    host = ion_origin.replace("https://", "").replace("http://", "").split("/")[0]
    parts = []
    for c in cookies:
        domain = (c.get("domain") or "").lstrip(".")
        if host == domain or host.endswith("." + domain):
            parts.append(f"{c['name']}={c['value']}")
    return "; ".join(parts)


def _connect(sb):
    return psycopg2.connect(
        host=sb["host"], port=sb["port"], dbname=sb["dbname"],
        user=sb["user"], password=sb["password"], connect_timeout=15,
    )


def _normalized_token_set(s):
    if not s:
        return set()
    n = re.sub(r"[^A-Z0-9 ]", " ", s.upper())
    n = re.sub(r"\s+", " ", n).strip()
    return set(t for t in n.split(" ") if len(t) >= 3)


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


def classify_and_find(conn, customer, addr1, addr2, city, resolvers):
    """For one unresolved row, search DB for plausible matches."""
    primary_addr = addr2 or addr1 or ""
    n_addr = normalize_address(primary_addr)
    n_name = normalize_customer_name(customer or "")

    # 1. Is the EXACT primary_addr present in DB at all?
    addr_candidates = resolvers["sl_by_addr_only"].get(n_addr, [])

    # 2. Find name candidates by token overlap (Customers)
    target_tokens = _normalized_token_set(customer or "")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.id, c.display_name, sl.id, sl.street, sl.city
            FROM public."Customers" c
            JOIN public.service_locations sl ON sl.account_id = c.id
            WHERE sl.is_active
        """)
        all_pairs = cur.fetchall()
    name_matches = []
    for c_id, db_name, sl_id, db_street, db_city in all_pairs:
        if not db_name:
            continue
        score = jaccard(target_tokens, _normalized_token_set(db_name))
        if score >= 0.5:
            name_matches.append({
                "sl_id": sl_id,
                "db_customer": db_name,
                "db_street": db_street,
                "db_city": db_city,
                "name_jaccard": round(score, 2),
            })
    name_matches.sort(key=lambda x: -x["name_jaccard"])

    # 3. Address LIKE candidates — same first 3 number+street tokens
    addr_first_tokens = " ".join((n_addr or "").split(" ")[:3])
    addr_matches = []
    if addr_first_tokens:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.display_name, sl.id, sl.street, sl.city
                FROM public.service_locations sl
                JOIN public."Customers" c ON c.id = sl.account_id
                WHERE upper(sl.street) LIKE %s
                LIMIT 8
            """, (f"%{addr_first_tokens}%",))
            for db_name, sl_id, db_street, db_city in cur.fetchall():
                addr_matches.append({
                    "sl_id": sl_id,
                    "db_customer": db_name,
                    "db_street": db_street,
                    "db_city": db_city,
                })

    # Classify
    if len(addr_candidates) >= 2 and not name_matches:
        classification = "multi_tenant_no_name_match"
    elif len(addr_candidates) >= 2 and name_matches:
        classification = "multi_tenant_name_close_but_not_exact"
    elif name_matches and not addr_candidates:
        classification = "name_match_but_address_differs"
    elif addr_matches and not name_matches:
        classification = "address_close_but_no_name_match"
    elif name_matches and addr_matches:
        classification = "soft_match_both_name_and_address"
    else:
        classification = "truly_missing"

    return {
        "ion": {
            "customer": customer,
            "address1": addr1,
            "address2": addr2,
            "city": city,
        },
        "classification": classification,
        "exact_addr_candidates_count": len(addr_candidates),
        "top_name_matches": name_matches[:3],
        "top_address_matches": addr_matches[:3],
    }


def main(ion_session, supabase_connection, lookback_days=30):
    ion_origin = ion_session["ionOrigin"]
    cf_client_id = ion_session.get("cfClientId") or ""
    headers = {
        "Cookie": _cookie_header(ion_session["cookies"], ion_origin),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html, */*",
    }

    # Fetch + parse + normalize (skip upsert)
    start_str = date.fromordinal(date.today().toordinal() - lookback_days).strftime("%Y-%m-%d")
    picker_url = f"{ion_origin}/reports/serviceLogs.cfm"
    requests.get(picker_url, params={
        "office": "", "tech": "", "Start": start_str, "end": "", "set": "1",
        "_cf_containerId": "rptDetail", "_cf_nodebug": "true",
        "_cf_nocache": "true", "_cf_clientid": cf_client_id, "_cf_rc": "1",
    }, headers=headers, allow_redirects=False, timeout=60)
    r = requests.get(f"{ion_origin}/reports/_xls/CompletedLogDetail.cfm",
                     headers=headers, allow_redirects=False, timeout=180)
    if r.status_code != 200:
        return {"ok": False, "stage": "data", "status": r.status_code}

    import os
    os.makedirs("./shared", exist_ok=True)
    with open("./shared/completed_log_detail.html", "w") as f:
        f.write(r.text)

    parsed = ion_parser.parse("./shared/completed_log_detail.html", "service_log")
    norm = ion_normalize.normalize_rows(parsed, supabase_connection)

    # Resolve every row, dedupe failures by (customer, addr2)
    conn = _connect(supabase_connection)
    try:
        resolvers = build_resolvers(conn)
        unresolved_keyed = {}  # (cust, addr2) -> sample row
        for row in norm["canonical_rows"]:
            v = row.get("visits", {}) or {}
            primary_addr = v.get("_address2") or v.get("_address1")
            sl_id = resolve_service_location_id(resolvers, primary_addr, v.get("_customer_name"))
            if sl_id is not None:
                continue
            # also try fallback address
            if v.get("_address2") and v.get("_address1") and v.get("_address1") != v.get("_address2"):
                if resolve_service_location_id(resolvers, v.get("_address1"), v.get("_customer_name")):
                    continue
            key = (v.get("_customer_name"), v.get("_address2") or v.get("_address1"))
            if key not in unresolved_keyed:
                unresolved_keyed[key] = v

        # Diagnose each unique unresolved row
        diagnoses = []
        for (cust, addr), v in unresolved_keyed.items():
            d = classify_and_find(
                conn, cust, v.get("_address1"), v.get("_address2"),
                v.get("_city"), resolvers,
            )
            diagnoses.append(d)
    finally:
        conn.close()

    # Group by classification
    by_class = defaultdict(int)
    for d in diagnoses:
        by_class[d["classification"]] += 1

    diagnoses.sort(key=lambda d: d["classification"])

    return {
        "total_canonical_rows": len(norm["canonical_rows"]),
        "unique_unresolved": len(diagnoses),
        "by_classification": dict(by_class),
        "details": diagnoses,
    }
