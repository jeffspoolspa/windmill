# requirements:
# psycopg2-binary
# requests

"""
f/billing_audit/reconcile_billing_periods

Reconcile the write-ahead promises (billing_audit.task_billing_periods) against
the ACTUAL QBO maintenance invoices, and stamp the result back on each promise.

Match rule (confirmed with Carter + empirically, 2026-06-02):
  - A maintenance invoice is DATED THE LAST DAY OF THE MONTH and carries a
    maintenance-LABOR line: item_name matching POOL MAINTENANCE* / HALF HOUR
    MAINTENANCE / FLAT RATE. Chemicals are separate GENERIC CHEMICALS:* lines.
  - Grain is ~1 month-end invoice PER CUSTOMER (a few multi-pool customers have
    >1). So we reconcile at (qbo_customer_id, billing_month): roll the month's
    promises up to the customer, compare to the customer's month-end invoice(s),
    and write the customer-month verdict onto each of that customer's task-month
    rows (exact 1:1 for the single-task majority; shared for multi-task).

Per (customer, billing_month) we set, on every matching task_billing_period row:
  invoice_labor_cents   SUM of maintenance-labor line amounts (customer-month)
  labor_ok              |invoiced_labor - expected_labor| <= labor_tol_cents
  consumables_ok        no item we recorded was under-billed beyond cons_tol
  status                reconciled (labor_ok & consumables_ok) | mismatch | missed
  reconciled_at         now()
  notes                 short diff summary; flags partial_coverage + multi_invoice

NOTE (2026-07 pipeline): this script NO LONGER writes qbo_invoice_id — the
billing.invoices link trigger (trg_link_invoice_to_maint_period, DocNumber ->
ion_invoice_number) is the single FK writer. This script is the detailed
VERDICT writer; its status/labor_ok/consumables_ok writes re-project
processing_status via trg_reproject_on_gate_change. It also calls
billing_audit.match_promises_to_ion + project_maint_processing_status per
month (stage 1 of the pipeline) so ION stamping rides the same schedule.

COVERAGE CAVEAT: maintenance.visits currently starts 2026-04-06, so APRIL is a
PARTIAL month (week 1 missing) -> per_visit April promises undercount by ~1 visit
and will read as mismatch; flat_rate_monthly April promises are unaffected (full
month). Rows in PARTIAL_MONTHS get a 'partial_coverage' note. The current/future
month (no invoice yet) is left untouched as 'visits_accruing' -- only months that
have closed (billing_month < this month) are reconciled.

SAFETY: dry_run=True default -> UPDATE in a transaction, gather the summary, then
ROLLBACK. Set dry_run=False to commit.
"""

import datetime
import re
from f.ION._lib.upsert import _connect

# TWO classifiers, two purposes (don't conflate them):
#
# (1) IS_MAINTENANCE -- defines WHICH invoices are maintenance invoices (the set that
#     must map 1:1 to a task). This is the AUTHORITATIVE rule from the billing-audit
#     skill (f/billing_audit/load_month.classify_invoice): the invoice has ANY line
#     whose item name contains one of NINE labor keywords. INCLUDES QUALITY CONTROL
#     and HALF HOUR (they make an invoice a maintenance invoice even though they don't
#     count as labor revenue). Verified: May = 518 such invoices (non-void), matching
#     the billing run. (The old billing_audit.maintenance_invoices table shows 521
#     because it never excluded the 3 voids -- we classify fresh, void-excluded.)
MAINT_KEYWORDS = re.compile(
    r"(POOL MAINTENANCE|FLAT RATE|CHEMICAL TESTING|SPA CLEAN|FOUNTAIN CLEAN"
    r"|QUALITY CONTROL|GREEN POOL|HALF HOUR|ONE TIME CLEAN)", re.I)
