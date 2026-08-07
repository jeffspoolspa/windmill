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
    record_qbo_payment, send_receipt, apply_credit, fetch_qbo_customer_email,
)
from f.billing._lib.wal import (
    latest_attempt, create_attempt, update_attempt,
    insert_webhook_expectation, dumps,
)
from f.billing._lib.cache import echo_payment
from f.billing._lib.events import emit

import psycopg2.extras

# Intuit's Request-Id idempotency cache window. Past it, an uncertain
# attempt's key would be treated as a NEW charge — so we expire the attempt
# and issue a fresh key (worst case a MISSING charge, never a double one).
UNCERTAIN_REUSE_WINDOW_H = 24


def stored_group_lines(attempt):
    """[[qbo_invoice_id, amount], ...] persisted on a multi-line anchor
    attempt, or None. Stored at create time so an interrupted charge resumes
    with its ORIGINAL membership/amounts — never a re-mixed set. Public:
    engines use it to detect an in-flight group anchor in pre-flight."""
    import json
    raw = attempt.get("raw_result")
    if not raw:
        return None
    try:
        d = raw if isinstance(raw, dict) else json.loads(raw)
        return d.get("group_lines")
    except Exception:
        return None


def _find_recorded_payment(conn, charge_id):
    """Leg-2 dedupe: did a QBO Payment for this Intuit charge already land?
    The Payment we create carries the CCTransId; the cache exposes it
    (cc_trans_id, converged by webhook + CDC). Found -> the 'failed' record
    actually succeeded and its response was lost. None on any error (treat
    as unproven — never heal on a guess)."""
    if not charge_id:
        return None
    try:
        cur = conn.cursor()
        # 1) the charge row's own link (stamped at record time — intent-arm)
        cur.execute("SELECT qbo_payment_id FROM billing.charges "
                    "WHERE charge_id = %s AND qbo_payment_id IS NOT NULL", (charge_id,))
        row = cur.fetchone()
        if not row:
            # 2) the reflection: a cached Payment carrying this CCTransId
            #    (covers the response-lost case where we never learned the id)
            cur.execute("SELECT qbo_payment_id FROM billing.customer_payments "
                        "WHERE cc_trans_id = %s LIMIT 1", (charge_id,))
            row = cur.fetchone()
        cur.close()
        return row[0] if row else None
    except Exception as e:
        print(f"  (orphan lookup warning: {e})")
        return None


def _link_charge_payment(conn, charge_id, payment_id):
    """Close the saga on the charge row: leg 2's id stamped onto leg 1's
    fact. Best-effort — the link is evidence, never the money path."""
    if not (charge_id and payment_id):
        return
    try:
        cur = conn.cursor()
        cur.execute("UPDATE billing.charges SET qbo_payment_id = %s "
                    "WHERE charge_id = %s", (payment_id, charge_id))
        conn.commit(); cur.close()
    except Exception as e:
        print(f"  (charge link warning: {e})")


#: Journal statuses that mean "money moved" (billing.charges CHECK vocab).
_SETTLED_CHARGE = ("succeeded", "settled", "captured", "recorded", "receipted")


def latest_charge(conn, qbo_invoice_id):
    """The journal's latest word on this invoice. billing.charges is the ONE
    source of outcome truth (RULED 2026-08-06) — the gate reads it
    (charge_attempted), the TS engine writes it, and the dispatch below asks
    it first. Legacy rows may lack attempted_at, hence the coalesce."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT * FROM billing.charges WHERE qbo_invoice_id = %s
           ORDER BY coalesce(attempted_at, updated_at, first_seen_at) DESC NULLS LAST
           LIMIT 1""",
        (qbo_invoice_id,))
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None


def attempt_by_key(conn, idempotency_key):
    """The WAL row a journal row's key points at — WORKING STATE for resume
    (group lines, ladder position), never consulted for an outcome the
    journal can answer."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM billing.processing_attempts WHERE idempotency_key = %s",
        (idempotency_key,))
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None


def _record_charge_intent(conn, qbo_invoice_id, attempt, channel, amount, cpm_id):
    """WRITE-AHEAD on the JOURNAL: the 'requested' row, committed before the
    charge fires. The WAL row (create_attempt) is working state written in
    lockstep; THIS row is what the dispatch reads. ON CONFLICT DO NOTHING —
    a resume reuses its intent row. Best-effort like every reflection."""
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO billing.charges
                 (qbo_invoice_id, payment_type, status, amount, idempotency_key,
                  customer_payment_method_id, raw, source, attempted_at)
               VALUES (%s, %s, 'requested', %s, %s, %s::uuid, '{}'::jsonb, 'live', now())
               ON CONFLICT (idempotency_key) DO NOTHING""",
            (qbo_invoice_id, "ach" if channel == "ach" else "card", amount,
             attempt["idempotency_key"], cpm_id))
        conn.commit(); cur.close()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"  (intent reflection warning: {e})")


