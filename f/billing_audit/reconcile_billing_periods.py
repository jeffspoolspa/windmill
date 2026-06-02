# requirements:
# psycopg2-binary

"""
f/billing_audit/reconcile_billing_periods

Reconcile the write-ahead promises (billing_audit.task_billing_periods) against
the ACTUAL QBO maintenance invoices, and stamp the result back on each promise.

Match rule (confirmed with Carter + empirically, 2026-06-02):
  - A maintenance invoice is DATED THE LAST DAY OF THE MONTH and carries a
    maintenance-LABOR line. LABOR services = POOL MAINTENANCE* / HALF HOUR
    MAINTENANCE / FLAT RATE / CHEMICAL TESTING / GREEN POOL / SPA CLEAN /
    QUALITY CONTROL (all 'Services:' labor lines, e.g. CHEMICAL TESTING =
    "LABOR TO TEST AND BALANCE"). Chemicals are separate GENERIC CHEMICALS:* lines.
  - Grain is ~1 month-end invoice PER CUSTOMER (a few multi-pool customers have
    >1). So we reconcile at (qbo_customer_id, billing_month): roll the month's
    promises up to the customer, compare to the customer's month-end invoice(s),
    and write the customer-month verdict onto each of that customer's task-month
    rows (exact 1:1 for the single-task majority; shared for multi-task).

Per (customer, billing_month) we set, on every matching task_billing_period row:
  qbo_invoice_id        representative month-end maintenance invoice (max labor)
  invoice_labor_cents   SUM of maintenance-labor line amounts (customer-month)
  labor_ok              |invoiced_labor - expected_labor| <= labor_tol_cents
  consumables_ok        no item we recorded was under-billed beyond cons_tol
  status                reconciled (labor_ok & consumables_ok) | mismatch | missed
  reconciled_at         now()
  notes                 short diff summary; flags partial_coverage + multi_invoice

BILLING-COVERAGE GATE: a closed month is only reconciled once its monthly billing
run has fired (>= min_coverage of the month's promise-customers have a month-end
maintenance invoice). Unbilled/in-progress months are left as visits_accruing.

COVERAGE CAVEAT: maintenance.visits starts 2026-04-06, so APRIL is a PARTIAL
month (week 1 missing); rows in PARTIAL_MONTHS get a 'partial_coverage' note. Only
closed months (billing_month < this month) are considered.

SAFETY: dry_run=True default -> UPDATE in a transaction, gather the summary, then
ROLLBACK. Set dry_run=False to commit.
"""

import datetime
from f.ION._lib.upsert import _connect

LABOR_PATTERNS = ("POOL MAINTENANCE", "HALF HOUR MAINTENANCE", "FLAT RATE",
                  "CHEMICAL TESTING", "GREEN POOL", "SPA CLEAN", "QUALITY CONTROL")
PARTIAL_MONTHS = {datetime.date(2026, 4, 1)}  # visits sync started 2026-04-06

FETCH_PROMISES = """
SELECT id, qbo_customer_id, billing_month, billing_method,
       expected_labor_cents, billable_visit_count,
       COALESCE(consumables, '{}'::jsonb) AS consumables
FROM billing_audit.task_billing_periods
WHERE qbo_customer_id IS NOT NULL
  AND billing_month < date_trunc('month', now())::date   -- only closed months
"""

FETCH_INVOICES = """
SELECT qbo_invoice_id, qbo_customer_id,
       date_trunc('month', txn_date)::date AS billing_month, line_items
FROM billing.invoices
WHERE qbo_customer_id IS NOT NULL
  AND line_items IS NOT NULL
  AND txn_date = (date_trunc('month', txn_date) + interval '1 month' - interval '1 day')::date
"""

UPDATE = """
UPDATE billing_audit.task_billing_periods SET
  qbo_invoice_id      = %(qbo_invoice_id)s,
  invoice_labor_cents = %(invoice_labor_cents)s,
  labor_ok            = %(labor_ok)s,
  consumables_ok      = %(consumables_ok)s,
  status              = %(status)s,
  reconciled_at       = now(),
  notes               = %(notes)s,
  updated_at          = now()
WHERE id = %(id)s
"""


def _is_labor(item_name):
    n = (item_name or "").upper()
    return any(p in n for p in LABOR_PATTERNS)


def _bare(item_name):
    # suffix after the last ':' -> "NA* - GENERIC CHEMICALS:MURIATIC ACID 1GAL" => "MURIATIC ACID 1GAL"
    if not item_name:
        return None
    return item_name.split(":")[-1].strip()


def _parse_invoice(line_items):
    """Return (labor_cents, consumables {bare_item: qty}) for one invoice."""
    labor_cents = 0
    cons = {}
    for li in (line_items or []):
        if (li or {}).get("line_type") != "item":
            continue
        name = li.get("item_name")
        amt = li.get("amount") or 0
        if _is_labor(name):
            labor_cents += int(round(float(amt) * 100))
        else:
            item = _bare(name)
            qty = li.get("qty")
            if item and qty is not None:
                cons[item] = cons.get(item, 0) + float(qty)
    return labor_cents, cons


