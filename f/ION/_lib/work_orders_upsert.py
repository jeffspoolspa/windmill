# requirements:
# pandas==2.1.4
# psycopg2-binary==2.9.9

"""
f/ION/_lib/work_orders_upsert — the ONE transform from an ION work-order
report row into public.work_orders.

Two callers, one writer:
  - f/ION/work_orders.flow (extract_and_upload step) — the 4-hourly scrape
  - f/ION/backfill_work_orders_from_mirror — history from the .NET `ion` mirror

clean(df)  : ION display headers -> our snake_case columns, currency parse,
             date coercion (garbage -> NULL, logged), '' -> NULL.
upsert(conn, df, ...) : COPY into a temp table, upsert on wo_number, then
             reconcile employee_id from assigned_to. Caller owns the transaction.

Per-column leadership is preserved by omission: the DataFrame only carries
ION columns (plus whatever `extra` the caller adds), so ON CONFLICT never
touches billing's columns. See docs/flows/sync/ion-work-orders.md.
"""

import csv
import io

import pandas as pd

COLUMN_MAPPING = {
    'WO #': 'wo_number', 'Type': 'type', 'Template': 'template',
    'WO Status': 'wo_status', 'Recurrence': 'recurrence', 'Prepaid': 'prepaid',
    'Approved Limit': 'approved_limit', 'Customer Type': 'customer_type',
    'Customer': 'customer', 'First Name': 'first_name', 'Last Name': 'last_name',
    'Address': 'address', 'Location': 'location', 'Home Phone': 'home_phone',
    'Mobile Phone': 'mobile_phone', 'Site Phone': 'site_phone',
    'Email Address': 'email_address', 'Builder': 'builder',
    'Install Date': 'install_date', 'Model': 'model', 'Part Number': 'part_number',
    'Serial Number': 'serial_number', 'Tag Number': 'tag_number',
    'Office Name': 'office_name', 'Created By': 'created_by',
    'Assigned To': 'assigned_to', 'Created': 'created', 'Scheduled': 'scheduled',
    'Started': 'started', 'Completed': 'completed', 'Last Sent': 'last_sent',
    'Approval Status': 'approval_status', 'Schedule Status': 'schedule_status',
    'Sub Total': 'sub_total', 'Tax Total': 'tax_total', 'Total Due': 'total_due',
    'Invoice #': 'invoice_number', 'Inv. Terms': 'inv_terms',
    'Total Min.': 'total_minutes', 'Trips': 'trips',
    'Work Description': 'work_description',
    'Technician Instructions': 'technician_instructions',
    'Corrective Action': 'corrective_action',
}

DATE_COLUMNS = ['install_date', 'created', 'scheduled', 'started', 'completed', 'last_sent']
CURRENCY_COLUMNS = ['approved_limit', 'sub_total', 'tax_total', 'total_due']


def clean(work_orders: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Report row -> our column shape. Returns (df, bad_date_summary).

    Accepts either ION display headers or already-snake_case columns (the
    mirror stores the latter) — rename is a no-op for unknown keys.
    """
    work_orders = work_orders.rename(columns=COLUMN_MAPPING)

    for col in CURRENCY_COLUMNS:
        if col in work_orders.columns:
            work_orders[col] = (work_orders[col].astype(str)
                .str.replace('$', '', regex=False)
                .str.replace(',', '', regex=False)
                .replace({'': None, 'None': None, 'nan': None}))
            work_orders[col] = pd.to_numeric(work_orders[col], errors='coerce')

    # ION's HTML report occasionally has garbage (part numbers, serial
    # fragments) in date columns. One bad row would abort the whole COPY and
    # roll back every upsert — which is how 2024 WOs silently stopped syncing.
    # Coerce to NULL and log the WO so it can be fixed in ION.
    bad_date_summary = {}
    for col in DATE_COLUMNS:
        if col not in work_orders.columns:
            continue
        raw = work_orders[col]
        parsed = pd.to_datetime(raw, errors='coerce')
        bad_mask = parsed.isna() & raw.notna() & (raw.astype(str).str.strip() != '')
        bad_count = int(bad_mask.sum())
        if bad_count > 0:
            bad_date_summary[col] = bad_count
            print(f'  {col}: coerced {bad_count} bad value(s) to NULL')
            for _, r in work_orders.loc[bad_mask, ['wo_number', 'customer', col]].iterrows():
                print(f"    WO {r.get('wo_number', '?')} ({r.get('customer', '?')}): {col}={r.get(col)!r}")
        work_orders[col] = parsed

    work_orders = work_orders.replace('', None)
    return work_orders, bad_date_summary


def upsert(conn, work_orders: pd.DataFrame, *, on_conflict: str = 'update') -> dict:
    """COPY + upsert on wo_number + employee reconcile. Caller commits.

    on_conflict='update' (the sync): ION-owned columns overwrite, except
      invoice_number which is COALESCEd — f/ION/refresh_stale_work_orders
      sources it from WOStatus.cfm, more reliable than the bulk report.
    on_conflict='nothing' (the backfill): existing rows are never touched.
    """
    columns = work_orders.columns.tolist()
    columns_str = ', '.join(columns)
    update_cols = [c for c in columns if c != 'wo_number']

    cur = conn.cursor()
    cur.execute('CREATE TEMP TABLE work_orders_temp (LIKE public.work_orders INCLUDING DEFAULTS) ON COMMIT DROP')
    buf = io.StringIO()
    work_orders.to_csv(buf, sep='\t', header=False, index=False, na_rep='',
                       quoting=csv.QUOTE_MINIMAL, escapechar='\\')
    buf.seek(0)
    cur.copy_expert(
        f"COPY work_orders_temp ({columns_str}) FROM STDIN WITH "
        "(FORMAT CSV, DELIMITER E'\\t', NULL '', QUOTE '\"', ESCAPE '\"')",
        buf,
    )

    if on_conflict == 'nothing':
        conflict = 'ON CONFLICT (wo_number) DO NOTHING'
    else:
        set_parts = [
            f'{c} = COALESCE(EXCLUDED.{c}, work_orders.{c})' if c == 'invoice_number'
            else f'{c} = EXCLUDED.{c}'
            for c in update_cols
        ]
        changed = ' OR '.join(f'work_orders.{c} IS DISTINCT FROM EXCLUDED.{c}' for c in update_cols)
        conflict = (f"ON CONFLICT (wo_number) DO UPDATE SET {', '.join(set_parts)}, "
                    f"last_updated = NOW() WHERE {changed}")

    cur.execute(
        f'INSERT INTO public.work_orders ({columns_str}) '
        f'SELECT {columns_str} FROM work_orders_temp {conflict}'
    )
    written = cur.rowcount or 0

    cur.execute("""
        UPDATE public.work_orders w
        SET employee_id = sub.emp_id
        FROM (
            SELECT wo.wo_number,
                   (SELECT e.id FROM public.employees e
                    WHERE wo.assigned_to = ANY(e.ion_username) LIMIT 1) AS emp_id
            FROM public.work_orders wo
            WHERE wo.assigned_to IS NOT NULL
        ) sub
        WHERE w.wo_number = sub.wo_number
          AND w.employee_id IS DISTINCT FROM sub.emp_id
    """)
    employee_reconciled = cur.rowcount or 0
    cur.close()
    return {'rows': len(work_orders), 'written': written, 'employee_links_reconciled': employee_reconciled}


def main():
    """Library module — import, don't run."""
    return {"ok": True}