def _expire_uncertain_charge(conn, prior_c):
    """Past Intuit's Request-Id window an uncertain journal row can never be
    resolved by key reuse — mark it error/expired so it stops answering the
    dispatch. (Vocab has no 'expired'; 'error' + message carries it.)"""
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE billing.charges SET status = 'error',
                 error_message = coalesce(error_message, '')
                                 || ' | expired after 24h by charge_and_record',
                 updated_at = now()
               WHERE id = %s""", (prior_c["id"],))
        conn.commit(); cur.close()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"  (expiry reflection warning: {e})")


#: Intuit's status vocabulary -> ours. billing.charges has a CHECK on this;
#: the vendor's literal wording survives in `raw`.
_CHARGE_STATUS = {"CAPTURED": "succeeded", "SETTLED": "succeeded",
                  "DECLINED": "declined", "CANCELLED": "error",
                  "FAILED": "error", "ERROR": "error"}


def charge_status(cr):
    """Our status for a charge result. `classification` is the money-path
    verdict and outranks Intuit's wording — an uncertain charge is uncertain
    even if a response body says CAPTURED."""
    cls = (cr.get("classification") or "").lower()
    if cls == "uncertain":
        return "uncertain"
    raw_status = (cr.get("status") or (cr.get("raw_response") or {}).get("status") or "")
    return _CHARGE_STATUS.get(raw_status.upper(),
                              "succeeded" if cls == "success" else
                              "declined" if cls == "declined" else "error")


def _upsert_charge(conn, cr, qbo_invoice_id, idempotency_key=None, cpm_id=None):
    """Record the charge OUTCOME on the journal row its intent opened.

    One row per attempt, keyed by idempotency_key (RULED 2026-08-06): the
    'requested' row is written before Intuit is called and the outcome
    CONVERGES it — never a second row. Fallbacks, in order:
      key row exists      -> update it in place (charge_id, status, echo)
      charge_id collision -> the Intuit daily sync inserted this charge
                             first: converge the sync row, mark the intent
                             row superseded (evidence, never the money path)
      no key (legacy)     -> prior behavior: upsert by charge_id, or a bare
                             insert so a no-id decline is still on record

    Best-effort: a reflection failure never fails the money path.
    Returns Intuit's charge_id when there is one, else None.
    """
    raw = cr.get("raw_response") or {}
    charge_id = cr.get("charge_id") or raw.get("id")
    status = charge_status(cr)
    try:
        cur = conn.cursor()
        converged = False
        if idempotency_key:
            try:
                cur.execute("""
                    UPDATE billing.charges SET
                      charge_id = coalesce(%s, charge_id), payment_type = %s,
                      status = %s, amount = coalesce(%s, amount),
                      auth_code = %s, card_type = %s, card_last4 = %s,
                      error_message = %s, raw = %s::jsonb,
                      customer_payment_method_id =
                        coalesce(%s::uuid, customer_payment_method_id),
                      attempted_at = coalesce(attempted_at, now()),
                      updated_at = now()
                    WHERE idempotency_key = %s
                """, (charge_id, cr.get("payment_type"), status,
                      cr.get("amount") or raw.get("amount"),
                      cr.get("auth_code") or raw.get("authCode"),
                      cr.get("card_type") or (raw.get("card") or {}).get("cardType"),
                      cr.get("card_last4"), cr.get("error"), dumps(raw),
                      cpm_id, idempotency_key))
                converged = cur.rowcount > 0
                conn.commit()
            except Exception as merge_e:
                # unique(charge_id) collision: the sync row got there first
                try:
                    conn.rollback()
                except Exception:
                    pass
                if charge_id and "charge_id" in str(merge_e):
                    cur = conn.cursor()
                    cur.execute("""
                        UPDATE billing.charges SET status = 'error',
                          error_message = 'superseded: charge landed on sync row',
                          updated_at = now()
                        WHERE idempotency_key = %s AND charge_id IS NULL
                    """, (idempotency_key,))
                    conn.commit()
                else:
                    raise
        if not converged and charge_id:
            # Intuit gave us an identity — converge on it (legacy path, and
            # the sync-collision path's second half)
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO billing.charges
                  (qbo_invoice_id, charge_id, payment_type, status, amount,
                   auth_code, card_type, card_last4, error_message, raw,
                   customer_payment_method_id, attempted_at, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                        %s::uuid, now(), 'live')
                ON CONFLICT (charge_id) WHERE charge_id IS NOT NULL DO UPDATE SET
                  status = EXCLUDED.status, raw = EXCLUDED.raw,
                  qbo_invoice_id = coalesce(billing.charges.qbo_invoice_id,
                                            EXCLUDED.qbo_invoice_id),
                  customer_payment_method_id =
                    coalesce(billing.charges.customer_payment_method_id,
                             EXCLUDED.customer_payment_method_id),
                  updated_at = now()
            """, (qbo_invoice_id, charge_id, cr.get("payment_type"),
                  status, cr.get("amount") or raw.get("amount"),
                  cr.get("auth_code") or raw.get("authCode"),
                  cr.get("card_type") or (raw.get("card") or {}).get("cardType"),
                  cr.get("card_last4"), cr.get("error"), dumps(raw), cpm_id))
            conn.commit()
        elif not converged:
            # No identity from Intuit and no intent row to converge — still a
            # real attempt against this invoice, and the ONLY record we tried.
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO billing.charges
                  (qbo_invoice_id, payment_type, status, amount,
                   error_message, raw, customer_payment_method_id,
                   attempted_at, source)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::uuid, now(), 'live')
            """, (qbo_invoice_id, cr.get("payment_type"), status,
                  cr.get("amount") or raw.get("amount"),
                  cr.get("error"), dumps(raw), cpm_id))
            conn.commit()
        cur.close()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"  (charge reflection warning: {e})")
    return charge_id


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
      invoice_number     doc-number label (WAL row + charge description)
      charge_label       optional charge-description override (group docs list)
      wo_number          WAL column (service-billing rows; else None)
      payment_method     legacy WAL text column override (else channel)
      payment_ref        QBO PaymentRefNum
      memo_prefix        policy half of the payment PrivateNote
      receipt_email      where the receipt goes, or None for no receipt
      force_retry        explicit human override: re-charge past a prior
                         same-PM decline / succeeded / reconcile-review
                         (default False — the service REFUSES those alone)
      preferred_type /   alternative to payment_method_id: the service
      target_payment_method_id   resolves + row-locks the instrument itself
                         and returns no_payment_method if none is usable

    Returns {status, amount, balances, charge_id, payment_id, receipt_sent,
    receipt_error, error, attempt_id, resumed} with status one of:
    read_failed | already_paid | would_charge | uncertain | declined |
    declined_no_retry | blocked_reconcile | already_succeeded |
    no_payment_method | payment_orphan | succeeded.
    """
    stage = intent["stage"]
    anchor = intent["qbo_invoice_id"]
    line_ids = list(intent.get("lines") or [anchor])

    # Instrument: the caller may pass payment_method_id/cpm_id/channel
    # pre-resolved (maint engine), or just preferred_type — then WE resolve,
    # serialized against a concurrent disable by a FOR UPDATE lock on the pm
    # row at selection time. (# ponytail: the lock covers selection, not the
    # external call — WAL-commit-before-charge forces release; a mid-flight
    # local disable can't stop Intuit anyway, their token is already live.)
    # the service reads its own labels — callers pass ids, not paperwork
    if conn is not None and not intent.get("invoice_number"):
        row = None
        try:
            from f.billing._lib.db import query_one
            row = query_one(conn, """SELECT i.doc_number, i.customer_name,
                                        i.qbo_customer_id, i.payment_method, w.wo_number
                                 FROM billing.invoices i
                                 LEFT JOIN public.work_orders w
                                        ON w.qbo_invoice_id = i.qbo_invoice_id
                                 WHERE i.qbo_invoice_id = %s""", (anchor,))
        except Exception as e:
            print(f"  (label load warning: {e})")
        if row:
            intent = {**{"invoice_number": row["doc_number"],
                         "customer_name": row["customer_name"] or "",
                         "customer_id": row["qbo_customer_id"],
                         "payment_method": row["payment_method"],
                         "wo_number": row["wo_number"],
                         "payment_ref": row["wo_number"],
                         # the customer reads this on their receipt —
                         # "Auto-charge" told them how WE work, not what they
                         # paid for
                         "memo_prefix": f"WO# {row['wo_number']} "
                                        f"| Inv# {row['doc_number']}"}, **intent}

    if not intent.get("payment_method_id"):
        target = intent.get("target_payment_method_id")
        pm = resolve_payment_method(conn, intent.get("customer_id"),
                                    preferred_type=intent.get("preferred_type"),
                                    cpm_id=str(target) if target else None)
        if not pm.get("has_method"):
            # WAL halt row — the attempts_ok indicator surfaces this; no
            # engine stamp anywhere
            att = create_attempt(conn, anchor, stage, intent.get("invoice_number"),
                                 "card", 0, dry_run,
                                 wo_number=intent.get("wo_number"),
                                 status="no_payment_method")
            update_attempt(conn, att["id"],
                           error_message=(pm.get("error") or "no PM on file")[:300])
            return {"status": "no_payment_method", "amount": None, "balances": None,
                    "charge_id": None, "payment_id": None, "receipt_sent": False,
                    "receipt_error": None, "error": pm.get("error") or "no PM on file",
                    "attempt_id": str(att["id"]), "resumed": None}
        intent = {**intent, "payment_method_id": pm["method_id"],
                  "cpm_id": pm["cpm_id"],
                  "channel": "card" if pm["payment_type"] in ("credit_card", "card") else "ach"}

    # SECOND, INDEPENDENT CHECK, at the money moment.
    #
    # Selection (resolve_payment_method, and the DB's routing function before
    # it) filters on is_active. That is ONE signal, so when is_active was wrong
    # every check downstream of it was wrong too — a wallet refresh re-enabled
    # a card the office had turned off and both "checks" waved it through
    # (Frank Turner, MC 9815, 2026-07-27).
    #
    # So this one reads deactivated_at — the human's decision — NOT the flag
    # derived from it. Different column, different failure mode: a bug in the
    # is_active invariant cannot take out both. It also runs whatever the
    # caller passed, including a fully pre-resolved payment_method_id, and
    # re-reads inside a FOR UPDATE so a disable racing the charge is
    # serialized rather than lost.
    # (# ponytail: the lock covers selection, not the external call — a
    # disable after Intuit has the request can't be stopped from here.)
    # Keyed on EITHER id: process_maint_charges passes payment_method_id with
    # cpm_id deliberately None when it thinks the row is not live, which would
    # have skipped this guard entirely on the one path that most needs it. A
    # token we have no row for is not ours to judge and passes; a token whose
    # row says disabled does not.
    if conn is not None and (intent.get("cpm_id") or intent.get("payment_method_id")):
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            if intent.get("cpm_id"):
                cur.execute("SELECT is_active, deactivated_at IS NOT NULL AS user_off "
                            "FROM billing.customer_payment_methods "
                            "WHERE id = %s FOR UPDATE", (intent["cpm_id"],))
            else:
                cur.execute("SELECT is_active, deactivated_at IS NOT NULL AS user_off "
                            "FROM billing.customer_payment_methods "
                            "WHERE qbo_payment_method_id = %s FOR UPDATE",
                            (intent["payment_method_id"],))
            row = cur.fetchone()
            conn.commit(); cur.close()
            if row and (row["user_off"] or not row["is_active"]):
                why = ("deactivated by a user" if row["user_off"]
                       else "payment method disabled")
                att = create_attempt(conn, anchor, stage, intent.get("invoice_number"),
                                     "card", 0, dry_run,
                                     wo_number=intent.get("wo_number"),
                                     status="no_payment_method")
                update_attempt(conn, att["id"], error_message=why)
                return {"status": "no_payment_method", "amount": None, "balances": None,
                        "charge_id": None, "payment_id": None, "receipt_sent": False,
                        "receipt_error": None, "error": why,
                        "attempt_id": str(att["id"]), "resumed": None}
        except Exception as e:
            # a failure to VERIFY is not permission to charge
            print(f"  (pm guard failed: {e})")
            raise

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

    # ── prior-outcome dispatch: billing.charges IS the journal (RULED
    # 2026-08-06 — intents live on charges; the gate, both engines, and this
    # dispatch read ONE table). processing_attempts is WORKING STATE for
    # in-flight resume, fetched by the journal row's idempotency_key — never
    # consulted for an outcome the journal can answer. Legacy journal rows
    # may lack idempotency_key / customer_payment_method_id (written before
    # intents moved here): every such gap falls back CONSERVATIVELY —
    # refusing a charge is recoverable; firing one is not.
    force_retry = bool(intent.get("force_retry"))
    reuse, resumed = None, None
    prior_c = latest_charge(conn, anchor)
    if prior_c:
        cst = (prior_c.get("status") or "").lower()
        c_key = prior_c.get("idempotency_key")
        wal_row = attempt_by_key(conn, c_key) if c_key else None

        if cst in _SETTLED_CHARGE:
            if prior_c.get("qbo_payment_id"):
                if not force_retry:
                    # done is done; remainder-charging is an explicit human act
                    return res("already_succeeded",
                               attempt_id=str(wal_row["id"]) if wal_row else None,
                               charge_id=prior_c.get("charge_id"),
                               payment_id=prior_c.get("qbo_payment_id"))
            else:
                healed = _find_recorded_payment(conn, prior_c.get("charge_id"))
                if healed:  # the record landed; only its response was lost
                    _link_charge_payment(conn, prior_c.get("charge_id"), healed)
                    if wal_row:
                        update_attempt(conn, wal_row["id"], status="succeeded",
                                       qbo_payment_id=healed)
                    return res("already_succeeded",
                               attempt_id=str(wal_row["id"]) if wal_row else None,
                               charge_id=prior_c.get("charge_id"), payment_id=healed,
                               amount=float(prior_c.get("amount") or 0))
                if wal_row and wal_row["status"] == "charge_succeeded":
                    # money moved, bookkeeping interrupted — resume, never park
                    reuse, resumed = wal_row, "charge_succeeded"
                else:
                    return res("payment_orphan",
                               attempt_id=str(wal_row["id"]) if wal_row else None,
                               charge_id=prior_c.get("charge_id"),
                               amount=float(prior_c.get("amount") or 0),
                               error="journal says settled but no QBO Payment is "
                                     "recorded and no resume state exists — human "
                                     "recovery only (QBO Payment create is not "
                                     "idempotent)")

        elif cst == "declined" and not force_retry and prior_c.get("charge_id"):
            row_cpm = prior_c.get("customer_payment_method_id")
            same_pm = (row_cpm is None  # legacy row w/o instrument: block, don't guess
                       or str(row_cpm) == (str(intent["cpm_id"])
                                           if intent.get("cpm_id") else None))
            if same_pm:
                # the service itself refuses to re-hit a card the journal just
                # declined — NO caller can do this accidentally
                return res("declined_no_retry",
                           attempt_id=str(wal_row["id"]) if wal_row else None,
                           charge_id=prior_c.get("charge_id"),
                           error=prior_c.get("error_message") or "declined")

        elif cst == "uncertain":
            if (wal_row and wal_row["status"] == "needs_reconcile_review"
                    and not force_retry):
                return res("blocked_reconcile", attempt_id=str(wal_row["id"]),
                           error=wal_row.get("error_message")
                                 or "reconciler could not determine prior state")
            c_at = prior_c.get("attempted_at") or prior_c.get("updated_at")
            if c_at and c_at.tzinfo is None:
                c_at = c_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - c_at) if c_at else timedelta()
            if age > timedelta(hours=UNCERTAIN_REUSE_WINDOW_H):
                _expire_uncertain_charge(conn, prior_c)
                if wal_row and wal_row["status"] == "charge_uncertain":
                    update_attempt(conn, wal_row["id"],
                                   status="charge_uncertain_expired",
                                   error_message=(wal_row.get("error_message") or "")
                                   + " | expired after 24h by charge_and_record")
            elif wal_row and wal_row["status"] in ("charge_uncertain", "pending"):
                reuse, resumed = wal_row, "charge_uncertain"
            elif not force_retry:
                # young uncertain with no resume state (legacy row, no key):
                # the truth is genuinely unknowable right now — never re-fire
                return res("blocked_reconcile",
                           error="journal charge is uncertain and carries no "
                                 "resume key — reconciler owns it")

        elif cst == "requested" and not prior_c.get("charge_id"):
            if wal_row and wal_row["status"] == "pending":
                reuse = wal_row  # key never used; fresh-read guard still applies
        # declined-without-id / error / expired → fall through to a fresh
        # attempt (fresh key + fresh-read guard). Whether a re-attempt is
        # ALLOWED is the engine's pre-flight policy, decided before here.

    # ── amount: fresh leader read, or the persisted in-flight facts ──
    if resumed:
        amount = round(float(reuse["charge_amount"] or 0), 2)
        lines = ([(inv, float(amt)) for inv, amt in stored_group_lines(reuse) or []]
                 or [(anchor, amount)])
        balances = None
    else:
        balances = {}
        for inv_id in line_ids:
            fresh = get_qbo_invoice_details(inv_id, realm_id, access_token, conn=conn)
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
        # lines = the attempt's domain-blind membership (one entry or five —
        # same shape); group_lines in raw_result doubles it for resume compat
        update_attempt(conn, attempt["id"], lines=dumps(lines),
                       **({"raw_result": dumps({"group_lines": lines})}
                          if len(lines) > 1 else {}))
        # the journal's write-ahead: the 'requested' row the dispatch reads
        _record_charge_intent(conn, anchor, attempt, channel, amount,
                              intent.get("cpm_id"))

    def _raw(extra):
        base = {"group_lines": lines} if len(lines) > 1 else {}
        return dumps({**base, **extra})

    # charge-aggregate participants: every line invoice + customer + pm.
    # Emits sit BEFORE their update_attempt so its commit lands both together.
    _parts = ([f"invoice:{inv}" for inv, _ in lines]
              + [f"customer:{intent['customer_id']}"]
              + ([f"pm:{intent['cpm_id']}"] if intent.get("cpm_id") else []))
    _prov = {"source": "intent", "intent_ref": str(attempt["id"])}

    # ── charge (skipped when resuming past a completed charge) ──
    if attempt["status"] in ("pending", "charge_uncertain"):
        fn = charge_bank_account if channel == "ach" else charge_card
        # charge_label lets a group charge describe all its docs while the WAL
        # anchor keeps its single invoice_number
        cr = fn(intent["payment_method_id"], amount, attempt["idempotency_key"],
                intent.get("charge_label") or intent.get("invoice_number") or "",
                intent.get("customer_name") or "", access_token)
        cls = cr["classification"]
        # the attempt is recorded against the ANCHOR invoice, id or not —
        # converging the 'requested' journal row this run opened
        reflected_id = _upsert_charge(conn, cr, anchor,
                                      idempotency_key=attempt["idempotency_key"],
                                      cpm_id=intent.get("cpm_id"))
        if cls == "uncertain":
            emit(conn, "charge", attempt["id"], "charge_uncertain",
                 participants=_parts,
                 payload={"amount": amount, "charge_id": reflected_id,
                          "error": cr.get("error"), "provenance": _prov})
            update_attempt(conn, attempt["id"], status="charge_uncertain",
                           error_message=cr.get("error"), charge_id=reflected_id,
                           charge_result=dumps(cr), raw_result=_raw({"charge": cr}))
            return res("uncertain", amount=amount, balances=balances,
                       attempt_id=str(attempt["id"]), error=cr.get("error"),
                       resumed=resumed)
        if cls == "declined":
            # stamping the declined charge's id makes the engines' same-PM
            # decline gate work as documented (real declines carry an id;
            # pre-charge halts never do)
            emit(conn, "charge", attempt["id"], "charge_declined",
                 participants=_parts,
                 payload={"amount": amount, "charge_id": reflected_id,
                          "error": cr.get("error"), "provenance": _prov})
            update_attempt(conn, attempt["id"], status="charge_declined",
                           error_message=cr.get("error"), charge_id=reflected_id,
                           charge_result=dumps(cr), raw_result=_raw({"charge": cr}))
            return res("declined", amount=amount, balances=balances,
                       attempt_id=str(attempt["id"]), error=cr.get("error"),
                       resumed=resumed)
        emit(conn, "charge", attempt["id"], "charge_captured",
             participants=_parts,
             payload={"amount": amount, "charge_id": cr.get("charge_id"),
                      "channel": channel, "provenance": _prov})
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
    if not attempt.get("qbo_payment_id"):
        # newly recorded (not a resume of an already-recorded payment):
        # the Payment's birth + its applications, source: our intent
        emit(conn, "payment", rec["payment_id"], "payment_recorded",
             participants=_parts,
             payload={"amount": cr.get("amount", amount),
                      "charge_id": cr.get("charge_id"),
                      "funding": {"kind": "charge"}, "provenance": _prov})
        emit(conn, "payment", rec["payment_id"], "payment_applied",
             participants=_parts,
             payload={"funding": {"kind": "payment", "id": rec["payment_id"]},
                      "lines": [{"invoice_id": inv, "amount": amt}
                                for inv, amt in lines],
                      "provenance": _prov})
    update_attempt(conn, attempt["id"], qbo_payment_id=rec["payment_id"])
    _link_charge_payment(conn, cr.get("charge_id"), rec["payment_id"])
    if rec.get("payment"):
        # write-time verified echo: the cache shows this payment at commit
        # time, and the payment's own webhook moot-finishes via supersession
        echo_payment(conn, rec["payment"])
    insert_webhook_expectation(conn, "Payment", rec["payment_id"])

    # ── receipt: best-effort, after the money is durable, switched by DATA ──
    receipt_sent, receipt_error = False, None
    if "receipt_email" not in intent:
        try:
            intent["receipt_email"] = fetch_qbo_customer_email(
                intent["customer_id"], access_token, realm_id)
        except Exception as e:
            print(f"  (receipt email lookup warning: {e})")
            intent["receipt_email"] = None
    if intent.get("receipt_email"):
        r = send_receipt(rec["payment_id"], intent["receipt_email"],
                         access_token, realm_id)
        receipt_sent, receipt_error = r["ok"], r["error"]
        if receipt_sent:
            # The invoice MUST be a participant. receipt_sent is a payment
            # event by design (you are sending a payment receipt, not an
            # invoice), and the invoice history is participants-aware — so
            # without this the receipt is invisible on the invoice it was
            # for, while every sibling event (charge_captured,
            # payment_recorded, payment_applied) already carries it.
            emit(conn, "payment", rec["payment_id"], "receipt_sent",
                 participants=_parts,
                 payload={"email": intent["receipt_email"], "provenance": _prov})

    update_attempt(conn, attempt["id"], status="succeeded",
                   raw_result=_raw({"charge": cr,
                                    "payment": {"payment_id": rec["payment_id"]},
                                    "receipt_sent": receipt_sent,
                                    "receipt_error": receipt_error}))
    return res("succeeded", amount=amount, balances=balances,
               attempt_id=str(attempt["id"]), charge_id=cr.get("charge_id"),
               payment_id=rec["payment_id"], receipt_sent=receipt_sent,
               receipt_error=receipt_error, resumed=resumed)


