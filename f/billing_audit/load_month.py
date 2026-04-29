
#extra_requirements:
#requests
#psycopg2-binary

import requests
import wmill
import psycopg2
import calendar
from datetime import date

LABOR_KEYWORDS = {
    "POOL MAINTENANCE": "PM",
    "FLAT RATE": "FR",
    "CHEMICAL TESTING": "CT",
    "SPA CLEAN": "SPA",
    "FOUNTAIN CLEAN": "FTN",
    "QUALITY CONTROL": "QC",
    "GREEN POOL": "GP",
    "HALF HOUR": "HH",
    "ONE TIME CLEAN": "OTC",
}


def derive_service_frequency(service_type, visit_count):
    """Derive service frequency tier from service_type and visit_count."""
    if service_type == "FR":
        return "flat_rate"
    if service_type == "OTC" or service_type == "HH+OTC":
        return "one_time"
    if service_type in ("GP", "GP+HH"):
        return "green_pool"
    if visit_count is None:
        return "unknown"
    if visit_count <= 1.5:
        return "monthly"
    if visit_count <= 3.5:
        return "biweekly"
    if visit_count <= 7.0:
        return "weekly"
    if visit_count <= 10.5:
        return "2x_weekly"
    return "high_freq"


def refresh_qbo_token():
    resource_path = "u/carter/quickbooks_api"
    resource = wmill.get_resource(resource_path)
    response = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"])
    )
    if not response.ok:
        raise Exception(f"Token refresh failed: {response.status_code} - {response.text}")
    tokens = response.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(path=resource_path, value=resource)
    return tokens["access_token"], resource["realm_id"]


def qbo_query(access_token, realm_id, query):
    base_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    all_results = []
    start_pos = 1
    page_size = 1000
    while True:
        paged_query = f"{query} STARTPOSITION {start_pos} MAXRESULTS {page_size}"
        response = requests.get(base_url, headers=headers, params={"query": paged_query})
        if not response.ok:
            raise Exception(f"QBO query failed: {response.status_code} - {response.text}")
        qr = response.json().get("QueryResponse", {})
        invoices = qr.get("Invoice", [])
        all_results.extend(invoices)
        total = qr.get("totalCount", len(invoices))
        if start_pos + page_size - 1 >= total or len(invoices) < page_size:
            break
        start_pos += page_size
    return all_results


