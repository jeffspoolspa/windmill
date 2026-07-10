# requirements:
# psycopg2-binary
# requests
# wmill

"""
f/billing/_lib/payments — the shared payment service (ADR 009 §B).

charge_and_record is the idempotent charge core: WAL find-or-create +
fresh-read + charge + QBO Payment create + best-effort receipt. It is
SEGMENT-BLIND — it never learns what kind of invoice it is charging. All
billing policy (decline handling, invoice-copy delivery, cache echo, roster
health, group anchoring, needs_review marks) stays in the engines and
dispatches on the returned status.

Invariants (ADR 009 addendum):
  - The service reads every invoice balance FRESH and decides the amount;
    the intent carries invoice ids, never a number. A failed read HALTS
    (status=read_failed, no cache fallback); balance <= 0 → already_paid.
  - The WAL row commits BEFORE the charge (crash-recoverable; a resume
    reuses the persisted idempotency key — Intuit dedupes on Request-Id).
    Resumes are exempt from the fresh-read guard (the balance may read 0
    because of our own in-flight charge) and charge the PERSISTED amount.
  - The receipt runs last, after the money is durable, and its failure is
    returned as receipt_sent=False — never a charge failure. The switch is
    DATA (intent.receipt_email present or None), not a boolean flag.
  - prior payment_orphan REFUSES to auto-resume: record_qbo_payment is not
    idempotent, so retrying a failed record without human verification in
    QBO/Intuit risks a double-recorded payment. Orphan recovery is an
    explicit engine sentence over the primitives.

Import as:  from f.billing._lib.payments import charge_and_record, ...
"""

from datetime import datetime, timezone, timedelta

import psycopg2.extras

from f.billing._lib.qbo import (
    charge_card, charge_bank_account, get_qbo_invoice_details,
    record_qbo_payment, send_receipt,
)
from f.billing._lib.wal import (
    latest_attempt, create_attempt, update_attempt,
    insert_webhook_expectation, dumps,
)

# Intuit's Request-Id idempotency cache window. Past it, an uncertain
# attempt's key would be treated as a NEW charge — so we expire the attempt
# and issue a fresh key (worst case a MISSING charge, never a double one).
UNCERTAIN_REUSE_WINDOW_H = 24


def _stored_group_lines(attempt):
    """[[qbo_invoice_id, amount], ...] persisted on a multi-line anchor
    attempt, or None. Stored at create time so an interrupted charge resumes
    with its ORIGINAL membership/amounts — never a re-mixed set."""
    import json
    raw = attempt.get("raw_result")
    if not raw:
        return None
    try:
        d = raw if isinstance(raw, dict) else json.loads(raw)
        return d.get("group_lines")
    except Exception:
        return None


