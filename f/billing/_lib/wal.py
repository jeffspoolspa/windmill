# requirements:
# psycopg2-binary

"""
f/billing/_lib/wal — the shared write-ahead log over billing.processing_attempts.

ADR 009 tier 1: each function is one DB side effect, no policy. The two
engines' hand-synced WAL twins (stage='process' in process_invoice,
stage='maint' in process_maint_period) unify here with stage as DATA.

The contract that makes retries safe (unchanged from both engines):
  - create_attempt generates the idempotency_key ONCE and COMMITS the row
    BEFORE any external call. Intuit dedupes on Request-Id, so a crash-and-
    resume that reuses the persisted key cannot double-charge.
  - latest_attempt ignores dry-run rows — sandbox plans never affect
    retry/resume decisions.

Import as:  from f.billing._lib.wal import create_attempt, update_attempt, ...
"""

import json
import uuid
from datetime import datetime, date
from decimal import Decimal

import psycopg2.extras

from f.billing._lib.events import emit


def json_default(o):
    """json.dumps default for DB-row types (Decimal / date / datetime / UUID)."""
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, uuid.UUID):
        return str(o)
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def dumps(obj):
    """json.dumps that handles billing.* row values. Use for every raw_result /
    charge_result WAL write."""
    return json.dumps(obj, default=json_default)


def latest_attempt(conn, qbo_invoice_id, stage):
    """Most recent NON-dry-run CHARGE attempt for this invoice at this stage.

    channel='email' rows are excluded: they are sends, not charges. Until
    delivery.send_and_record stopped calling create_attempt, a send booked a
    row in this CHARGE write-ahead log — same invoice, same stage,
    status='succeeded', charge_id NULL. charge_and_record reads this function
    to decide "have we already done this?", so such a row answered yes for a
    charge that never happened, and the invoice could never be charged again
    without force_retry. 729 of them exist (Apr-Jul 2026, every one with
    charge_id NULL); WO 5039608 / invoice 68300 is how it surfaced.

    The send side is already fixed, so this stops no NEW rows — it stops the
    existing ones from being read as charges. Excluding by channel rather than
    deleting the rows keeps the delivery history intact.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT * FROM billing.processing_attempts
           WHERE qbo_invoice_id = %s AND stage = %s AND dry_run = false
             AND channel IS DISTINCT FROM 'email'
           ORDER BY attempted_at DESC LIMIT 1""",
        (qbo_invoice_id, stage))
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None


def create_attempt(conn, qbo_invoice_id, stage, invoice_number, channel,
                   charge_amount, dry_run, cpm_id=None, wo_number=None,
                   payment_method=None, status="pending"):
    """WRITE-AHEAD: insert + COMMIT the attempt with a fresh idempotency_key
    BEFORE any external call.

    channel 'card' normalizes to the schema's 'credit_card'. payment_method is
    the legacy dual-written text column; defaults to the normalized channel
    (maint behavior) unless the caller passes its own legacy value (process
    engine rollout compat). status override exists for grouped-charge SIBLING
    rows written after the outcome is known (never 'pending' — their keys are
    never charged; the anchor row carries the real charge).
    """
    db_channel = "credit_card" if channel == "card" else channel
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """INSERT INTO billing.processing_attempts
             (wo_number, invoice_number, qbo_invoice_id, stage, status,
              idempotency_key, payment_method, charge_amount, dry_run,
              channel, customer_payment_method_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING *""",
        (wo_number, invoice_number, qbo_invoice_id, stage, status,
         str(uuid.uuid4()), payment_method or db_channel, charge_amount,
         dry_run, db_channel, cpm_id))
    row = cur.fetchone()
    # charge_attempted (ADR 010): the charge aggregate's birth event, in the
    # SAME write-ahead transaction as the WAL row. Only for real intents —
    # dry runs made no side effect, and sibling stubs (status != 'pending')
    # are bookkeeping whose keys are never charged.
    if not dry_run and status == "pending":
        emit(conn, "charge", row["id"], "charge_attempted",
             participants=[f"invoice:{qbo_invoice_id}"]
                          + ([f"pm:{cpm_id}"] if cpm_id else []),
             payload={"stage": stage, "amount": charge_amount,
                      "channel": db_channel, "invoice_number": invoice_number,
                      "wo_number": wo_number,
                      "provenance": {"source": "intent",
                                     "intent_ref": str(row["id"])}})
    conn.commit()
    cur.close()
    return dict(row)


def update_attempt(conn, attempt_id, **fields):
    """Set columns on one attempt row + commit. Field names are code-authored
    column names, never user input."""
    if not fields:
        return
    sets = ", ".join(f"{k} = %s" for k in fields)
    cur = conn.cursor()
    cur.execute(f"UPDATE billing.processing_attempts SET {sets} WHERE id = %s",
                list(fields.values()) + [attempt_id])
    conn.commit(); cur.close()