#
# (2) LABOR_$ -- the subset that counts as LABOR REVENUE for the dollar reconcile.
#     EXCLUDES HALF HOUR (a consumable add-on, per Carter) and QUALITY CONTROL
#     (non-billable labor; its consumables still bill). Also excludes the generic
#     "Services:LABOR" (one-time/repair) and "Services:FREIGHT" by requiring a
#     specific recurring SKU. SALT CELL CLEAN is a CONSUMABLE add-on (Carter, 2026-06-03),
#     NOT per-visit labor -- it billed as "Services:SALT CELL CLEAN" qty1 ~$50 on e.g.
#     RALEIGH's invoice but is not a maintenance visit; counting it inflated both the labor
#     dollars and (via its qty) the visit count. Excluded from labor; tracked as consumable.
LABOR_INCLUDE = re.compile(
    r"(POOL MAINTENANCE|FLAT RATE|CHEMICAL TESTING|GREEN POOL|SPA CLEAN"
    r"|FOUNTAIN CLEAN|ONE TIME CLEAN)", re.I)
LABOR_EXCLUDE = ("HALF HOUR MAINTENANCE", "QUALITY CONTROL", "SALT CELL CLEAN")
PARTIAL_MONTHS = {datetime.date(2026, 4, 1)}  # visits sync started 2026-04-06

FETCH_PROMISES = """
SELECT id, qbo_customer_id, billing_month, billing_method,
       expected_labor_cents, billable_visit_count,
       COALESCE(consumables, '{}'::jsonb) AS consumables
FROM billing_audit.task_billing_periods
WHERE qbo_customer_id IS NOT NULL
  AND billing_month < date_trunc('month', now())::date   -- only closed months
  AND locked_at IS NULL                                  -- skip finalized months
"""

FETCH_INVOICES = """
SELECT qbo_invoice_id, qbo_customer_id,
       date_trunc('month', txn_date)::date AS billing_month,
       (txn_date = (date_trunc('month', txn_date) + interval '1 month' - interval '1 day')::date) AS is_lastday,
       line_items
FROM billing.invoices i
WHERE qbo_customer_id IS NOT NULL
  AND line_items IS NOT NULL
  AND COALESCE(total_amt, 0) <> 0   -- exclude VOIDED invoices (QBO zeroes total on void)
  AND date_trunc('month', txn_date)::date < date_trunc('month', now())::date
  AND NOT EXISTS (SELECT 1 FROM public.work_orders w      -- WO invoices are a
                  WHERE w.qbo_invoice_id = i.qbo_invoice_id)  -- different pipeline
"""

