# requirements:
# pandas==2.1.4
# psycopg2-binary==2.9.9
# wmill
# requests

"""
f/ION/backfill_work_orders_from_mirror — history into public.work_orders
from the .NET `ion` mirror, through the SAME transform the 4h scrape uses.

Dry by default: the whole run executes and ROLLS BACK, returning counts.
Carter arms with dry_run=False.

Sources (ion.work_orders, both already Closed in ION, not yet in our cache):
  shape A — report rows: `raw` is the exact ~40-column report row the scrape
            parses (2019-2021). Zero mapping.
  shape B — form-grain rows (2022-2025): ION ids, resolved via the mirror's
            vocabulary tables. Rulings 2026-09-16:
              completed       := wo_date
              corrective_action := NULL
              sub_total       := sum(qty * sales_price) of billable wo_lines

Every backfilled WO is stamped skipped_at + skipped_reason = HISTORY_REASON.
That one annotation is what keeps them out of the billing pipeline
(bootstrap_indicators, the pre-process dispatcher, pull_qbo_invoices all
already honor skipped_at) and what v_revenue_by_month uses to show them.

Invoices: doc numbers present in the qbo mirror (qbo.invoices) are cached
into billing.invoices first, so the WO insert links to them. Shape/transform
copied from pull_qbo_invoices (same line_items + subtotal helpers). Doc
numbers absent from the mirror stay unlinked; the dashboard falls back to
the WO's ION sub_total.

Triggers held off for the transaction (ALTER TABLE ... DISABLE TRIGGER,
transactional, re-enabled before commit):
  billing.invoices.trg_request_pm_refresh_on_invoice_insert  (webhook per customer -> QBO)
  public.work_orders.trg_enqueue_service_preprocess           (queue rows the drainer would only retire)
  public.work_orders.trigger_new_estimate                     (history is not a lead)
"""

from datetime import datetime, timezone

import pandas as pd
import psycopg2.extras

from f.billing._lib.db import get_db_conn
from f.ION._lib.work_orders_upsert import clean, upsert
from f.service_billing.pull_qbo_invoices import qbo_invoice_subtotal, transform_line_items

HISTORY_REASON = 'pre-pipeline history'  # mirrored in v_revenue_by_month (migration 20260916150000)

NOT_IN_CACHE = "NOT EXISTS (SELECT 1 FROM public.work_orders p WHERE p.wo_number = w.wo_number)"

SHAPE_A = f"""
SELECT w.raw
FROM ion.work_orders w
WHERE w.raw ? 'wo_status'
  AND w.raw->>'schedule_status' = 'Closed'
  AND {NOT_IN_CACHE}
  AND (%(year)s IS NULL OR left(w.raw->>'completed', 4) = %(year)s)
ORDER BY w.raw->>'completed'
"""

SHAPE_B = f"""
SELECT w.wo_number,
       t.label                                   AS type,
       s.label                                   AS wo_status,
       CASE WHEN w.billing_type = '2' THEN 'Yes' ELSE 'No' END AS prepaid,
       w.approved_limit::text                    AS approved_limit,
       c.customer_type,
       c.full_name                               AS customer,
       c.first_name, c.last_name,
       COALESCE(NULLIF(c.service_line2, ''), c.service_line1) AS address,
       NULLIF(concat_ws(', ', NULLIF(c.service_city, ''),
                        NULLIF(concat_ws(' ', NULLIF(c.service_state, ''), NULLIF(c.service_postal, '')), '')), '') AS location,
       c.home_phone, c.mobile_phone,
       c.email                                   AS email_address,
       c.office                                  AS office_name,
       u.label                                   AS assigned_to,
       w.created::text                           AS created,
       w.wo_date::text                           AS scheduled,
       w.wo_date::text                           AS completed,
       w.schedule_status,
       (SELECT round(sum(l.qty * l.sales_price), 2)::text FROM ion.wo_lines l
         WHERE l.wo_number = w.wo_number AND l.billable) AS sub_total,
       NULLIF(w.invoice_number, '')              AS invoice_number,
       tm.label                                  AS inv_terms,
       w.wodesc                                  AS work_description,
       w.tech_note                               AS technician_instructions
FROM ion.work_orders w
JOIN ion.wo_types t        ON t.id = w.wo_type
LEFT JOIN ion.wo_statuses s ON s.id = w.status
LEFT JOIN ion.wo_terms tm   ON tm.id = w.term_id
LEFT JOIN ion.customers c   ON c.ion_cust_id = w.customer_id
LEFT JOIN ion.users u       ON u.id = w.assigned_to
WHERE NOT (w.raw ? 'wo_status')
  AND w.schedule_status = 'Closed'
  AND {NOT_IN_CACHE}
  AND (%(year)s IS NULL OR extract(year FROM w.wo_date)::text = %(year)s)
ORDER BY w.wo_date
"""

HELD_TRIGGERS = [
    ('billing.invoices', 'trg_request_pm_refresh_on_invoice_insert'),
    ('public.work_orders', 'trg_enqueue_service_preprocess'),
    ('public.work_orders', 'trigger_new_estimate'),
]


def load_rows(cur, shape: str, year: str | None) -> pd.DataFrame:
    cur.execute(SHAPE_A if shape == 'A' else SHAPE_B, {'year': year})
    rows = cur.fetchall()
    if shape == 'A':
        rows = [r['raw'] for r in rows]
    return pd.DataFrame([dict(r) for r in rows])