def charge_and_record(conn, intent, access_token, realm_id, dry_run=False):
    """The payment port. intent (all policy passed as data):

      stage              WAL scope ('process' | 'maint')
      qbo_invoice_id     anchor invoice (WAL identity)
      lines              [qbo_invoice_id, ...] this ONE charge covers
                         (default: just the anchor; anchor must be first)
      payment_method_id  Intuit cardOnFile / bankAccountOnFile id
      cpm_id             our customer_payment_methods uuid (audit link)
      channel            'ach' | 'card' | 'credit_card'
      customer_id        QBO customer id
      customer_name      for the charge description
      invoice_number     doc-number label (charge description + WAL row)
      wo_number          WAL column (service-billing rows; else None)
      payment_method     legacy WAL text column override (else channel)
      payment_ref        QBO PaymentRefNum
      memo_prefix        policy half of the payment PrivateNote
      receipt_email      where the receipt goes, or None for no receipt

    Returns {status, amount, balances, charge_id, payment_id, receipt_sent,
    receipt_error, error, attempt_id, resumed} with status one of:
    read_failed | already_paid | would_charge | uncertain | declined |
    payment_orphan | succeeded.
    """
    stage = intent["stage"]
    anchor = intent["qbo_invoice_id"]
    line_ids = list(intent.get("lines") or [anchor])
    channel = "ach" if intent["channel"] == "ach" else "card"

    def res(status, **rest):
        return {"status": status, "amount": rest.pop("amount", None),
                "balances": rest.pop("balances", None),
                "charge_id": rest.pop("charge_id", None),
                "payment_id": rest.pop("payment_id", None),
                "receipt_sent": rest.pop("receipt_sent", False),
                "receipt_error": rest.pop("receipt_error", None),
                "error": rest.pop("error", None),
                "attempt_id": rest.pop("attempt_id", None),
                "resumed": rest.pop("resumed", None), **rest}

    # ── prior-attempt dispatch (mechanism: Intuit idempotency semantics) ──
    prior = latest_attempt(conn, anchor, stage)
    reuse, resumed = None, None
    if prior:
        st = prior["status"]
        if st == "payment_orphan":
            return res("payment_orphan", attempt_id=str(prior["id"]),
                       charge_id=prior.get("charge_id"),
                       amount=float(prior.get("charge_amount") or 0),
                       error="prior attempt is payment_orphan — human recovery only "
                             "(a blind record_payment retry can double-record)")
        if st == "pending":
            reuse = prior  # key never used; fresh-read guard still applies
        elif st == "charge_uncertain":
            attempted_at = prior.get("attempted_at")
            if attempted_at and attempted_at.tzinfo is None:
                attempted_at = attempted_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - attempted_at) if attempted_at else timedelta()
            if age > timedelta(hours=UNCERTAIN_REUSE_WINDOW_H):
                update_attempt(conn, prior["id"], status="charge_uncertain_expired",
                               error_message=(prior.get("error_message") or "")
                               + " | expired after 24h by charge_and_record")
            else:
                reuse, resumed = prior, "charge_uncertain"
        elif st == "charge_succeeded":
            reuse, resumed = prior, "charge_succeeded"
        # succeeded / declined / expired → fall through to a fresh attempt
        # (fresh key + fresh-read guard). Whether a re-attempt is ALLOWED is
        # the engine's pre-flight policy, decided before calling here.

    # ── amount: fresh leader read, or the persisted in-flight facts ──
    if resumed:
        amount = round(float(reuse["charge_amount"] or 0), 2)
        lines = ([(inv, float(amt)) for inv, amt in _stored_group_lines(reuse) or []]
                 or [(anchor, amount)])
        balances = None
    else:
        balances = {}
        for inv_id in line_ids:
            fresh = get_qbo_invoice_details(inv_id, realm_id, access_token)
            if fresh is None:
                return res("read_failed", balances=balances or None,
                           error="fresh QBO invoice read failed — charge held "
                                 "(no stale-cache fallback)")
            balances[inv_id] = fresh["balance"]
        if any(b <= 0 for b in balances.values()):
            return res("already_paid", balances=balances,
                       error="invoice balance <= 0 at fresh read (paid upstream)")
        lines = [(inv_id, round(balances[inv_id], 2)) for inv_id in line_ids]
        amount = round(sum(amt for _, amt in lines), 2)

    if dry_run:
        return res("would_charge", amount=amount, balances=balances, resumed=resumed)

    # ── WAL anchor (write-ahead: committed before the charge fires) ──
    if reuse is not None:
        attempt = reuse
    else:
        attempt = create_attempt(
            conn, anchor, stage, intent.get("invoice_number"), channel, amount,
            False, cpm_id=intent.get("cpm_id"), wo_number=intent.get("wo_number"),
            payment_method=intent.get("payment_method"))
        if len(lines) > 1:
            update_attempt(conn, attempt["id"],
                           raw_result=dumps({"group_lines": lines}))

    def _raw(extra):
        base = {"group_lines": lines} if len(lines) > 1 else {}
        return dumps({**base, **extra})

    # ── charge (skipped when resuming past a completed charge) ──
    if attempt["status"] in ("pending", "charge_uncertain"):
        fn = charge_bank_account if channel == "ach" else charge_card
        cr = fn(intent["payment_method_id"], amount, attempt["idempotency_key"],
                intent.get("invoice_number") or "", intent.get("customer_name") or "",
                access_token)
        cls = cr["classification"]
        if cls == "uncertain":
            update_attempt(conn, attempt["id"], status="charge_uncertain",
                           error_message=cr.get("error"),
                           charge_result=dumps(cr), raw_result=_raw({"charge": cr}))
            return res("uncertain", amount=amount, balances=balances,
                       attempt_id=str(attempt["id"]), error=cr.get("error"),
                       resumed=resumed)
        if cls == "declined":
            update_attempt(conn, attempt["id"], status="charge_declined",
                           error_message=cr.get("error"),
                           charge_result=dumps(cr), raw_result=_raw({"charge": cr}))
            return res("declined", amount=amount, balances=balances,
                       attempt_id=str(attempt["id"]), error=cr.get("error"),
                       resumed=resumed)
        update_attempt(conn, attempt["id"], status="charge_succeeded",
                       charge_id=cr.get("charge_id"),
                       charge_result=dumps(cr), raw_result=_raw({"charge": cr}))
    else:
        # charge_succeeded resume: money moved; finish the bookkeeping
        cr = {"charge_id": attempt["charge_id"],
              "payment_type": "ach" if channel == "ach" else "card",
              "amount": amount, "card_type": None, "card_last4": None,
              "auth_code": "", "status": "CAPTURED"}

    # ── QBO Payment (resume-safe: never re-record an existing one) ──
    if attempt.get("qbo_payment_id"):
        rec = {"success": True, "payment_id": attempt["qbo_payment_id"]}
    else:
        rec = record_qbo_payment(intent["customer_id"], cr.get("amount", amount), cr,
                                 intent.get("payment_ref"), intent.get("memo_prefix", ""),
                                 access_token, realm_id, lines)
    if not rec["success"]:
        update_attempt(conn, attempt["id"], status="payment_orphan",
                       error_message=f"record_payment failed: {str(rec.get('error'))[:300]}")
        return res("payment_orphan", amount=amount,
                   attempt_id=str(attempt["id"]), charge_id=cr.get("charge_id"),
                   error=rec.get("error"), resumed=resumed)
    update_attempt(conn, attempt["id"], qbo_payment_id=rec["payment_id"])
    insert_webhook_expectation(conn, "Payment", rec["payment_id"])

    # ── receipt: best-effort, after the money is durable, switched by DATA ──
    receipt_sent, receipt_error = False, None
    if intent.get("receipt_email"):
        r = send_receipt(rec["payment_id"], intent["receipt_email"],
                         access_token, realm_id)
        receipt_sent, receipt_error = r["ok"], r["error"]

    update_attempt(conn, attempt["id"], status="succeeded",
                   raw_result=_raw({"charge": cr,
                                    "payment": {"payment_id": rec["payment_id"]},
                                    "receipt_sent": receipt_sent,
                                    "receipt_error": receipt_error}))
    return res("succeeded", amount=amount, balances=balances,
               attempt_id=str(attempt["id"]), charge_id=cr.get("charge_id"),
               payment_id=rec["payment_id"], receipt_sent=receipt_sent,
               receipt_error=receipt_error, resumed=resumed)


