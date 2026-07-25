# requirements:
# psycopg2-binary

"""
f/billing/_lib/decisions — credit decision facts.

SOLE AUTHOR (python side) of billing.invoice_credit_decisions. Terminal rows
(applied / rejected) are never overwritten. There is NO 'proposed' state —
undecided is the ABSENCE of a row.

The row and its event commit TOGETHER (one transaction) — a decision can
never exist without its credit_applied event.

Import as:  from f.billing._lib.decisions import record_applied
"""

from f.billing._lib.db import execute
from f.billing._lib.events import emit


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
    emit(conn, "invoice", qbo_invoice_id, "credit_applied",
         participants=[f"payment:{credit_id}"],
         payload={"amount": amount, "applied_via": applied_via, "reason": reason,
                  "provenance": {"source": "intent", "intent_ref": applied_via}})
    if commit:
        conn.commit()   # ONE commit — fact + event atomic