def classify_invoice(inv):
    """Classify invoice line items. Returns dict with is_maintenance based on labor SKU presence.
    
    Visit count = SUM of quantities across ALL labor line items, excluding discounts.
    This correctly handles invoices that break out visits as separate line items.
    """
    lines = inv.get("Line", [])
    labor_types = set()
    labor_visit_sum = 0.0  # Accumulate total labor visits
    has_countable_labor = False  # Track if we found any countable labor lines
    per_visit_rate = None
    line_items = []

    for line in lines:
        if line.get("DetailType") != "SalesItemLineDetail":
            continue
        si = line.get("SalesItemLineDetail", {})
        item_ref = si.get("ItemRef", {})
        item_name = item_ref.get("name", "")
        item_id = item_ref.get("value", "")
        qty = float(si.get("Qty", 0))
        unit_price = float(si.get("UnitPrice", 0))
        amount = float(line.get("Amount", 0))
        desc = line.get("Description", "")
        upper = item_name.upper()

        matched_type = None
        for keyword, code in LABOR_KEYWORDS.items():
            if keyword in upper:
                matched_type = code
                labor_types.add(code)
                if code in ("PM", "CT", "SPA", "FTN", "QC", "OTC"):
                    # Exclude discount lines from visit count
                    if "DISCOUNT" not in upper and "CHEM CHECK" not in upper:
                        labor_visit_sum += qty
                        has_countable_labor = True
                        if per_visit_rate is None:
                            per_visit_rate = unit_price
                elif code == "FR":
                    if per_visit_rate is None:
                        per_visit_rate = unit_price
                elif code == "HH":
                    # HH lines: count visits but don't set per_visit_rate from HH
                    if "DISCOUNT" not in upper:
                        labor_visit_sum += qty
                        has_countable_labor = True
                elif code == "GP":
                    # GP lines: count visits
                    if "DISCOUNT" not in upper:
                        labor_visit_sum += qty
                        has_countable_labor = True
                        if per_visit_rate is None:
                            per_visit_rate = unit_price
                break

        if matched_type:
            line_type = "labor"
        elif "CC FEE" in upper or "LATE FEE" in upper:
            line_type = "fee"
        elif "MISCELLANEOUS" in upper:
            line_type = "other"
        elif "DISCOUNT" in upper or amount < 0:
            line_type = "adjustment"
        else:
            line_type = "chemical"

        line_items.append({
            "qbo_item_id": item_id, "item_name": item_name, "description": desc,
            "quantity": qty, "unit_price": unit_price, "amount": amount, "line_type": line_type,
        })

    service_type = "+".join(sorted(labor_types)) if labor_types else None
    has_labor_sku = service_type is not None
    
    # Visit count: use the accumulated sum, or None for FR / no-labor invoices
    visit_count = labor_visit_sum if has_countable_labor else None
    # FR explicitly gets no visit count
    if service_type == "FR":
        visit_count = None

    chemical_total = sum(li["amount"] for li in line_items if li["line_type"] == "chemical")
    chem_per_visit = None
    if visit_count and visit_count > 0 and chemical_total > 0:
        chem_per_visit = round(chemical_total / visit_count, 2)

    # Derive service frequency from service_type and visit_count
    service_frequency = derive_service_frequency(service_type, visit_count) if service_type else None

    return {
        "qbo_invoice_id": inv.get("Id"),
        "doc_number": inv.get("DocNumber"),
        "invoice_date": inv.get("TxnDate"),
        "qbo_customer_id": inv.get("CustomerRef", {}).get("value"),
        "customer_name": inv.get("CustomerRef", {}).get("name"),
        "service_type": service_type,
        "visit_count": visit_count,
        "per_visit_rate": per_visit_rate,
        "invoice_total": float(inv.get("TotalAmt", 0)),
        "chemical_total": round(chemical_total, 2),
        "chem_per_visit": chem_per_visit,
        "line_item_count": len(line_items),
        "line_items": line_items,
        "has_labor_sku": has_labor_sku,
        "is_maintenance": has_labor_sku,
        "service_frequency": service_frequency,
    }


def get_db_conn():
    supabase = wmill.get_resource("u/carter/supabase")
    return psycopg2.connect(
        host=supabase.get("host"), port=supabase.get("port", 6543),
        dbname=supabase.get("dbname", "postgres"), user=supabase.get("user"),
        password=supabase.get("password"), sslmode=supabase.get("sslmode", "require"),
    )


def get_maintenance_customer_ids(conn):
    """Get set of qbo_customer_ids flagged as maintenance in Customers table."""
    cur = conn.cursor()
    cur.execute('SELECT qbo_customer_id FROM public."Customers" WHERE is_maintenance = true')
    ids = {row[0] for row in cur.fetchall()}
    cur.close()
    return ids


def get_consumable_whitelist(conn):
    """Get set of qbo_item_ids that are confirmed consumables from maint invoices."""
    cur = conn.cursor()
    cur.execute('SELECT qbo_item_id FROM billing_audit.consumable_items')
    ids = {row[0] for row in cur.fetchall()}
    cur.close()
    return ids


def update_consumable_whitelist(conn, confirmed_invoices, billing_month_date):
    """Upsert chemical line items from confirmed maint invoices into the whitelist."""
    if not confirmed_invoices:
        return 0
    cur = conn.cursor()
    new_count = 0
    for inv in confirmed_invoices:
        for li in inv["line_items"]:
            if li["line_type"] != "chemical":
                continue
            cur.execute("""
                INSERT INTO billing_audit.consumable_items 
                    (qbo_item_id, item_name, occurrences, first_seen_month, last_seen_month, updated_at)
                VALUES (%s, %s, 1, %s, %s, NOW())
                ON CONFLICT (qbo_item_id) DO UPDATE SET
                    occurrences = billing_audit.consumable_items.occurrences + 1,
                    last_seen_month = GREATEST(billing_audit.consumable_items.last_seen_month, EXCLUDED.last_seen_month),
                    first_seen_month = LEAST(billing_audit.consumable_items.first_seen_month, EXCLUDED.first_seen_month),
                    updated_at = NOW()
            """, (li["qbo_item_id"], li["item_name"], billing_month_date, billing_month_date))
            if cur.statusmessage == 'INSERT 0 1':
                new_count += 1
    conn.commit()
    cur.close()
    return new_count