# How long we expect QBO webhooks to arrive after we make a write. If they
# don't show up within this window, cdc_reconciler flips the expectation to
# 'missing' and it surfaces in the UI for human investigation.
WEBHOOK_GRACE_MINUTES = 5


def insert_webhook_expectation(conn, entity_type, entity_id):
    """Record that QBO should send a webhook for this entity within the grace
    window. Best-effort only — failure never fails the write it verifies.

    Race-handling: QBO regularly fires webhooks <300ms after our write —
    faster than we can INSERT this row. We check billing.webhook_log for a
    matching event in the last 30 seconds; if present, insert as 'confirmed'
    directly with webhook_received_at backfilled from the log.
    """
    if not entity_id:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO billing.webhook_expectations (
                entity_type, entity_id, expected_by, source, status, webhook_received_at
            )
            SELECT
                %s, %s,
                now() + (%s || ' minutes')::interval,
                'self_initiated',
                CASE WHEN recent_wh.received_at IS NOT NULL THEN 'confirmed' ELSE 'pending' END,
                recent_wh.received_at
            FROM (
                SELECT MAX(received_at) AS received_at
                  FROM billing.webhook_log
                 WHERE entity_type = %s
                   AND entity_id = %s
                   AND received_at > now() - interval '30 seconds'
            ) AS recent_wh
            RETURNING status
        """, (entity_type, entity_id, str(WEBHOOK_GRACE_MINUTES),
              entity_type, entity_id))
        row = cur.fetchone()
        if row and row[0] == 'confirmed':
            print(f"  webhook_expectation pre-confirmed [{entity_type}:{entity_id}] "
                  f"(webhook beat insert; race resolved inline)")
        conn.commit(); cur.close()
    except Exception as e:
        print(f"  (webhook_expectation insert warning [{entity_type}:{entity_id}]: {e})")


# ── self-check: fake conn, NO database ───────────────────────────────────────

class _FakeCursor:
    def __init__(self, row=None):
        self._row = row
        self.executed = []
    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
    def fetchone(self):
        return self._row
    def close(self):
        pass


class _FakeConn:
    def __init__(self, row=None):
        self.cur = _FakeCursor(row)
        self.commits = 0
    def cursor(self, **kw):
        return self.cur
    def commit(self):
        self.commits += 1


def _selfcheck():
    checks = []
    def ok(name, cond):
        checks.append((name, bool(cond)))

    conn = _FakeConn(row={"id": "a1", "status": "pending", "idempotency_key": "k"})
    row = create_attempt(conn, "inv9", "maint", "1042", "card", 50.0, False, cpm_id="c1")
    sql, params = conn.cur.executed[0]
    ok("create is an INSERT..RETURNING", "INSERT INTO billing.processing_attempts" in sql
       and "RETURNING *" in sql)
    ok("create commits (write-ahead)", conn.commits == 1)
    ok("card normalizes to credit_card", params[9] == "credit_card" and params[6] == "credit_card")
    ok("stage + status are data", params[3] == "maint" and params[4] == "pending")
    ok("idempotency key is a fresh uuid", len(params[5]) == 36)
    ok("returns the row", row["id"] == "a1")

    # a send is not a charge: the lookup that answers "already charged?" must
    # not read channel='email' rows (the 729 legacy sends booked in this WAL)
    conn_la = _FakeConn(row={"id": "a2", "status": "succeeded"})
    latest_attempt(conn_la, "inv9", "process")
    sql_la, _ = conn_la.cur.executed[0]
    ok("latest_attempt excludes sends", "channel IS DISTINCT FROM 'email'" in sql_la)
    ok("latest_attempt still ignores dry runs", "dry_run = false" in sql_la)

    conn2 = _FakeConn()
    update_attempt(conn2, "a1", status="charge_succeeded", charge_id="ch_1")
    sql2, params2 = conn2.cur.executed[0]
    ok("update sets exactly the passed fields",
       sql2 == "UPDATE billing.processing_attempts SET status = %s, charge_id = %s WHERE id = %s"
       and params2 == ["charge_succeeded", "ch_1", "a1"])
    update_attempt(conn2, "a1")  # no fields → no-op
    ok("empty update is a no-op", len(conn2.cur.executed) == 1)

    conn3 = _FakeConn()
    insert_webhook_expectation(conn3, "Payment", None)
    ok("null entity skips expectation insert", conn3.cur.executed == [])
    ok("dumps handles Decimal/date/uuid",
       dumps({"d": Decimal("1.5"), "t": date(2026, 7, 10),
              "u": uuid.UUID("00000000-0000-0000-0000-000000000001")})
       == '{"d": 1.5, "t": "2026-07-10", "u": "00000000-0000-0000-0000-000000000001"}')

    failed = [n for n, p in checks if not p]
    return {"passed": len(checks) - len(failed), "total": len(checks), "failed": failed}


def main():
    """No-DB self-check (invocable as a Windmill job to verify the extraction)."""
    result = _selfcheck()
    result["ok"] = not result["failed"]
    return result


if __name__ == "__main__":
    print(main())
