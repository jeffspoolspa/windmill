# requirements:
# pandas==2.1.4
# psycopg2-binary==2.9.9
# sqlalchemy==2.0.43
#
# Upsert ION work orders into public.work_orders. The `billable` column is a
# generated column derived from billable_override + schedule_status, so we do
# NOT include it in the upsert (Postgres rejects writes to GENERATED columns).
# employee_id reconciled separately via ion_username lookup.
#
# Date sanitization: ION's HTML report occasionally has garbage strings
# (part numbers, serial fragments) in date columns. Without coercion, ONE
# bad row causes the entire COPY-into-temp-table to abort, rolling back
# ALL upserts — which is exactly how 2024 work orders silently failed to
# sync for who-knows-how-long. We pd.to_datetime(errors='coerce') each date
# column, log the offending wo_number + customer + raw value so the data
# can be cleaned up in ION too, then proceed.
#
# Failure mode: raise instead of returning status='error' so Windmill
# marks the job failed and alerts fire. Previously the script returned
# error-state inside a 'success' dict, which the flow read as green.

import pandas as pd
import json
from sqlalchemy import create_engine, text
import io
import csv

DATE_COLUMNS = ['install_date', 'created', 'scheduled', 'started', 'completed', 'last_sent']


def main(previous_result: dict, supabase_connection: dict):
    print('Loading reports...')
    with open(previous_result['report_1']['filepath'], 'r') as f:
        report1_data = json.load(f)
    work_orders = pd.DataFrame(
        report1_data['raw_table'][4:],
        columns=report1_data['raw_table'][3]
    )
    print(f'Loaded {len(work_orders)} work orders')

    print('Cleaning data...')
    column_mapping = {
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
        'Corrective Action': 'corrective_action'
    }
    work_orders = work_orders.rename(columns=column_mapping)

    currency_cols = ['approved_limit', 'sub_total', 'tax_total', 'total_due']
    for col in currency_cols:
        if col in work_orders.columns:
            work_orders[col] = (work_orders[col].astype(str)
                .str.replace('$', '', regex=False)
                .str.replace(',', '', regex=False)
                .replace('', None))
            work_orders[col] = pd.to_numeric(work_orders[col], errors='coerce')

    # Date coercion + verbose logging of bad rows so we can trace each one
    # back to its WO in ION and verify it's an ION data-entry mistake vs a
    # parser misalignment bug on our side.
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
            bad_rows = work_orders.loc[bad_mask, ['wo_number', 'customer', col]]
            for _, r in bad_rows.iterrows():
                wo = r.get('wo_number', '?')
                cust = r.get('customer', '?')
                val = r.get(col, '?')
                print(f'    WO {wo} ({cust}): {col}={val!r}')
        work_orders[col] = parsed

    work_orders = work_orders.replace('', None)

    print('Data cleaned')

    print('Upserting to Supabase...')
    connection_string = (
        f"postgresql://{supabase_connection['user']}:{supabase_connection['password']}"
        f"@{supabase_connection['host']}:{supabase_connection['port']}/{supabase_connection['dbname']}"
    )
    engine = create_engine(connection_string)
    records = work_orders
    total_rows = len(records)
    columns = records.columns.tolist()
    columns_str = ', '.join(columns)
    update_cols = [col for col in columns if col != 'wo_number']

    success = False
    error_msg = None
    employee_reconciled = 0
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.execute(text('CREATE TEMP TABLE work_orders_temp (LIKE work_orders INCLUDING DEFAULTS) ON COMMIT DROP'))
                output = io.StringIO()
                records.to_csv(output, sep='\t', header=False, index=False, na_rep='', quoting=csv.QUOTE_MINIMAL, escapechar='\\')
                output.seek(0)
                raw_conn = conn.connection
                cursor = raw_conn.cursor()
                copy_sql = (
                    f"COPY work_orders_temp ({columns_str}) FROM STDIN WITH "
                    "(FORMAT CSV, DELIMITER E'\\t', NULL '', QUOTE '\"', ESCAPE '\"')"
                )
                cursor.copy_expert(copy_sql, output)
                # Preserve refresh-supplied invoice_number when bulk doesn't provide one.
                # f/ION/refresh_stale_work_orders sources invoice_number from the per-WO
                # WOStatus.cfm endpoint — more reliable than bulk WorkOrderDetail.cfm,
                # which silently drops some WOs (e.g., WO 4972018: in CARTER ADMIN's
                # manual download, absent from playwright session same URL same user).
                # Without COALESCE a NULL from EXCLUDED would overwrite a good refresh
                # value every 4 hours.
                update_set_parts = []
                for col in update_cols:
                    if col == 'invoice_number':
                        update_set_parts.append(
                            f'{col} = COALESCE(EXCLUDED.{col}, work_orders.{col})'
                        )
                    else:
                        update_set_parts.append(f'{col} = EXCLUDED.{col}')
                update_set = ', '.join(update_set_parts)
                where_conditions = ' OR '.join([
                    f'work_orders.{col} IS DISTINCT FROM EXCLUDED.{col}' for col in update_cols
                ])
                upsert_sql = (
                    f'INSERT INTO work_orders ({columns_str}) '
                    f'SELECT {columns_str} FROM work_orders_temp '
                    f'ON CONFLICT (wo_number) DO UPDATE SET {update_set}, last_updated = NOW() '
                    f'WHERE {where_conditions}'
                )
                conn.execute(text(upsert_sql))

                emp_update = conn.execute(text("""
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
                """))
                employee_reconciled = emp_update.rowcount or 0

                trans.commit()
                print(f'Upserted {total_rows} work orders; reconciled {employee_reconciled} employee links')
                success = True
            except Exception as e:
                trans.rollback()
                error_msg = str(e)
                print(f'Upsert error: {error_msg}')
    except Exception as e:
        error_msg = str(e)
        print(f'Connection error: {error_msg}')

    if not success:
        raise Exception(
            f'Upsert failed for {total_rows} work orders: {error_msg}'
        )

    return {
        'status': 'success',
        'total_work_orders': total_rows,
        'processed': total_rows,
        'failed': 0,
        'employee_links_reconciled': employee_reconciled,
        'bad_dates_coerced': bad_date_summary,
        'error': None,
    }