def recover_orphan(conn, qbo_invoice_id, stage, customer_id, payment_ref,
                   memo_prefix, access_token, realm_id):
    """Human-verified orphan recovery: retry ONLY record_qbo_payment with the
    attempt's persisted charge — NEVER charges again. Mechanism, not policy
    (a blind record retry can double-record; the human verified in QBO/Intuit
    first). Returns {status: recovered|still_orphan|no_orphan, ...}."""
    import json
    prior = latest_attempt(conn, qbo_invoice_id, stage)
    if not prior or prior["status"] != "payment_orphan":
        return {"status": "no_orphan",
                "error": f"prior status: {prior['status'] if prior else 'none'}"}
    cr = prior.get("charge_result") or {}
    if isinstance(cr, str):
        cr = json.loads(cr)
    charge_id = prior.get("charge_id") or cr.get("charge_id")
    if not charge_id:
        return {"status": "no_orphan", "error": "no charge_id on orphan attempt"}
    cr.setdefault("charge_id", charge_id)
    healed = _find_recorded_payment(conn, charge_id)
    if healed:  # record landed earlier; response was lost — no second create
        update_attempt(conn, prior["id"], status="succeeded", qbo_payment_id=healed)
        _link_charge_payment(conn, charge_id, healed)
        return {"status": "recovered", "attempt_id": str(prior["id"]),
                "charge_id": charge_id, "payment_id": healed,
                "amount": float(prior["charge_amount"] or 0),
                "note": "healed from cache — QBO already had the payment"}
    amount = float(prior["charge_amount"] or 0)
    lines = ([(inv, float(amt)) for inv, amt in stored_group_lines(prior) or []]
             or [(qbo_invoice_id, amount)])
    rec = record_qbo_payment(customer_id, amount, cr, payment_ref, memo_prefix,
                             access_token, realm_id, lines)
    if not rec["success"]:
        update_attempt(conn, prior["id"], status="payment_orphan",
                       error_message=f"orphan recovery: record still failing: "
                                     f"{str(rec.get('error'))[:300]}")
        return {"status": "still_orphan", "attempt_id": str(prior["id"]),
                "charge_id": charge_id, "amount": amount, "error": rec.get("error")}
    emit(conn, "payment", rec["payment_id"], "payment_recorded",
         participants=[f"invoice:{inv}" for inv, _ in lines]
                      + [f"customer:{customer_id}"],
         payload={"amount": amount, "charge_id": charge_id,
                  "funding": {"kind": "charge"},
                  "provenance": {"source": "intent", "intent_ref": str(prior["id"]),
                                 "recovered_from": "payment_orphan"}})
    emit(conn, "payment", rec["payment_id"], "payment_applied",
         participants=[f"invoice:{inv}" for inv, _ in lines]
                      + [f"customer:{customer_id}"],
         payload={"funding": {"kind": "payment", "id": rec["payment_id"]},
                  "lines": [{"invoice_id": inv, "amount": amt} for inv, amt in lines],
                  "provenance": {"source": "intent", "intent_ref": str(prior["id"])}})
    if rec.get("payment"):
        echo_payment(conn, rec["payment"])
    _link_charge_payment(conn, charge_id, rec["payment_id"])
    insert_webhook_expectation(conn, "Payment", rec["payment_id"])
    update_attempt(conn, prior["id"], status="succeeded",
                   qbo_payment_id=rec["payment_id"])
    return {"status": "recovered", "attempt_id": str(prior["id"]),
            "charge_id": charge_id, "amount": amount,
            "payment_id": rec["payment_id"]}


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


