# requirements:
# psycopg2-binary
# wmill

"""
f/billing/_lib/decisions — credit decision facts.

SOLE AUTHOR (python side) of billing.invoice_credit_decisions. Terminal rows
(applied / rejected) are never overwritten. There is NO 'proposed' state —
undecided is the ABSENCE of a row.

NO EVENT is emitted here. Applying a credit moves money, and that movement is
already `payment_applied` on the carrier payment — with the amount, the
invoice as participant, and a link to the transaction in QBO. A separate
`credit_applied` on the invoice said the same thing a second time from the
other side, so the history showed one application twice.

The decision ROW is our record of WHY (the match reason); the EVENT is the
record of WHAT MOVED, and it belongs to the payment.

Import as:  from f.billing._lib.decisions import record_applied
"""

from f.billing._lib.db import execute


def record_applied(conn, qbo_invoice_id, credit_id, amount, reason=None,
                   *, applied_via="pre_process", decided_by="auto", commit=True):
    execute(conn, """
        INSERT INTO billing.invoice_credit_decisions
              (qbo_invoice_id, credit_id, amount, unapplied_at_decision,
               state, reason, decided_by, applied_via, decided_at, applied_at)
        VALUES (%s, %s, %s, %s, 'applied', %s, %s, %s, now(), now())
        ON CONFLICT (qbo_invoice_id, credit_id) DO UPDATE SET
              state = 'applied', amount = EXCLUDED.amount,
              reason = COALESCE(EXCLUDED.reason,
                                billing.invoice_credit_decisions.reason),
              decided_by = EXCLUDED.decided_by, applied_via = EXCLUDED.applied_via,
              decided_at = now(), applied_at = now()
         WHERE billing.invoice_credit_decisions.state
               NOT IN ('applied', 'rejected')""",
        (qbo_invoice_id, credit_id, amount, amount, reason, decided_by, applied_via))
    if commit:
        conn.commit()
