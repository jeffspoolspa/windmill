# requirements:
# pandas==2.1.4
# psycopg2-binary==2.9.9
# sqlalchemy==2.0.43
#
# Upsert ION work orders into public.work_orders. The `billable` column is a
# generated column derived from billable_override + schedule_status, so we do
# NOT include it in the upsert (Postgres rejects writes to GENERATED columns).
# employee_id reconciled separately via ion_username lookup.

import pandas as pd
import json
from sqlalchemy import create_engine, text
import io
import csv

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
    work_orders = work_orders.replace('', None)

    # NOTE: `billable` is a GENERATED column on public.work_orders
    # (COALESCE(billable_override, schedule_status IN ('Closed', 'Closed - Not Invoiced')))
    # so we deliberately don't add it to the DataFrame. Postgres will compute it.
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
                update_set = ', '.join([f'{col} = EXCLUDED.{col}' for col in update_cols])
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

    return {
        'status': 'success' if success else 'error',
        'total_work_orders': total_rows,
        'processed': total_rows if success else 0,
        'failed': 0 if success else total_rows,
        'employee_links_reconciled': employee_reconciled,
        'error': error_msg if not success else None,
    }