def main(supabase_connection, dry_run=True, labor_tol_cents=100, cons_tol=0.01,
         min_coverage=0.5):
    conn = _connect(supabase_connection)
    try:
        # ---- load the customer-month maintenance invoices ----
        inv = {}  # (cust, month) -> {labor_cents, cons{}, ids[(labor,id)]}
        with conn.cursor() as cur:
            cur.execute(FETCH_INVOICES)
            for qid, cust, month, line_items in cur.fetchall():
                lc, cons = _parse_invoice(line_items)
                if lc <= 0:
                    continue  # not the maintenance-labor invoice
                e = inv.setdefault((cust, month), {"labor": 0, "cons": {}, "ids": []})
                e["labor"] += lc
                for k, v in cons.items():
                    e["cons"][k] = e["cons"].get(k, 0) + v
                e["ids"].append((lc, qid))

        # ---- load the promises, group to customer-month ----
        groups = {}  # (cust, month) -> {row_ids[], expected, our_cons{}, methods set}
        with conn.cursor() as cur:
            cur.execute(FETCH_PROMISES)
            for rid, cust, month, method, exp_cents, bvc, cons in cur.fetchall():
                g = groups.setdefault((cust, month), {"ids": [], "expected": 0, "cons": {}, "methods": set()})
                g["ids"].append(rid)
                g["expected"] += (exp_cents or 0)
                g["methods"].add(method)
                for k, v in (cons or {}).items():
                    g["cons"][k] = g["cons"].get(k, 0) + float(v)

        # ---- billing-coverage gate: only reconcile months whose monthly billing
        #      run has actually fired. A closed month with almost no month-end
        #      maintenance invoices isn't "missed" -- it just hasn't been billed
        #      yet (e.g. running this on the 1st before the month's run). Leave
        #      those promises untouched (status stays visits_accruing). ----
        prom_cust = {}
        for (cust, month) in groups:
            prom_cust.setdefault(month, set()).add(cust)
        inv_cust = {}
        for (cust, month) in inv:
            inv_cust.setdefault(month, set()).add(cust)
        coverage = {}
        reconcilable = set()
        for month, custs in prom_cust.items():
            covered = len(custs & inv_cust.get(month, set()))
            cov = covered / len(custs) if custs else 0.0
            coverage[month.strftime("%Y-%m")] = round(cov, 3)
            if cov >= min_coverage:
                reconcilable.add(month)
        skipped_months = sorted(m.strftime("%Y-%m") for m in prom_cust if m not in reconcilable)

        # ---- reconcile each customer-month (in a billed month) ----
        updates = []
        summary = {}  # month -> status -> count
        tot = {"expected": 0, "invoiced": 0}
        for (cust, month), g in groups.items():
            if month not in reconcilable:
                continue  # month not billed yet -> leave as visits_accruing
            partial = month in PARTIAL_MONTHS
            iv = inv.get((cust, month))
            note_bits = []
            if partial:
                note_bits.append("partial_coverage:visits_start_2026-04-06")

            if not iv:
                status, labor_ok, cons_ok = "missed", False, None
                qbo_id, invoiced = None, None
            else:
                invoiced = iv["labor"]
                qbo_id = max(iv["ids"])[1]  # invoice with the largest labor
                if len(iv["ids"]) > 1:
                    note_bits.append("multi_invoice:%d" % len(iv["ids"]))
                diff = invoiced - g["expected"]
                labor_ok = abs(diff) <= labor_tol_cents
                note_bits.append("labor_diff:%+d" % diff)
                # consumables: did we record usage that wasn't billed (under-billed)?
                underbilled = []
                for item, oqty in g["cons"].items():
                    iqty = iv["cons"].get(item, 0)
                    if oqty - iqty > cons_tol:
                        underbilled.append("%s:%.1f>%.1f" % (item, oqty, iqty))
                cons_ok = (len(underbilled) == 0)
                if underbilled:
                    note_bits.append("cons_underbilled:" + ",".join(underbilled[:4]))
                status = "reconciled" if (labor_ok and cons_ok) else "mismatch"
                tot["invoiced"] += invoiced

            tot["expected"] += g["expected"]
            note = "; ".join(note_bits)[:500] or None
            for rid in g["ids"]:
                updates.append({
                    "id": rid, "qbo_invoice_id": qbo_id,
                    "invoice_labor_cents": invoiced, "labor_ok": labor_ok,
                    "consumables_ok": cons_ok, "status": status, "notes": note,
                })
            mk = month.strftime("%Y-%m")
            summary.setdefault(mk, {}).setdefault(status, 0)
            summary[mk][status] += 1

        # ---- write ----
        with conn.cursor() as cur:
            cur.executemany(UPDATE, updates)
            written = cur.rowcount

        if dry_run:
            conn.rollback()
        else:
            conn.commit()

        by_month = []
        for mk in sorted(summary):
            row = {"month": mk}
            row.update(summary[mk])
            by_month.append(row)
        return {
            "dry_run": dry_run, "committed": not dry_run,
            "customer_months": len(groups), "rows_written": written,
            "total_expected_usd": round(tot["expected"] / 100.0, 2),
            "total_invoiced_usd": round(tot["invoiced"] / 100.0, 2),
            "by_month_status": by_month,
            "month_coverage": coverage,
            "skipped_unbilled_months": skipped_months,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
