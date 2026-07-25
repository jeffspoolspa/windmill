# requirements:
# psycopg2-binary
# requests
# wmill

"""
f/billing/_lib/credits — open credits, and applying one.

Two functions:
  open_for(conn, customer_id)          -> the customer's open credits
  apply(conn, qbo, credit, invoice, reason) -> apply ONE credit to ONE invoice

`apply` owns the whole application: the QBO write, the local echo, the
payment_applied event, the decision row, and the credit_applied event. The
caller loops and decides *whether* — it never orchestrates the *how*.

A credit that isn't applied needs no bookkeeping at all: it stays open, and
billing.invoice_ready surfaces it as undecided until a human acts.

Import as:  from f.billing._lib import credits
"""

from f.billing._lib.payments import load_applicable_credits, apply_credits
from f.billing._lib import decisions


def open_for(conn, customer_id, **selectors):
    """Open (unapplied) credits for a customer, oldest first. Selectors are
    data (memo_match / memo_exclude / ref_match) — see load_applicable_credits."""
    return sorted(load_applicable_credits(conn, customer_id, **selectors),
                  key=lambda c: (c.get("txn_date") is None, c.get("txn_date")))


def apply(conn, qbo, credit, invoice, reason, *, applied_via="pre_process"):
    """Apply ONE credit to ONE invoice. Returns the amount applied (0.0 if the
    application did not land).

    The payment service fresh-reads the invoice balance and caps the amount,
    so a credit larger than what is owed applies only what is owed. It also
    echoes the cache and emits payment_applied; the decision write emits
    credit_applied. Both are atomic with their facts.
    """
    result = apply_credits(conn, invoice["qbo_customer_id"], invoice["qbo_invoice_id"],
                           qbo.access_token, qbo.realm_id,
                           credits=[credit], applied_via=applied_via)
    total = 0.0
    for entry in result["applied"]:
        decisions.record_applied(conn, invoice["qbo_invoice_id"],
                                 entry["qbo_payment_id"], entry["amount"],
                                 reason, applied_via=applied_via)
        total += float(entry["amount"])
    return total