UPDATE = """
UPDATE billing_audit.task_billing_periods SET
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
    if any(p in n for p in LABOR_EXCLUDE):
        return False
    # Must match a specific recurring-maintenance SKU. A bare "Services:" prefix is
    # NOT enough -- "Services:LABOR" / "Services:FREIGHT" are one-time/non-maintenance.
    return bool(LABOR_INCLUDE.search(n))


def _bare(item_name):
    # suffix after the last ':' -> "NA* - GENERIC CHEMICALS:MURIATIC ACID 1GAL" => "MURIATIC ACID 1GAL"
    # NORMALIZED: collapse internal whitespace + uppercase, and strip the
    # "NA* - " / "GENERIC CHEMICALS" prefixes that some catalog items carry
    # WITHOUT the colon ("NA* - GENERIC CHEMICALS CAL HYPO 1LB") — both the
    # double-space and no-colon variants made billed items look entirely
    # unbilled (June 2026: 30+ false mismatches each).
    if not item_name:
        return None
    n = item_name.split(":")[-1]
    n = re.sub(r"^\s*NA\*\s*-\s*", "", n, flags=re.I)
    n = re.sub(r"^\s*GENERIC CHEMICALS\s*", "", n, flags=re.I)
    return re.sub(r"\s+", " ", n).strip().upper()


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


def main(supabase_connection, dry_run=True, labor_tol_cents=100, cons_tol=0.25,
         min_coverage=0.5):
    conn = _connect(supabase_connection)
    try:
        # ---- load the customer-month maintenance invoices ----
        # EVERY maintenance invoice in the month counts (2026-07-03, Carter):
        # the promises now cover ALL task categories (recurring, green pool,
        # one-time, QC visits' chems), so the customer-month compare is
        # recorded-vs-billed over the customer's WHOLE month — mid-month QC
        # chem invoices, off-cycle jobs, split re-bills all included. The old
        # prefer-last-day window dropped legitimate invoices (Revels: 3
        # invoices billing 8 chlorine read as 4). A maintenance invoice = any
        # invoice with a maintenance-keyword line (incl. QUALITY CONTROL);
        # WO invoices are excluded in SQL; voids too (total_amt = 0).
        def _bucket():
            return {"labor": 0, "cons": {}, "ids": []}

        inv = {}  # (cust, month) -> bucket over all maintenance invoices
        with conn.cursor() as cur:
            cur.execute(FETCH_INVOICES)
            for qid, cust, month, is_lastday, line_items in cur.fetchall():
                lc, cons = _parse_invoice(line_items)
                is_maint = lc > 0 or any(
                    MAINT_KEYWORDS.search((li or {}).get("item_name") or "")
                    for li in (line_items or [])
                    if (li or {}).get("line_type") == "item")
                if not is_maint:
                    continue  # repairs/parts/one-off sales — not this pipeline
                b = inv.setdefault((cust, month), _bucket())
                b["labor"] += lc
                for k, v in cons.items():
                    b["cons"][k] = b["cons"].get(k, 0) + v
                b["ids"].append((lc, qid))

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
                    nk = re.sub(r"\s+", " ", (k or "")).strip().upper()
                    g["cons"][nk] = g["cons"].get(nk, 0) + float(v)

        # ---- stage 1: stamp ION invoice numbers + amounts on each month's
        #      promises (billing_audit.match_promises_to_ion), then project
        #      processing_status (pending -> ion_matched | needs_review).
        #      Same transaction: a dry_run rolls these back too. ----
        ion_stamped = {}
        backfilled = {}
        with conn.cursor() as cur:
            for month in sorted({m for (_, m) in groups}):
                cur.execute("SELECT billing_audit.match_promises_to_ion(%s)", (month,))
                n = cur.fetchone()[0]
                # CDC-truncation backstop: stamped promises whose invoice is
                # in QBO but never reached the cache (a burst window overflows
                # the CDC response cap and the cursor jumps past the dropped
                # rows). Pull them by DocNumber via the canonical refresh —
                # the upsert fires the link trigger + preprocess queue.
                if not dry_run:
                    try:
                        from f.billing.backfill_missing_invoices import main as backfill
                        bf = backfill(month.strftime("%Y-%m"))
                        if bf.get("found_in_qbo"):
                            backfilled[month.strftime("%Y-%m")] = bf["found_in_qbo"]
                    except Exception as e:
                        print(f"  backfill {month}: {e}")
                # true-up the trigger-maintained live chem totals from the
                # authoritative CPV view (catalog price edits, task
                # recategorization) — the hourly drift backstop
                cur.execute("SELECT billing_audit.rebuild_customer_month_chem(%s)", (month,))
                cur.execute(
                    "SELECT billing_audit.project_maint_processing_status(%s)", (month,))
                if n:
                    ion_stamped[month.strftime("%Y-%m")] = n

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
            if iv is not None and iv["labor"] <= 0 and not iv["cons"]:
                iv = None
            note_bits = []
            if partial:
                note_bits.append("partial_coverage:visits_start_2026-04-06")

            if not iv:
                status, labor_ok, cons_ok = "missed", False, None
                invoiced = None
            else:
                invoiced = iv["labor"]
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
                    "id": rid,
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
            "ion_stamped": ion_stamped,
            "cache_backfilled": backfilled,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