def load_applicable_credits(conn, qbo_customer_id, memo_match=None,
                            memo_exclude="maint", ref_match=None,
                            max_age_months=6):
    """Unapplied credits selected by DATA, not domain knowledge: the caller
    says what it's looking for (memo_match='maint' for maintenance,
    ref_match=<wo_number> for a work order) or what to skip
    (memo_exclude='maint', the service-billing default). What to DO about
    them (halt / apply / override) is engine policy — this is just the read."""
    if not qbo_customer_id:
        return []
    sql = """
        SELECT qbo_payment_id, type, unapplied_amt, total_amt, txn_date, ref_num, memo
        FROM billing.customer_payments
        WHERE qbo_customer_id = %s
          AND unapplied_amt > 0
          AND (txn_date IS NULL OR txn_date >= (now() - (%s || ' months')::interval)::date)
    """
    params = [qbo_customer_id, str(max_age_months)]
    if memo_match:
        sql += " AND memo ~* %s"
        params.append(memo_match)
    if memo_exclude:
        sql += " AND (memo IS NULL OR memo !~* %s)"
        params.append(memo_exclude)
    if ref_match:
        sql += " AND ref_num = %s"
        params.append(ref_match)
    sql += " ORDER BY txn_date DESC NULLS LAST"
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]; cur.close()
    return rows


