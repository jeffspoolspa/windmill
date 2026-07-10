# requirements:
# psycopg2-binary

"""
f/billing/_lib/cache — verified-echo writes onto billing.invoices (ADR 009 §C).

Tier 1: each function is ONE cache write, one column-set per known fact.
Write what you can prove, when you can prove it: email_status when the send
returned ok (a fact we author), balance when a confirming read returned it
(a value we observe). Never write a fabricated balance — the auto-promote
trigger treats balance <= 0 as really-paid.

Import as:  from f.billing._lib.cache import echo_invoice, echo_balance, mark_emailed
"""

import json
import uuid
from datetime import datetime, date
from decimal import Decimal


def _dumps(obj):
    def d(o):
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        if isinstance(o, uuid.UUID):
            return str(o)
        raise TypeError(f"not JSON serializable: {type(o).__name__}")
    return json.dumps(obj, default=d)


def invoice_subtotal(inv):
    """Subtotal from a QBO Invoice payload: the SubTotalLineDetail line, else
    TotalAmt minus tax. Pure."""
    for line in inv.get("Line", []) or []:
        if line.get("DetailType") == "SubTotalLineDetail":
            try:
                return round(float(line.get("Amount", 0) or 0), 2)
            except (TypeError, ValueError):
                pass
    total = float(inv.get("TotalAmt", 0) or 0)
    tax = float((inv.get("TxnTaxDetail") or {}).get("TotalTax", 0) or 0)
    return round(total - tax, 2)


def echo_invoice(conn, qbo_invoice_id, qbo_invoice):
    """Full-snapshot echo of a freshly READ QBO invoice (balance, email
    status, raw). The payload must come from QBO — never synthesized."""
    cur = conn.cursor()
    cur.execute("""
        UPDATE billing.invoices
        SET subtotal = %s, balance = %s, total_amt = %s,
            email_status = %s, raw = %s::jsonb, fetched_at = now()
        WHERE qbo_invoice_id = %s
    """, (invoice_subtotal(qbo_invoice),
          float(qbo_invoice.get("Balance", 0) or 0),
          float(qbo_invoice.get("TotalAmt", 0) or 0),
          qbo_invoice.get("EmailStatus"),
          _dumps(qbo_invoice), qbo_invoice_id))
    conn.commit(); cur.close()


def echo_balance(conn, qbo_invoice_id, balance):
    """One-column echo of a balance QBO reported on a confirming read."""
    cur = conn.cursor()
    cur.execute("UPDATE billing.invoices SET balance = %s WHERE qbo_invoice_id = %s",
                (balance, qbo_invoice_id))
    conn.commit(); cur.close()


def mark_emailed(conn, qbo_invoice_id):
    """One-column echo of a send we performed and QBO acknowledged."""
    cur = conn.cursor()
    cur.execute("UPDATE billing.invoices SET email_status = 'EmailSent' WHERE qbo_invoice_id = %s",
                (qbo_invoice_id,))
    conn.commit(); cur.close()


def _selfcheck():
    checks = []
    def ok(name, cond):
        checks.append((name, bool(cond)))

    ok("subtotal prefers SubTotalLineDetail",
       invoice_subtotal({"Line": [{"DetailType": "SubTotalLineDetail", "Amount": "12.34"}],
                         "TotalAmt": 99, "TxnTaxDetail": {"TotalTax": 1}}) == 12.34)
    ok("subtotal falls back to total minus tax",
       invoice_subtotal({"TotalAmt": 10.0, "TxnTaxDetail": {"TotalTax": 0.7}}) == 9.3)

    class C:
        def __init__(self): self.executed = []
        def cursor(self, **kw): return self
        def execute(self, sql, params): self.executed.append((" ".join(sql.split()), params))
        def commit(self): pass
        def close(self): pass
    c = C()
    echo_invoice(c, "I1", {"Balance": "5.00", "TotalAmt": "5.00", "EmailStatus": "NotSet"})
    ok("echo_invoice writes the read-back values",
       c.executed[0][1][1] == 5.0 and c.executed[0][1][3] == "NotSet")
    echo_balance(c, "I1", 0.0)
    ok("echo_balance is one column", c.executed[1][0].startswith(
        "UPDATE billing.invoices SET balance = %s WHERE"))
    mark_emailed(c, "I1")
    ok("mark_emailed is one column", "email_status = 'EmailSent'" in c.executed[2][0])

    failed = [n for n, p in checks if not p]
    return {"passed": len(checks) - len(failed), "total": len(checks), "failed": failed}


def main():
    result = _selfcheck()
    result["ok"] = not result["failed"]
    return result


if __name__ == "__main__":
    print(main())
