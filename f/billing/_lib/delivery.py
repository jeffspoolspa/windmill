# requirements:
# psycopg2-binary
# requests
# wmill

"""
f/billing/_lib/delivery — the shared document-delivery service.

Emailing a customer their invoice copy or receipt is a DELIVERY action, not a
payment (payments = moving money, in _lib/payments). This module is the single,
idempotent gate for getting a QBO document to the customer, shared by every
workflow that sends: the maintenance charge worker (auto), the manual Send
action, and service billing. One shared gate is what makes a double-send
structurally impossible.

Send preconditions live HERE, not in invoice_ready (Carter 2026-07-22): a rule
about the harm of SENDING belongs to the send service. Current rule: never
FIRST-send an invoice whose due date is already past — QBO would immediately
brand it past-due to the customer. Resends are exempt (an already-delivered
invoice is legitimately past due).

Layer: service (composes the _lib/qbo send primitive + the _lib/cache echo).

Import as:  from f.billing._lib.delivery import deliver_invoice
"""

from datetime import date

from f.billing._lib.qbo import send_invoice
from f.billing._lib.cache import mark_emailed


def _due_date(conn, invoice_id):
    cur = conn.cursor()
    cur.execute("SELECT due_date FROM billing.invoices WHERE qbo_invoice_id = %s",
                (invoice_id,))
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def deliver_invoice(conn, invoice_id, email, email_status,
                    access_token, realm_id, resend=False):
    """Email the customer their invoice copy, idempotently, and record the
    emailed fact. Skips if the invoice is already `EmailSent` (unless
    `resend=True` — the manual "Send invoice copies" path). Refuses a FIRST
    send when the due date is already past (fix the date in QBO, then send).
    On success records the emailed fact (mark_emailed echo).
    Returns {ok, error?, already?}.
    """
    if not resend and email_status == "EmailSent":
        return {"ok": True, "already": True}
    if not email:
        return {"ok": False, "error": "no email on file"}
    if email_status != "EmailSent":  # first send — the past-due guard applies
        due = _due_date(conn, invoice_id)
        if due is not None and due < date.today():
            return {"ok": False,
                    "error": f"due date {due} is past — first send would arrive "
                             f"past-due; update the due date in QBO first"}
    r = send_invoice(invoice_id, email, access_token, realm_id)
    if r.get("ok"):
        mark_emailed(conn, invoice_id)
    return r


def _selfcheck():
    """No network/DB — verify the idempotency + send-precondition guards."""
    from datetime import timedelta
    calls = {"sent": 0, "marked": 0}
    g = globals()
    real_send, real_mark, real_due = g["send_invoice"], g["mark_emailed"], g["_due_date"]
    due_by_id = {}

    def fake_send(inv, email, at, realm):
        calls["sent"] += 1
        return {"ok": True, "error": None}

    def fake_mark(conn, inv):
        calls["marked"] += 1

    def fake_due(conn, inv):
        return due_by_id.get(inv)

    g["send_invoice"], g["mark_emailed"], g["_due_date"] = fake_send, fake_mark, fake_due
    try:
        # already EmailSent, not a resend -> skip (no send)
        r = deliver_invoice(None, "i1", "a@b.com", "EmailSent", "t", "r")
        assert r == {"ok": True, "already": True} and calls["sent"] == 0
        # resend bypasses the guard -> sends + records (even if past due)
        due_by_id["i1"] = date.today() - timedelta(days=30)
        r = deliver_invoice(None, "i1", "a@b.com", "EmailSent", "t", "r", resend=True)
        assert r["ok"] and calls["sent"] == 1 and calls["marked"] == 1
        # no email -> no send
        r = deliver_invoice(None, "i1", None, "NotSet", "t", "r")
        assert r["ok"] is False and calls["sent"] == 1
        # FIRST send with past due date -> refused
        due_by_id["i2"] = date.today() - timedelta(days=1)
        r = deliver_invoice(None, "i2", "a@b.com", "NotSet", "t", "r")
        assert r["ok"] is False and "due date" in r["error"] and calls["sent"] == 1
        # first send, due today -> sends + records
        due_by_id["i3"] = date.today()
        r = deliver_invoice(None, "i3", "a@b.com", "NotSet", "t", "r")
        assert r["ok"] and calls["sent"] == 2 and calls["marked"] == 2
        # first send, no due date on record -> sends (no false block)
        r = deliver_invoice(None, "i4", "a@b.com", "NotSet", "t", "r")
        assert r["ok"] and calls["sent"] == 3
    finally:
        g["send_invoice"], g["mark_emailed"], g["_due_date"] = real_send, real_mark, real_due
    return "ok"


def main():
    return {"selfcheck": _selfcheck()}