def flag_maintenance_customers(conn, qbo_customer_ids):
    """Set is_maintenance=true for customers who had labor-SKU invoices."""
    if not qbo_customer_ids:
        return 0
    cur = conn.cursor()
    cur.execute("""
        UPDATE public."Customers" SET is_maintenance = true
        WHERE qbo_customer_id = ANY(%s) AND is_maintenance = false
    """, (list(qbo_customer_ids),))
    updated = cur.rowcount
    conn.commit()
    cur.close()
    return updated


def insert_month(conn, billing_month_date, maint_invoices):
    """Insert maintenance invoices + line items into Supabase."""
    if not maint_invoices:
        return {"inserted_invoices": 0, "inserted_line_items": 0}

    cur = conn.cursor()
    inv_count = 0
    li_count = 0

    for inv in maint_invoices:
        cur.execute("""
            INSERT INTO billing_audit.maintenance_invoices (
                qbo_invoice_id, doc_number, billing_month, invoice_date,
                qbo_customer_id, customer_name, service_type,
                visit_count, per_visit_rate, invoice_total,
                chemical_total, chem_per_visit, line_item_count,
                service_frequency
            ) VALUES (
                %(qbo_invoice_id)s, %(doc_number)s, %(billing_month)s, %(invoice_date)s,
                %(qbo_customer_id)s, %(customer_name)s, %(service_type)s,
                %(visit_count)s, %(per_visit_rate)s, %(invoice_total)s,
                %(chemical_total)s, %(chem_per_visit)s, %(line_item_count)s,
                %(service_frequency)s
            ) RETURNING id
        """, {
            "qbo_invoice_id": inv["qbo_invoice_id"],
            "doc_number": inv["doc_number"],
            "billing_month": billing_month_date.isoformat(),
            "invoice_date": inv["invoice_date"],
            "qbo_customer_id": inv["qbo_customer_id"],
            "customer_name": inv["customer_name"],
            "service_type": inv["service_type"],
            "visit_count": inv["visit_count"],
            "per_visit_rate": inv["per_visit_rate"],
            "invoice_total": inv["invoice_total"],
            "chemical_total": inv["chemical_total"],
            "chem_per_visit": inv["chem_per_visit"],
            "line_item_count": inv["line_item_count"],
            "service_frequency": inv["service_frequency"],
        })
        invoice_id = cur.fetchone()[0]
        inv_count += 1

        for li in inv["line_items"]:
            cur.execute("""
                INSERT INTO billing_audit.maintenance_invoice_line_items (
                    invoice_id, qbo_item_id, item_name, description,
                    quantity, unit_price, amount, line_type
                ) VALUES (
                    %(invoice_id)s, %(qbo_item_id)s, %(item_name)s, %(description)s,
                    %(quantity)s, %(unit_price)s, %(amount)s, %(line_type)s
                )
            """, {**li, "invoice_id": invoice_id})
            li_count += 1

    conn.commit()
    cur.close()
    return {"inserted_invoices": inv_count, "inserted_line_items": li_count}


