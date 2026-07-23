# requirements:
# psycopg2-binary

"""
f/billing/_lib/events — append_event/emit, the single writer to billing.events.

ADR 010 tier 1: one primitive = one INSERT, no policy. billing.events is the
append-only billing domain event stream (immutability trigger-enforced in the
DB); this module is the only code path that writes it. Event names, aggregates,
and the what-is-an-event rules live in docs/conventions/EVENT_VOCABULARY.md —
check the registry before emitting a new type.

Two entry points:
  - append_event: strict — raises on bad input, no commit (the fact joins the
    caller's open transaction so it commits atomically with the state write it
    records). Use for backfills and anywhere failure should halt.
  - emit: best-effort wrapper for the MONEY PATH — same insert, but a failure
    prints a warning and returns None instead of raising, so telemetry can
    never kill a charge mid-flight (the _upsert_charge reflection pattern).

Self-contained on purpose (own JSON encoder, no _lib imports): wal.py imports
this module, so importing wal back would be a cycle.
"""

import json
import uuid as _uuid
from datetime import datetime, date

AGGREGATES = {"invoice", "payment", "charge", "customer", "work_order"}
_FIXED_ACTORS = {"auto", "qbo_webhook", "reconciler", "system"}


def _json_default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, _uuid.UUID):
        return str(o)
    try:  # Decimal and friends
        return float(o)
    except Exception:
        raise TypeError(f"not JSON serializable: {type(o).__name__}")


def _valid_actor(actor):
    """Fixed set, or a user email (the '@' is the discriminator)."""
    return actor in _FIXED_ACTORS or "@" in actor


def append_event(conn, aggregate, aggregate_id, type, payload=None,
                 actor="auto", participants=None, occurred_at=None):
    """INSERT one immutable fact; returns the assigned seq. NO commit — the
    fact rides the caller's transaction.

    - aggregate must be a registered aggregate (EVENT_VOCABULARY.md section).
    - participants: list like ["invoice:194", "pm:<uuid>"] — every entity the
      fact names (money-path events always include their customer:<id>). The
      home aggregate is implicit and need not be repeated.
    - payload: dict; provenance ({source, intent_ref | discovered_via, ...})
      goes here.
    - occurred_at: pass QBO MetaData time for observed facts when known;
      default now() (DB clock) otherwise.
    """
    if aggregate not in AGGREGATES:
        raise ValueError(f"unknown aggregate {aggregate!r} — register it in "
                         "EVENT_VOCABULARY.md (and the DB check) first")
    if not aggregate_id:
        raise ValueError("aggregate_id is required")
    if not _valid_actor(actor):
        raise ValueError(f"actor {actor!r} not in {sorted(_FIXED_ACTORS)} and "
                         "not a user email")
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO billing.events
             (aggregate, aggregate_id, type, actor, participants, payload,
              occurred_at)
           VALUES (%s, %s, %s, %s, %s, %s::jsonb, COALESCE(%s, now()))
           RETURNING seq""",
        (aggregate, str(aggregate_id), type, actor,
         [str(p) for p in (participants or []) if p],
         json.dumps(payload or {}, default=_json_default), occurred_at))
    row = cur.fetchone()
    cur.close()
    return row[0]


def emit(conn, aggregate, aggregate_id, type, **kw):
    """Best-effort append_event for the money path: warn-and-continue on any
    failure. Returns the seq or None."""
    try:
        return append_event(conn, aggregate, aggregate_id, type, **kw)
    except Exception as e:
        print(f"  (event emit warning [{aggregate}:{aggregate_id} {type}]: {e})")
        return None


# ── self-check: fake conn, NO database ───────────────────────────────────────

class _FakeCursor:
    def __init__(self, row=(41,)):
        self._row = row
        self.executed = []
    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
    def fetchone(self):
        return self._row
    def close(self):
        pass


class _FakeConn:
    def __init__(self, row=(41,)):
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

    conn = _FakeConn()
    seq = append_event(conn, "payment", "88", "payment_applied",
                       payload={"lines": [{"invoice_id": "194", "amount": 50.0}],
                                "provenance": {"source": "external",
                                               "discovered_via": "webhook"}},
                       actor="qbo_webhook",
                       participants=["invoice:194", "customer:77"])
    sql, params = conn.cur.executed[0]
    ok("insert targets billing.events", "INSERT INTO billing.events" in sql)
    ok("returns seq", seq == 41)
    ok("NO commit inside (joins caller tx)", conn.commits == 0)
    ok("participants pass as strings", params[4] == ["invoice:194", "customer:77"])
    ok("payload JSON-encoded", '"invoice_id": "194"' in params[5])
    ok("occurred_at defaults via COALESCE(now())",
       "COALESCE(%s, now())" in sql and params[6] is None)

    def raises(fn):
        try:
            fn(); return False
        except ValueError:
            return True
    ok("unknown aggregate rejected",
       raises(lambda: append_event(conn, "lead", "1", "x")))
    ok("empty aggregate_id rejected",
       raises(lambda: append_event(conn, "invoice", "", "x")))
    ok("bad actor rejected",
       raises(lambda: append_event(conn, "invoice", "1", "x", actor="hacker")))
    ok("user-email actor accepted", _valid_actor("carter@jeffspoolspa.com"))

    # emit(): best-effort — a broken conn warns, returns None, never raises
    class _Boom:
        def cursor(self, **kw):
            raise RuntimeError("db down")
    ok("emit swallows failure", emit(_Boom(), "invoice", "1", "invoice_edited") is None)
    ok("emit passes through on success",
       emit(_FakeConn(), "charge", "a1", "charge_captured") == 41)
    ok("encoder handles date/uuid",
       json.dumps({"t": date(2026, 7, 23),
                   "u": _uuid.UUID("00000000-0000-0000-0000-000000000001")},
                  default=_json_default)
       == '{"t": "2026-07-23", "u": "00000000-0000-0000-0000-000000000001"}')

    failed = [n for n, p in checks if not p]
    return {"passed": len(checks) - len(failed), "total": len(checks),
            "failed": failed}


def main():
    """No-DB self-check (invocable as a Windmill job to verify the module)."""
    result = _selfcheck()
    result["ok"] = not result["failed"]
    return result


if __name__ == "__main__":
    print(main())
