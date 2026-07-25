# requirements:
# psycopg2-binary
# requests

"""
f/billing/_lib/customers — what we know about a customer.

Import as:  from f.billing._lib import customers
"""

from f.billing._lib.db import query_one
from f.billing._lib import payment_methods


def payment_route(conn, qbo, qbo_customer_id, wo_text=None):
    """How this customer pays. One question, one answer.

    'email'                -> we send the invoice; method_id is NULL.
    'credit_card' / 'ach'  -> we charge their default active method.

    Refreshes the wallet from QBO FIRST. Both halves of the rule read
    billing.customer_payment_methods — the preference falls back to the
    default method's type, and the target IS a method row — so routing off a
    stale cache picks a card the customer removed, or misses one they just
    added. The refresh is TTL-gated, so a burst of invoices for one customer
    costs one wallet read, not one per invoice.

    The rule itself lives in SQL (billing.resolve_preferred_payment_type +
    pick_target_payment_method) because billing.invoice_ready and the
    payment-method trigger ask the same question — one rule, three callers.
    `wo_text` carries the per-job '*bill*' override.
    """
    payment_methods.refresh(conn, qbo_customer_id, qbo.access_token)

    row = query_one(conn, """
        WITH pref AS (SELECT billing.resolve_preferred_payment_type(%s, %s) AS type)
        SELECT type,
               billing.pick_target_payment_method(%s, type) AS method_id
          FROM pref""",
        (qbo_customer_id, wo_text, qbo_customer_id))
    return {"preferred_payment_type": row["type"],
            "target_payment_method_id": row["method_id"],
            "payment_method": "invoice" if row["type"] == "email" else "on_file"}