def main(billing_month: str = "2025-11"):
    year, month = map(int, billing_month.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    billing_month_date = date(year, month, 1)
    invoice_date_str = f"{year}-{month:02d}-{last_day:02d}"

    conn = get_db_conn()

    # Idempotency check
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM billing_audit.maintenance_invoices WHERE billing_month = %s",
        (billing_month_date.isoformat(),)
    )
    existing = cur.fetchone()[0]
    cur.close()
    if existing > 0:
        conn.close()
        return {"status": "already_loaded", "billing_month": billing_month, "existing_count": existing}

    # Load known maintenance customer IDs and consumable whitelist
    known_maint_ids = get_maintenance_customer_ids(conn)
    consumable_ids = get_consumable_whitelist(conn)

    # Pull invoices from QBO
    access_token, realm_id = refresh_qbo_token()
    query = f"SELECT * FROM Invoice WHERE TxnDate = '{invoice_date_str}'"
    raw_invoices = qbo_query(access_token, realm_id, query)

    if not raw_invoices:
        conn.close()
        return {"status": "no_invoices", "billing_month": billing_month, "query_date": invoice_date_str}

    # Classify all invoices
    classified = [classify_invoice(inv) for inv in raw_invoices]

    # Rescue chemical-delivery invoices from known maint customers.
    # Requirements: (1) no labor SKU, (2) customer is flagged as maint,
    # (3) ALL line items exist in the consumable_items whitelist.
    customer_flag_rescued = []
    rescue_skipped = []
    for inv in classified:
        if not inv["has_labor_sku"] and inv["qbo_customer_id"] in known_maint_ids:
            # Check every line item against the consumable whitelist
            inv_item_ids = {li["qbo_item_id"] for li in inv["line_items"]}
            unknown_items = inv_item_ids - consumable_ids
            if not unknown_items and inv_item_ids:
                inv["is_maintenance"] = True
                inv["service_type"] = "CHEM_ONLY"
                inv["service_frequency"] = "unknown"
                customer_flag_rescued.append(inv["customer_name"])
            else:
                rescue_skipped.append({
                    "customer": inv["customer_name"],
                    "doc": inv["doc_number"],
                    "total": inv["invoice_total"],
                    "unknown_items": [
                        li["item_name"] for li in inv["line_items"]
                        if li["qbo_item_id"] in unknown_items
                    ],
                })

    maint = [c for c in classified if c["is_maintenance"]]
    service = [c for c in classified if not c["is_maintenance"]]

    # Separate confirmed (has labor SKU) from rescued for whitelist update
    confirmed = [c for c in maint if c["has_labor_sku"]]

    # Insert maintenance invoices
    counts = insert_month(conn, billing_month_date, maint)

    # Update consumable whitelist with new items from confirmed invoices
    new_consumables = update_consumable_whitelist(conn, confirmed, billing_month_date)

    # Auto-flag customers who had labor-SKU invoices this month
    labor_customer_ids = {
        c["qbo_customer_id"] for c in classified if c["has_labor_sku"]
    }
    newly_flagged = flag_maintenance_customers(conn, labor_customer_ids)

    conn.close()

    # Build summary
    service_type_counts = {}
    freq_counts = {}
    for inv in maint:
        st = inv["service_type"]
        service_type_counts[st] = service_type_counts.get(st, 0) + 1
        sf = inv.get("service_frequency", "unknown")
        freq_counts[sf] = freq_counts.get(sf, 0) + 1

    labor_revenue = round(sum(
        li["amount"] for inv in maint for li in inv["line_items"] if li["line_type"] == "labor"
    ), 2)
    chemical_revenue = round(sum(
        li["amount"] for inv in maint for li in inv["line_items"] if li["line_type"] == "chemical"
    ), 2)
    fee_revenue = round(sum(
        li["amount"] for inv in maint for li in inv["line_items"] if li["line_type"] == "fee"
    ), 2)
    adjustment_total = round(sum(
        li["amount"] for inv in maint for li in inv["line_items"] if li["line_type"] == "adjustment"
    ), 2)

    return {
        "status": "loaded",
        "billing_month": billing_month,
        "query_date": invoice_date_str,
        "total_invoices_from_qbo": len(raw_invoices),
        "maintenance_count": len(maint),
        "service_count": len(service),
        "unique_customers": len(set(c["qbo_customer_id"] for c in maint)),
        "service_type_breakdown": service_type_counts,
        "frequency_breakdown": freq_counts,
        "labor_revenue": labor_revenue,
        "chemical_revenue": chemical_revenue,
        "fee_revenue": fee_revenue,
        "adjustment_total": adjustment_total,
        "total_revenue": round(labor_revenue + chemical_revenue + fee_revenue + adjustment_total, 2),
        "customers_newly_flagged": newly_flagged,
        "customer_flag_rescued": customer_flag_rescued,
        "rescue_skipped": rescue_skipped,
        "new_consumable_items": new_consumables,
        "consumable_whitelist_size": len(consumable_ids),
        **counts,
        "service_invoice_samples": [
            {"doc": s["doc_number"], "customer": s["customer_name"], "total": s["invoice_total"],
             "items": [li["item_name"] for li in s["line_items"][:3]]}
            for s in service[:5]
        ],
    }