def apply_credits(conn, customer_id, invoice_id, access_token, realm_id,
                  credits=None, memo_match=None, memo_exclude=None,
                  ref_match=None, applied_via="auto_match", dry_run=False):
    """Apply credits to ONE invoice, each capped at the invoice's remaining
    fresh balance. Fresh-reads the balance before starting (read failure
    applies nothing) and stops at zero.

    Selection is DATA either way: pass `credits` (rows the caller's own
    policy pre-picked, e.g. a WO matcher) or selector args for
    load_applicable_credits. After each successful apply the service echoes
    OUR bookkeeping: decrement customer_payments.unapplied_amt (fires the
    credits_ok recompute trigger) + upsert billing.payment_invoice_links.

    Returns {applied: [...], failed: [...], remaining_balance, errors: [...]}.
    """
    out = {"applied": [], "failed": [], "remaining_balance": None, "errors": []}
    if credits is None:
        credits = load_applicable_credits(conn, customer_id, memo_match=memo_match,
                                          memo_exclude=memo_exclude, ref_match=ref_match)
    if not credits:
        return out
    fresh = get_qbo_invoice_details(invoice_id, realm_id, access_token, conn=conn)
    if fresh is None:
        out["errors"].append("fresh invoice read failed — no credits applied")
        return out
    remaining = fresh["balance"]
    out["remaining_balance"] = remaining
    for c in credits:
        if remaining <= 0:
            break
        amount = round(min(float(c["unapplied_amt"]), remaining), 2)
        if amount <= 0:
            continue
        entry = {"qbo_payment_id": c["qbo_payment_id"], "type": c["type"],
                 "amount": amount, "dry_run": dry_run}
        if dry_run:
            out["applied"].append(entry)
            remaining = round(remaining - amount, 2)
            continue
        r = apply_credit(c["qbo_payment_id"], c["type"], invoice_id,
                         {"value": customer_id}, amount, access_token, realm_id)
        if not r["success"]:
            out["failed"].append({**entry, "error": r["error"]})
            out["errors"].append(f"{c['qbo_payment_id']}: {r['error']}")
            continue
        # ── PAST THE POINT OF NO RETURN ──────────────────────────────────
        # The credit is now applied in QBO and that cannot be undone. Nothing
        # below may raise out of this function: an exception would abort the
        # transaction, discard every record of a real movement of money, and
        # the retry could not tell "we already applied it" from "nothing to
        # do" — it would fresh-read a zero balance and record nothing.
        #
        # 2026-07-26: a CHECK violation on payment_invoice_links.applied_via
        # did exactly that to $1,000 of Latimer credits. The failure survived
        # only in a Windmill job result, outside the system entirely.
        try:
            cur = conn.cursor()
            if r.get("payment") and not r.get("is_cm_link"):
                # WRITE-TIME VERIFIED ECHO: the response carries the payment's
                # TRUE UnappliedAmt — write what QBO said, not what we computed
                echo_payment(conn, r["payment"])
            else:
                if r.get("payment"):
                    echo_payment(conn, r["payment"])  # the new zero-total link payment
                # the CREDIT MEMO's remaining balance is a cross-entity RIPPLE
                # (the response describes the link payment, not the CM) — this
                # decrement is COMPUTED, converged by pull_qbo_credits/CDC
                cur.execute(
                    "UPDATE billing.customer_payments SET unapplied_amt = GREATEST(unapplied_amt - %s, 0) "
                    "WHERE qbo_payment_id = %s", (amount, c["qbo_payment_id"]))
            cur.execute(
                """INSERT INTO billing.payment_invoice_links
                     (payment_id, invoice_id, amount, applied_via)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (payment_id, invoice_id) DO UPDATE SET
                     amount = billing.payment_invoice_links.amount + EXCLUDED.amount""",
                (c["qbo_payment_id"], invoice_id, amount, applied_via))
            # payment_applied on the CARRIER: the credit's own Payment, or the $0
            # bridge Payment a credit-memo apply just minted (ADR 010 §B)
            is_cm = c["type"] == "credit_memo"
            carrier = ((r.get("payment") or {}).get("Id") if is_cm else None) \
                or c["qbo_payment_id"]
            emit(conn, "payment", carrier, "payment_applied",
                 participants=[f"invoice:{invoice_id}", f"customer:{customer_id}"]
                              + ([f"payment:{c['qbo_payment_id']}"] if is_cm else []),
                 payload={"funding": {"kind": "credit_memo" if is_cm else "payment",
                                      "id": c["qbo_payment_id"]},
                          "lines": [{"invoice_id": invoice_id, "amount": amount}],
                          "provenance": {"source": "intent",
                                         "intent_ref": f"apply_credits/{applied_via}"}})
            conn.commit(); cur.close()
        except Exception as book_err:
            # The money moved; our books did not. Record THAT on a clean
            # transaction — an unrecorded external write is the one outcome
            # this function must never produce.
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                emit(conn, "payment", c["qbo_payment_id"], "payment_apply_unrecorded",
                     participants=[f"invoice:{invoice_id}", f"customer:{customer_id}"],
                     payload={"amount": amount, "invoice_id": invoice_id,
                              "error": f"{type(book_err).__name__}: {str(book_err)[:400]}",
                              "note": "credit APPLIED in QBO but bookkeeping failed; "
                                      "reconcile from QBO LinkedTxn",
                              "provenance": {"source": "intent",
                                             "intent_ref": f"apply_credits/{applied_via}"}})
                conn.commit()
            except Exception as emit_err:
                print(f"  (CRITICAL: applied {amount} of {c['qbo_payment_id']} to "
                      f"{invoice_id} and could NOT record it: {book_err}; "
                      f"emit also failed: {emit_err})")
                try:
                    conn.rollback()
                except Exception:
                    pass
            out["failed"].append({**entry, "error": str(book_err)[:300],
                                  "applied_in_qbo_unrecorded": True})
            out["errors"].append(f"{c['qbo_payment_id']}: APPLIED IN QBO but not "
                                 f"recorded: {str(book_err)[:200]}")
            remaining = round(remaining - amount, 2)
            continue
        out["applied"].append(entry)
        remaining = round(remaining - amount, 2)
    out["remaining_balance"] = remaining
    return out