# ── payment-method resolution (the 3 divergent engine copies, unified) ──────

def resolve_payment_method(conn, customer_id=None, preferred_type=None, cpm_id=None):
    """Pick the instrument to charge, from the DB cache (refreshed 4h by
    pull_customer_payment_methods; the row links processing_attempts back to
    the exact card charged).

    cpm_id given → load that exact row (the target pre-processing picked);
    inactive/missing comes back has_method=False with the reason.
    Otherwise → QBO-flagged defaults only (never surprise-charge a non-default),
    preferring preferred_type, else the most recently added default.
    """
    if cpm_id:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, qbo_payment_method_id, type, card_brand, last_four,
                   is_default, is_active, raw, auto_disabled_at, auto_disabled_reason
            FROM billing.customer_payment_methods
            WHERE id = %s
        """, (cpm_id,))
        row = cur.fetchone(); cur.close()
        if not row:
            return {"has_method": False, "error": f"target_payment_method_id {cpm_id} not found"}
        if not row.get("is_active"):
            reason = row.get("auto_disabled_reason") or "manually deactivated"
            return {"has_method": False,
                    "error": f"target PM is no longer active ({reason})",
                    "stale_cpm_id": str(row["id"])}
        return _pm_row_to_result(dict(row), picked_reason="invoice_target")

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if preferred_type in ("card", "credit_card", "ach"):
        normalized = "credit_card" if preferred_type in ("card", "credit_card") else "ach"
        cur.execute("""
            SELECT id, qbo_payment_method_id, type, card_brand, last_four,
                   is_default, raw
            FROM billing.customer_payment_methods
            WHERE qbo_customer_id = %s
              AND is_active = true AND is_default = true AND type = %s
            ORDER BY (raw->>'created') DESC NULLS LAST
            LIMIT 1
        """, (customer_id, normalized))
        row = cur.fetchone()
        if row:
            cur.close()
            return _pm_row_to_result(dict(row), picked_reason="user_override")

    cur.execute("""
        SELECT id, qbo_payment_method_id, type, card_brand, last_four,
               is_default, raw
        FROM billing.customer_payment_methods
        WHERE qbo_customer_id = %s
          AND is_active = true AND is_default = true
        ORDER BY (raw->>'created') DESC NULLS LAST
        LIMIT 1
    """, (customer_id,))
    row = cur.fetchone(); cur.close()
    if not row:
        return {"has_method": False,
                "error": "No default card or bank account on file (DB cache)"}
    return _pm_row_to_result(dict(row), picked_reason="most_recent_default")


def _pm_row_to_result(row, picked_reason):
    raw = row.get("raw") or {}
    base = {
        "has_method": True,
        "payment_type": row["type"],   # 'credit_card' | 'ach'
        "method_id": row["qbo_payment_method_id"],
        "cpm_id": str(row["id"]),
        "last4": row.get("last_four"),
        "is_default": bool(row.get("is_default")),
        "picked_reason": picked_reason,
    }
    if row["type"] in ("credit_card", "card"):
        return {**base, "card_type": row.get("card_brand"),
                "exp_month": raw.get("expMonth"), "exp_year": raw.get("expYear")}
    return {**base, "bank_name": row.get("card_brand") or "Bank"}


def load_applicable_credits(conn, qbo_customer_id):
    """Pre-charge safety net: unapplied credits that COULD cover this charge.
    Excludes maintenance-scoped credits (memo ~* 'maint') and stale ones
    (>6 months). What to DO about them (halt / apply / override) is engine
    policy — this is just the read."""
    if not qbo_customer_id:
        return []
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT qbo_payment_id, type, unapplied_amt, total_amt, txn_date, ref_num, memo
        FROM billing.customer_payments
        WHERE qbo_customer_id = %s
          AND unapplied_amt > 0
          AND (memo IS NULL OR memo !~* 'maint')
          AND (txn_date IS NULL OR txn_date >= (now() - interval '6 months')::date)
        ORDER BY txn_date DESC NULLS LAST
    """, (qbo_customer_id,))
    rows = [dict(r) for r in cur.fetchall()]; cur.close()
    return rows


