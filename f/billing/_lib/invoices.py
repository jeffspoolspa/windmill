# requirements:
# psycopg2-binary

"""
f/billing/_lib/invoices — the invoice aggregate's fact writers.

SOLE AUTHOR of these columns. Every caller imports these; nothing else writes
them. Status is never written here — it is projected by triggers from the
facts below (docs: audit/parts/03-derivations-sql.md).

Import as:  from f.billing._lib.invoices import load, write_enrichment
"""

from f.billing._lib.db import query_one, execute, execute_sql


def load(conn, qbo_invoice_id):
    """The invoice joined to its work order — the shape every billing
    sentence needs. Replaces 4 hand-written copies of this join."""
    return query_one(conn, """
        SELECT i.*,
               concat_ws(' ', w.work_description, w.technician_instructions,
                              w.corrective_action) AS wo_text, w.wo_number, w.assigned_to, w.type, w.type AS wo_type,
               w.work_description, w.technician_instructions, w.corrective_action,
               w.completed, w.sub_total AS wo_sub_total
          FROM billing.invoices i
          JOIN public.work_orders w ON w.qbo_invoice_id = i.qbo_invoice_id
         WHERE i.qbo_invoice_id = %s""", (qbo_invoice_id,))


def write_enrichment(conn, qbo_invoice_id, *, payment_method, preferred_payment_type,
                     target_payment_method_id, qbo_class, memo, statement_memo,
                     memo_locked, commit=True):
    """The enrichment result — route, class, memo — plus the attempt stamp.

    NOT written here: enrichment_ok (derives via trg_set_enrichment_ok),
    billing_status / needs_review_reason (projection triggers). This single
    UPDATE fires the indicator cascade; everything downstream derives."""
    (execute_sql if commit else execute)(conn, """
        UPDATE billing.invoices
           SET payment_method           = %s,
               preferred_payment_type   = %s,
               target_payment_method_id = %s,
               qbo_class                = %s,
               memo                     = %s,
               statement_memo           = %s,
               memo_locked              = %s,
               pre_processed_at         = now()
         WHERE qbo_invoice_id = %s""",
        (payment_method, preferred_payment_type, target_payment_method_id,
         qbo_class, memo, statement_memo, bool(memo_locked), qbo_invoice_id))