# ── self-check: fakes swapped into this module's namespace, NO network/DB ───

def _selfcheck():
    g = globals()
    real = {k: g[k] for k in ("latest_attempt", "create_attempt", "update_attempt",
                              "insert_webhook_expectation", "get_qbo_invoice_details",
                              "charge_card", "charge_bank_account",
                              "record_qbo_payment", "send_receipt", "apply_credit",
                              "load_applicable_credits", "echo_payment",
                              "latest_charge", "attempt_by_key",
                              "_record_charge_intent")}
    checks, calls = [], []
    def ok(name, cond):
        checks.append((name, bool(cond)))

    state = {"prior": None, "prior_charge": None, "wal_by_key": {}, "fresh": {},
             "charge": None, "record": None,
             "receipt": {"ok": True, "error": None}, "updates": []}

    def fake_latest(conn, inv, stage):
        calls.append("latest"); return state["prior"]
    def fake_latest_charge(conn, inv):
        calls.append("journal"); return state["prior_charge"]
    def fake_attempt_by_key(conn, key):
        return (state["wal_by_key"] or {}).get(key)
    def fake_intent(conn, inv, attempt, channel, amount, cpm_id):
        calls.append("intent")
    def fake_create(conn, inv, stage, invoice_number, channel, amount, dry_run, **kw):
        calls.append("create")
        return {"id": "A1", "status": "pending", "idempotency_key": "KEY-1",
                "charge_amount": amount, "qbo_payment_id": None}
    def fake_update(conn, attempt_id, **fields):
        state["updates"].append(fields)
    def fake_expect(conn, et, eid):
        calls.append(f"expect:{et}")
    def fake_fresh(inv, realm, at, conn=None):
        calls.append(f"fresh:{inv}"); return state["fresh"].get(inv)
    def fake_charge(pmid, amount, key, num, name, at):
        calls.append(f"charge:{amount}:{key}")
        r = dict(state["charge"])
        if r.get("classification") == "success":
            r["amount"] = amount  # Intuit echoes the charged amount
        return r
    def fake_record(cust, amount, cr, ref, memo, at, rid, lines):
        calls.append(f"record:{amount}:{len(lines)}")
        r = dict(state["record"])
        if r.get("success"):
            r.setdefault("payment", {"Id": r.get("payment_id"),
                                     "UnappliedAmt": 0, "TotalAmt": amount})
        return r
    def fake_echo_payment(conn, body):
        calls.append(f"echo_payment:{body.get('Id')}")
    def fake_receipt(pid, email, at, rid):
        calls.append(f"receipt:{email}"); return state["receipt"]

    g.update(latest_attempt=fake_latest, create_attempt=fake_create,
             update_attempt=fake_update, insert_webhook_expectation=fake_expect,
             get_qbo_invoice_details=fake_fresh, charge_card=fake_charge,
             charge_bank_account=fake_charge, record_qbo_payment=fake_record,
             send_receipt=fake_receipt, echo_payment=fake_echo_payment,
             latest_charge=fake_latest_charge, attempt_by_key=fake_attempt_by_key,
             _record_charge_intent=fake_intent)
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

        # 4. declined: WAL charge_declined + the declined charge's Intuit id
        #    stamped (makes the same-PM decline gate honest); no downstream
        calls.clear(); state["updates"].clear()
        state["charge"] = {"classification": "declined", "error": "card expired",
                           "raw_response": {"id": "chD", "status": "DECLINED"}}
        r = charge_and_record(None, dict(base_intent), "at", "rid")
        ok("declined -> WAL + charge id + no downstream calls",
           r["status"] == "declined" and r["error"] == "card expired"
           and state["updates"][-1]["status"] == "charge_declined"
           and state["updates"][-1].get("charge_id") == "chD"
           and not any(c.startswith(("record", "receipt")) for c in calls))
        ok("fresh attempt gets its lines stamped",
           any("lines" in u for u in state["updates"]))

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
        ok("recorded payment is echoed from the write RESPONSE",
           "echo_payment:P77" in calls)

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

        # 8. journal settled w/o payment + WAL charge_succeeded resumes
        #    WITHOUT re-charging (money moved; bookkeeping interrupted)
        calls.clear(); state["updates"].clear()
        state["record"] = {"success": True, "payment_id": "P78"}
        state["prior_charge"] = {"id": 1, "status": "succeeded", "charge_id": "chX",
                                 "qbo_payment_id": None, "amount": 30.0,
                                 "idempotency_key": "OLDKEY"}
        state["wal_by_key"] = {"OLDKEY": {
            "id": "A0", "status": "charge_succeeded", "charge_id": "chX",
            "charge_amount": 30.0, "qbo_payment_id": None, "raw_result": None,
            "attempted_at": None, "error_message": None,
            "idempotency_key": "OLDKEY"}}
        r = charge_and_record(None, dict(base_intent), "at", "rid")
        ok("charge_succeeded resume skips charge + fresh read, records payment",
           r["status"] == "succeeded" and r["resumed"] == "charge_succeeded"
           and r["charge_id"] == "chX" and r["amount"] == 30.0
           and not any(c.startswith(("charge", "fresh")) for c in calls))

        # 9. journal settled w/o payment and WITHOUT resume state REFUSES
        state["wal_by_key"] = {"OLDKEY": {"id": "A0", "status": "payment_orphan",
                                          "charge_id": "chX", "charge_amount": 30.0}}
        r = charge_and_record(None, dict(base_intent), "at", "rid")
        ok("payment_orphan refuses auto-resume", r["status"] == "payment_orphan")
        state["prior_charge"] = None; state["wal_by_key"] = {}

        # 10. multi-line: fresh-reads every member, one charge for the sum
        calls.clear(); state["prior"] = None
        state["fresh"] = {"I1": {"balance": 10.0, "email_status": None},
                          "I2": {"balance": 15.5, "email_status": None}}
        state["record"] = {"success": True, "payment_id": "P79"}
        r = charge_and_record(None, {**base_intent, "lines": ["I1", "I2"]}, "at", "rid")
        ok("group: one charge for the summed fresh balances, one 2-line payment",
           r["status"] == "succeeded" and r["amount"] == 25.5
           and "charge:25.5:KEY-1" in calls and "record:25.5:2" in calls)

        # 11. apply_credits: caps at the fresh balance, stops at zero,
        #     selection rides in as data, successful applies are echoed
        class _C:
            def __init__(self): self.sql = []
            def cursor(self, **kw): return self
            def execute(self, q, p=None): self.sql.append(" ".join(q.split()))
            def commit(self): pass
            def close(self): pass
        applied, fconn = [], _C()
        g["load_applicable_credits"] = lambda conn, cust, **sel: (
            calls.append(f"credits:{sel.get('ref_match')}"),
            [{"qbo_payment_id": "C1", "type": "payment", "unapplied_amt": 30.0},
             {"qbo_payment_id": "C2", "type": "credit_memo", "unapplied_amt": 30.0}])[1]
        g["apply_credit"] = lambda cid, ctype, inv, cref, amt, at, rid: (
            applied.append((cid, amt)),
            {"success": True, "payment": {"Id": cid, "UnappliedAmt": 30.0 - amt},
             **({"is_cm_link": True} if ctype == "credit_memo" else {})})[1]
        state["fresh"] = {"I1": {"balance": 40.0, "email_status": None}}
        r = apply_credits(fconn, "C9", "I1", "at", "rid", ref_match="WO42")
        ok("apply_credits caps at balance and stops at zero",
           applied == [("C1", 30.0), ("C2", 10.0)]
           and r["remaining_balance"] == 0 and "credits:WO42" in calls)
        ok("payment credits echo the RESPONSE; only the CM ripple is computed",
           "echo_payment:C1" in calls and "echo_payment:C2" in calls
           and sum("customer_payments SET unapplied_amt" in q for q in fconn.sql) == 1
           and sum("payment_invoice_links" in q for q in fconn.sql) == 2)

        # 13. journal settled WITH payment REFUSES without force_retry;
        #     proceeds with it (remainder-charging is an explicit human act)
        calls.clear(); state["updates"].clear()
        state["prior_charge"] = {"id": 2, "status": "succeeded", "charge_id": "chX",
                                 "qbo_payment_id": "P70", "amount": 30.0,
                                 "idempotency_key": None,
                                 "customer_payment_method_id": None}
        state["fresh"] = {"I1": {"balance": 12.0, "email_status": None}}
        state["charge"] = {"classification": "success", "charge_id": "ch10",
                           "amount": 12.0, "payment_type": "card",
                           "auth_code": "A", "card_type": "V", "card_last4": "1"}
        state["record"] = {"success": True, "payment_id": "P80"}
        r = charge_and_record(None, dict(base_intent), "at", "rid")
        ok("journal succeeded -> already_succeeded, nothing fires",
           r["status"] == "already_succeeded" and r["payment_id"] == "P70"
           and not any(c.startswith(("charge", "fresh", "record")) for c in calls))
        r = charge_and_record(None, {**base_intent, "force_retry": True}, "at", "rid")
        ok("force_retry charges the remainder past a prior success",
           r["status"] == "succeeded" and "charge:12.0:KEY-1" in calls)

        # 14. journal uncertain whose WAL row says needs_reconcile_review blocks
        state["prior_charge"] = {"id": 3, "status": "uncertain", "charge_id": None,
                                 "qbo_payment_id": None, "idempotency_key": "KU",
                                 "attempted_at": datetime.now(timezone.utc)}
        state["wal_by_key"] = {"KU": {"id": "A0", "status": "needs_reconcile_review",
                                      "error_message": "cc mismatch"}}
        r = charge_and_record(None, dict(base_intent), "at", "rid")
        ok("reconcile-review blocks", r["status"] == "blocked_reconcile"
           and r["error"] == "cc mismatch")
        state["wal_by_key"] = {}

        # 15. same-instrument real decline refuses; a LEGACY row without an
        #     instrument id blocks conservatively (refusing is recoverable,
        #     firing is not — Simmons 68300); a different instrument retries
        calls.clear()
        state["prior_charge"] = {"id": 4, "status": "declined", "charge_id": "chD",
                                 "qbo_payment_id": None, "idempotency_key": None,
                                 "customer_payment_method_id": "CPM1",
                                 "error_message": "card expired"}
        r = charge_and_record(None, {**base_intent, "cpm_id": "CPM1"}, "at", "rid")
        ok("same-instrument decline -> declined_no_retry, card untouched",
           r["status"] == "declined_no_retry" and r["charge_id"] == "chD"
           and not any(c.startswith("charge") for c in calls))
        state["prior_charge"]["customer_payment_method_id"] = None
        r = charge_and_record(None, {**base_intent, "cpm_id": "OTHER"}, "at", "rid")
        ok("legacy decline row w/o instrument blocks conservatively",
           r["status"] == "declined_no_retry")
        state["prior_charge"]["customer_payment_method_id"] = "CPM1"
        r = charge_and_record(None, {**base_intent, "cpm_id": "OTHER"}, "at", "rid")
        ok("different instrument retries freely", r["status"] == "succeeded")
        state["prior_charge"] = None

        # 16. recover_orphan: records with the persisted charge, never re-charges
        calls.clear(); state["updates"].clear()
        state["prior"] = {"id": "A0", "status": "payment_orphan", "charge_id": "chX",
                          "charge_amount": 30.0, "charge_result": None, "raw_result": None}
        state["record"] = {"success": True, "payment_id": "P81"}
        r = recover_orphan(None, "I1", "maint", "C1", "1042", "memo", "at", "rid")
        ok("orphan recovery records + succeeds without charging",
           r["status"] == "recovered" and r["payment_id"] == "P81"
           and not any(c.startswith("charge") for c in calls)
           and any(u.get("status") == "succeeded" for u in state["updates"]))
        state["prior"] = None

        # 17. service-side instrument resolution: none usable -> no_payment_method;
        #     usable -> resolved + charged (lock warning tolerated on fake conn)
        state["prior"] = None
        g["resolve_payment_method"] = lambda conn, cust, **kw: {"has_method": False,
                                                                "error": "no card"}
        calls.clear()
        r = charge_and_record(None, {k: v for k, v in base_intent.items()
                                     if k not in ("payment_method_id", "channel")}
                              | {"preferred_type": "credit_card"}, "at", "rid")
        ok("unresolvable instrument -> no_payment_method + WAL halt row",
           r["status"] == "no_payment_method" and r["error"] == "no card"
           and "create" in calls and r["attempt_id"] == "A1")
        g["resolve_payment_method"] = lambda conn, cust, **kw: {
            "has_method": True, "method_id": "pmX", "cpm_id": "CPMX",
            "payment_type": "credit_card"}
        state["fresh"] = {"I1": {"balance": 9.0, "email_status": None}}
        state["charge"] = {"classification": "success", "charge_id": "ch11",
                           "amount": 9.0, "payment_type": "card",
                           "auth_code": "A", "card_type": "V", "card_last4": "2"}
        state["record"] = {"success": True, "payment_id": "P82"}
        r = charge_and_record(None, {k: v for k, v in base_intent.items()
                                     if k not in ("payment_method_id", "channel")}
                              | {"preferred_type": "credit_card"}, "at", "rid")
        ok("service resolves + charges", r["status"] == "succeeded"
           and "charge:9.0:KEY-1" in calls)

        # 18. orphan with a cache-matched payment SELF-HEALS (leg-2 dedupe);
        #     unproven orphan still refuses
        class _LookupConn:
            # `q` decides the shape: the pm guard reads through a
            # RealDictCursor and wants a dict; the orphan lookup wants the
            # tuple it was constructed with
            def __init__(self, row): self._row = row; self._pm = False
            def cursor(self, cursor_factory=None): return self
            def execute(self, q, p=None):
                self._pm = "customer_payment_methods" in q
            def fetchone(self):
                return ({"is_active": True, "user_off": False} if self._pm
                        else self._row)
            def commit(self): pass
            def close(self): pass
        calls.clear(); state["updates"].clear()
        state["prior_charge"] = {"id": 5, "status": "succeeded", "charge_id": "chX",
                                 "qbo_payment_id": None, "amount": 30.0,
                                 "idempotency_key": "KO"}
        state["wal_by_key"] = {"KO": {"id": "A0", "status": "payment_orphan",
                                      "charge_id": "chX", "charge_amount": 30.0}}
        r = charge_and_record(_LookupConn(("P90",)), dict(base_intent), "at", "rid")
        ok("orphan heals when the record provably landed",
           r["status"] == "already_succeeded" and r["payment_id"] == "P90"
           and any(u.get("qbo_payment_id") == "P90" for u in state["updates"])
           and not any(c.startswith(("charge", "record")) for c in calls))
        r = charge_and_record(_LookupConn(None), dict(base_intent), "at", "rid")
        ok("unproven orphan still refuses", r["status"] == "payment_orphan")
        state["prior_charge"] = None; state["wal_by_key"] = {}

        # 12. apply_credits halts on a failed fresh read; caller-picked
        #     credits list bypasses the selector load
        state["fresh"] = {"I1": None}
        r = apply_credits(_C(), "C9", "I1", "at", "rid",
                          credits=[{"qbo_payment_id": "C1", "type": "payment",
                                    "unapplied_amt": 5.0}])
        ok("apply_credits refuses on read failure",
           r["applied"] == [] and r["errors"])
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
