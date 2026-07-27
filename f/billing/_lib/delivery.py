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

Import as:  from f.billing._lib.delivery import deliver_invoice, send_and_record
"""

from datetime import date

from f.billing._lib.qbo import (
    send_invoice, send_invoice_email, bump_invoice_due_date_to_today,
    fetch_qbo_invoice,
)
from f.billing._lib.cache import mark_emailed, echo_invoice
# NOT the charge WAL — only the webhook expectation is shared. Importing
# create_attempt/update_attempt here is what let a plain email write a
# processing_attempts row and emit charge_attempted.
from f.billing._lib.wal import insert_webhook_expectation
from f.billing._lib.events import emit

SEND_RETRIES = 3
SEND_BACKOFF_S = 5


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


def send_and_record(conn, invoice_row, balance, stage, access_token, realm_id):
    """The WORKFLOW send: WAL-book an attempt, remedy a past-due first send by
    bumping the due date (we have the primitive; refusing was the pre-bump
    rule), send with retries, emit invoice_emailed, echo the mirror. Shared by
    any engine that delivers as part of processing. Returns {success, ...}."""
    import time
    qbo_invoice_id = invoice_row["qbo_invoice_id"]

    # A send books NOTHING ahead of itself, and that is deliberate.
    #
    # It used to call create_attempt — the CHARGE write-ahead log — which wrote
    # a processing_attempts row and emitted charge_attempted. Emailing an
    # invoice is not a charge attempt: it made billing.charge_attempted()
    # ambiguous and put a charge event on invoices that were never charged.
    #
    # And a send needs no WAL of its own. A WAL exists so an interrupted effect
    # is recoverable, which matters for a credit application because "did we
    # already do this?" is unanswerable without rebuilding LinkedTxn. A send is
    # different: QBO's own EmailStatus answers it, and re-sending is idempotent
    # (QBO skips an already-EmailSent invoice). The event stream is the log —
    # invoice_emailed on success, processing_failed with the error via the
    # queue on failure. When inbound/outbound email gets its own table, the
    # emit is what feeds it.

    if (balance or 0) > 0:  # unpaid + emailed shouldn't arrive OVERDUE
        # conn is passed so the bump goes through update_invoice_sparse and
        # emits invoice_edited {due_date: from -> to} like any other edit
        due = bump_invoice_due_date_to_today(qbo_invoice_id, access_token, realm_id,
                                             conn=conn)
        if due.get("success") and not due.get("skipped"):
            insert_webhook_expectation(conn, "Invoice", qbo_invoice_id)

    last_error = None
    for i in range(SEND_RETRIES):
        email = send_invoice_email(qbo_invoice_id, invoice_row.get("qbo_customer_id"),
                                   access_token, realm_id)
        if email["success"]:
            # Emit even when QBO reports it already EmailSent. The emit used to
            # be suppressed on `skipped`, which meant a retry, resume or
            # re-drain after a partial failure delivered the invoice and left
            # NO record — the same shape as the credit application that lost
            # $1,000 on 2026-07-26. `skipped` says who sent it, not whether it
            # happened; billing.invoice_emailed is deduped by the fold, not by
            # us declining to write it.
            emit(conn, "invoice", qbo_invoice_id, "invoice_emailed",
                 participants=[f"customer:{invoice_row.get('qbo_customer_id')}"],
                 payload={"sent_to": email.get("sent_to"),
                          "already_sent": bool(email.get("skipped")),
                          "provenance": {"source": "intent" if not email.get("skipped")
                                                   else "external",
                                         "intent_ref": stage}})
            if not email.get("skipped"):
                insert_webhook_expectation(conn, "Invoice", qbo_invoice_id)
            fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id, conn=conn)
            return {"success": True, "sent_to": email.get("sent_to"),
                    "skipped": email.get("skipped", False)}
        last_error = email.get("error")
        if i + 1 < SEND_RETRIES:
            time.sleep(SEND_BACKOFF_S)

    # No row to close: the failure surfaces as processing_failed (the queue
    # writes the error) and the invoice simply stays unsent, which the gate sees.
    return {"success": False, "error": last_error}


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
        # send_and_record books the SEND LOG (not the charge WAL), emits
        # invoice_emailed, and never emits a charge event
        emits, bumps = [], []
        saved = {k: g[k] for k in ("insert_webhook_expectation", "emit",
                                   "send_invoice_email", "bump_invoice_due_date_to_today",
                                   "fetch_qbo_invoice", "echo_invoice")}
        g.update(
            insert_webhook_expectation=lambda c, t, i: None,
            emit=lambda *a, **k: emits.append(a[3]),
            send_invoice_email=lambda q, cu, at, r: {"success": True, "sent_to": "x@y"},
            bump_invoice_due_date_to_today=lambda q, at, r, conn=None: (
                bumps.append(conn) or {"success": True}),
            fetch_qbo_invoice=lambda q, at, r, conn=None: (None, None),
            echo_invoice=lambda c, q, b: None)
        try:
            inv = {"qbo_invoice_id": "i9", "qbo_customer_id": "c1",
                   "doc_number": "1", "wo_number": "w1", "payment_method": None}
            r = send_and_record(None, inv, 50.0, "process", "t", "r")
            assert r["success"], "send should succeed"
            assert emits == ["invoice_emailed"], emits
            assert "charge_attempted" not in emits, "a send must NOT emit a charge event"
            assert bumps == [None], "the bump must receive conn so it emits invoice_edited"

            # unpaid=0 -> no due-date bump (nothing would arrive overdue)
            bumps.clear()
            g["send_invoice_email"] = lambda q, cu, at, r: {"success": False, "error": "boom"}
            g["SEND_BACKOFF_S"] = 0
            r = send_and_record(None, inv, 0, "process", "t", "r")
            assert not r["success"] and r["error"] == "boom"
            assert bumps == [], "settled invoice needs no due-date bump"

            # a QBO-side 'skipped' send still emits: a retry must not deliver silently
            emits.clear()
            g["send_invoice_email"] = lambda q, cu, at, r: {
                "success": True, "skipped": True, "sent_to": "x@y"}
            r = send_and_record(None, inv, 0, "process", "t", "r")
            assert r["success"] and r["skipped"] and emits == ["invoice_emailed"], emits
        finally:
            g.update(saved)
    finally:
        g["send_invoice"], g["mark_emailed"], g["_due_date"] = real_send, real_mark, real_due
    return "ok"


def main():
    return {"selfcheck": _selfcheck()}
