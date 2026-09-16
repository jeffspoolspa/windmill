# requirements:
# pandas==2.1.4
# psycopg2-binary==2.9.9
#
# Parse the scraped report and hand it to the one work-order writer,
# f/ION/_lib/work_orders_upsert (clean + COPY upsert + employee link).
# Raises on failure so Windmill marks the job failed and alerts fire.

import json

import pandas as pd
import psycopg2

from f.ION._lib.work_orders_upsert import clean, upsert


def main(previous_result: dict, supabase_connection: dict):
    with open(previous_result['report_1']['filepath'], 'r') as f:
        report1_data = json.load(f)
    work_orders = pd.DataFrame(report1_data['raw_table'][4:], columns=report1_data['raw_table'][3])
    print(f'Loaded {len(work_orders)} work orders')

    work_orders, bad_dates = clean(work_orders)

    conn = psycopg2.connect(
        host=supabase_connection['host'], port=supabase_connection['port'],
        dbname=supabase_connection['dbname'], user=supabase_connection['user'],
        password=supabase_connection['password'],
    )
    try:
        result = upsert(conn, work_orders)
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise Exception(f'Upsert failed for {len(work_orders)} work orders: {e}')
    finally:
        conn.close()

    print(f"Upserted {result['rows']} work orders; reconciled {result['employee_links_reconciled']} employee links")
    return {
        'status': 'success',
        'total_work_orders': result['rows'],
        'processed': result['rows'],
        'failed': 0,
        'employee_links_reconciled': result['employee_links_reconciled'],
        'bad_dates_coerced': bad_dates,
        'error': None,
    }