def cache_invoices_from_mirror(cur, doc_numbers: list[str]) -> int:
    """billing.invoices rows from qbo.invoices for these doc numbers, if absent."""
    if not doc_numbers:
        return 0
    cur.execute("""
        SELECT q.raw, q.checked_at
        FROM qbo.invoices q
        WHERE q.doc_number = ANY(%s)
          AND NOT EXISTS (SELECT 1 FROM billing.invoices b WHERE b.qbo_invoice_id = q.id)
          -- billing.invoices FKs public."Customers"; a customer QBO knows but our
          -- cache does not cannot carry an invoice row. Counted, not cached.
          AND EXISTS (SELECT 1 FROM public."Customers" c WHERE c.qbo_customer_id = q.customer_id)
    """, (doc_numbers,))
    n = 0
    for r in cur.fetchall():
        inv = r['raw']
        customer_ref = inv.get('CustomerRef', {}) or {}
        cur.execute("""
            INSERT INTO billing.invoices (
                qbo_invoice_id, doc_number, qbo_customer_id, customer_name,
                txn_date, due_date, total_amt, subtotal, balance, email_status,
                line_items, raw, fetched_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
            ON CONFLICT (qbo_invoice_id) DO NOTHING
        """, (
            inv.get('Id'), str(inv.get('DocNumber')), customer_ref.get('value'), customer_ref.get('name'),
            inv.get('TxnDate'), inv.get('DueDate'),
            float(inv.get('TotalAmt', 0) or 0), qbo_invoice_subtotal(inv),
            float(inv.get('Balance', 0) or 0), inv.get('EmailStatus'),
            psycopg2.extras.Json(transform_line_items(inv.get('Line', []))),
            psycopg2.extras.Json(inv),
            r['checked_at'] or datetime.now(timezone.utc),
        ))
        n += cur.rowcount or 0
    return n


def main(dry_run: bool = True, shape: str = 'both', year: str = None, limit: int = None):
    """dry_run=True runs everything and rolls back. shape: A | B | both.
    year: restrict to one completed-year (string, e.g. '2023'). limit: rows per shape."""
    shapes = ['A', 'B'] if shape == 'both' else [shape.upper()]
    conn = get_db_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    summary = {'dry_run': dry_run, 'shapes': {}}
    try:
        for tbl, trg in HELD_TRIGGERS:
            cur.execute(f'ALTER TABLE {tbl} DISABLE TRIGGER {trg}')

        for s in shapes:
            df = load_rows(cur, s, year)
            if limit:
                df = df.head(int(limit))
            if df.empty:
                summary['shapes'][s] = {'rows': 0}
                continue
            df, bad_dates = clean(df)
            df['skipped_at'] = datetime.now(timezone.utc).isoformat()
            df['skipped_reason'] = HISTORY_REASON

            docs = sorted({d for d in df['invoice_number'].dropna().astype(str) if d})
            cached = cache_invoices_from_mirror(cur, docs)
            res = upsert(conn, df, on_conflict='nothing')

            cur.execute("""
                SELECT count(*) AS n FROM qbo.invoices q
                WHERE q.doc_number = ANY(%s)
                  AND NOT EXISTS (SELECT 1 FROM public."Customers" c WHERE c.qbo_customer_id = q.customer_id)
            """, (docs,))
            no_customer = cur.fetchone()['n']
            cur.execute("""
                SELECT count(*) FILTER (WHERE qbo_invoice_id IS NOT NULL) AS linked,
                       count(*) AS total, min(completed)::text AS lo, max(completed)::text AS hi
                FROM public.work_orders WHERE skipped_reason = %s
            """, (HISTORY_REASON,))
            snap = cur.fetchone()
            summary['shapes'][s] = {
                'rows': res['rows'], 'inserted': res['written'],
                'invoice_numbers': len(docs), 'invoices_cached_from_mirror': cached,
                'invoices_in_mirror_but_customer_uncached': no_customer,
                'bad_dates_coerced': bad_dates,
                'history_total_after': snap['total'], 'history_linked_after': snap['linked'],
                'history_completed_range': [snap['lo'], snap['hi']],
            }
            print(f"shape {s}: {summary['shapes'][s]}")

        # Proof the pipeline did not wake: nothing queued for these invoices.
        cur.execute("""
            SELECT count(*) AS n FROM billing.service_preprocess_queue q
            JOIN public.work_orders w ON w.qbo_invoice_id = q.qbo_invoice_id
            WHERE w.skipped_reason = %s AND q.finished_at IS NULL
        """, (HISTORY_REASON,))
        summary['preprocess_queue_rows_for_history'] = cur.fetchone()['n']
        cur.execute("""
            SELECT count(*) AS n FROM billing.invoices i
            JOIN public.work_orders w ON w.qbo_invoice_id = i.qbo_invoice_id
            WHERE w.skipped_reason = %s AND i.billing_status IS NOT NULL
        """, (HISTORY_REASON,))
        summary['history_invoices_with_billing_status'] = cur.fetchone()['n']

        for tbl, trg in HELD_TRIGGERS:
            cur.execute(f'ALTER TABLE {tbl} ENABLE TRIGGER {trg}')

        if dry_run:
            conn.rollback()
            print('DRY RUN — rolled back')
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
    return summary
