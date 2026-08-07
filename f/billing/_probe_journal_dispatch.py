# requirements:
# psycopg2-binary
# requests
# wmill

# TEMPORARY probe (delete after run): exercises the journal-first dispatch
# against invoice 68300's REAL billing.charges row. dry_run + invalid token:
# no write path, no charge path — worst case is read_failed.

from f.billing._lib.db import get_db_conn
from f.billing._lib.payments import charge_and_record


def main():
    conn = get_db_conn()
    try:
        return charge_and_record(conn, {
            "stage": "process", "qbo_invoice_id": "68300", "channel": "card",
            "payment_method_id": "probe", "cpm_id": "bf7a867b-2f2c-422c-be49-331221315923",
            "customer_id": "9655", "customer_name": "Simmons, Judy",
            "invoice_number": "7956325", "payment_ref": "7956325",
            "memo_prefix": "probe", "receipt_email": None,
        }, "invalid-token", "invalid-realm", dry_run=True)
    finally:
        conn.close()