# ── self-check: fakes swapped into this module's namespace, NO network/DB ───

def _selfcheck():
    g = globals()
    real = {k: g[k] for k in ("latest_attempt", "create_attempt", "update_attempt",
                              "insert_webhook_expectation", "get_qbo_invoice_details",
                              "charge_card", "charge_bank_account",
                              "record_qbo_payment", "send_receipt")}
    checks, calls = [], []
    def ok(name, cond):
        checks.append((name, bool(cond)))

    state = {"prior": None, "fresh": {}, "charge": None, "record": None,
             "receipt": {"ok": True, "error": None}, "updates": []}

    def fake_latest(conn, inv, stage):
        calls.append("latest"); return state["prior"]
    def fake_create(conn, inv, stage, invoice_number, channel, amount, dry_run, **kw):
        calls.append("create")
        return {"id": "A1", "status": "pending", "idempotency_key": "KEY-1",
                "charge_amount": amount, "qbo_payment_id": None}
    def fake_update(conn, attempt_id, **fields):
        state["updates"].append(fields)
    def fake_expect(conn, et, eid):
        calls.append(f"expect:{et}")
    def fake_fresh(inv, realm, at):
        calls.append(f"fresh:{inv}"); return state["fresh"].get(inv)
    def fake_charge(pmid, amount, key, num, name, at):
        calls.append(f"charge:{amount}:{key}"); return state["charge"]
    def fake_record(cust, amount, cr, ref, memo, at, rid, lines):
        calls.append(f"record:{amount}:{len(lines)}"); return state["record"]
    def fake_receipt(pid, email, at, rid):
        calls.append(f"receipt:{email}"); return state["receipt"]

    g.update(latest_attempt=fake_latest, create_attempt=fake_create,
             update_attempt=fake_update, insert_webhook_expectation=fake_expect,
             get_qbo_invoice_details=fake_fresh, charge_card=fake_charge,
             charge_bank_account=fake_charge, record_qbo_payment=fake_record,
             send_receipt=fake_receipt)
    try:
        base_intent = {"stage": "maint", "qbo_invoice_id": "I1", "channel": "card",
                       "payment_method_id": "pm1", "customer_id": "C1",
                       "customer_name": "Jane", "invoice_number": "1042",
                       "payment_ref": "1042", "memo_prefix": "Test | Inv# 1042",
                       "receipt_email": "j@x.com"}

        # 1. failed fresh read HALTS
        state["fresh"] = {"I1": None}
        r = charge_and_record(None, dict(base_intent), "at", "rid")
        ok("read_failed halts", r["status"] == "read_failed" and "create" not in calls)

        # 2. balance <= 0 → already_paid, no WAL row
        state["fresh"] = {"I1": {"balance": 0.0, "email_status": None}}
        r = charge_and_record(None, dict(base_intent), "at", "rid")
        ok("zero balance -> already_paid", r["status"] == "already_paid"
           and "create" not in calls)

        # 3. dry_run reads fresh, decides amount, writes NOTHING
        state["fresh"] = {"I1": {"balance": 42.5, "email_status": None}}
        r = charge_and_record(None, dict(base_intent), "at", "rid", dry_run=True)
        ok("dry_run -> would_charge with fresh amount",
           r["status"] == "would_charge" and r["amount"] == 42.5
           and "create" not in calls and not state["updates"])

        # 4. declined: WAL charge_declined, no payment, no receipt
        calls.clear(); state["updates"].clear()
        state["charge"] = {"classification": "declined", "error": "card expired"}
        r = charge_and_record(None, dict(base_intent), "at", "rid")
        ok("declined -> WAL + no downstream calls",
           r["status"] == "declined" and r["error"] == "card expired"
           and state["updates"][-1]["status"] == "charge_declined"
           and not any(c.startswith(("record", "receipt")) for c in calls))

        # 5. success end-to-end: fresh amount charged w/ persisted key,
        #    payment recorded, expectation inserted, receipt sent, WAL succeeded
        calls.clear(); state["updates"].clear()
        state["charge"] = {"classification": "success", "charge_id": "ch9",
                           "amount": 42.5, "payment_type": "card",
                           "auth_code": "A", "card_type": "Visa", "card_last4": "4242"}
        state["record"] = {"success": True, "payment_id": "P77"}
        r = charge_and_record(None, dict(base_intent), "at", "rid")
        ok("success charges fresh amount with persisted key",
           "charge:42.5:KEY-1" in calls)
        ok("success records + expects + receipts + succeeds",
           r["status"] == "succeeded" and r["payment_id"] == "P77"
           and r["receipt_sent"] is True and "expect:Payment" in calls
           and any(u.get("status") == "succeeded" for u in state["updates"]))

        # 6. receipt_email=None -> no receipt call (data switch, not a flag)
        calls.clear()
        r = charge_and_record(None, {**base_intent, "receipt_email": None}, "at", "rid")
        ok("null receipt_email skips the send",
           r["status"] == "succeeded" and r["receipt_sent"] is False
           and not any(c.startswith("receipt") for c in calls))

        # 7. record failure -> payment_orphan (money moved, ledger didn't)
        calls.clear(); state["updates"].clear()
        state["record"] = {"success": False, "error": "QBO 500"}
        r = charge_and_record(None, dict(base_intent), "at", "rid")
        ok("record failure -> payment_orphan",
           r["status"] == "payment_orphan" and r["charge_id"] == "ch9"
           and any(u.get("status") == "payment_orphan" for u in state["updates"]))

        # 8. prior charge_succeeded resumes WITHOUT re-charging
        calls.clear(); state["updates"].clear()
        state["record"] = {"success": True, "payment_id": "P78"}
        state["prior"] = {"id": "A0", "status": "charge_succeeded", "charge_id": "chX",
                          "charge_amount": 30.0, "qbo_payment_id": None,
                          "raw_result": None, "attempted_at": None,
                          "error_message": None, "idempotency_key": "OLDKEY"}
        r = charge_and_record(None, dict(base_intent), "at", "rid")
        ok("charge_succeeded resume skips charge + fresh read, records payment",
           r["status"] == "succeeded" and r["resumed"] == "charge_succeeded"
           and r["charge_id"] == "chX" and r["amount"] == 30.0
           and not any(c.startswith(("charge", "fresh")) for c in calls))

        # 9. prior payment_orphan REFUSES
        state["prior"] = {"id": "A0", "status": "payment_orphan", "charge_id": "chX",
                          "charge_amount": 30.0}
        r = charge_and_record(None, dict(base_intent), "at", "rid")
        ok("payment_orphan refuses auto-resume", r["status"] == "payment_orphan")

        # 10. multi-line: fresh-reads every member, one charge for the sum
        calls.clear(); state["prior"] = None
        state["fresh"] = {"I1": {"balance": 10.0, "email_status": None},
                          "I2": {"balance": 15.5, "email_status": None}}
        state["record"] = {"success": True, "payment_id": "P79"}
        r = charge_and_record(None, {**base_intent, "lines": ["I1", "I2"]}, "at", "rid")
        ok("group: one charge for the summed fresh balances, one 2-line payment",
           r["status"] == "succeeded" and r["amount"] == 25.5
           and "charge:25.5:KEY-1" in calls and "record:25.5:2" in calls)
    finally:
        g.update(real)

    failed = [n for n, p in checks if not p]
    return {"passed": len(checks) - len(failed), "total": len(checks), "failed": failed}


def main():
    """No-network/no-DB self-check of the service orchestration (money code —
    run after every deploy)."""
    result = _selfcheck()
    result["ok"] = not result["failed"]
    return result


if __name__ == "__main__":
    print(main())
