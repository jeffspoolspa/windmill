# requirements:
# psycopg2-binary
# wmill

"""
f/billing/_lib/db — the one Supabase connection helper.

ADR 009 (tier-1 primitive): one implementation per external operation. This
replaces the get_db_conn() boilerplate copy-pasted into 24 billing /
service_billing scripts, each identical, each pointing at the same resource.
Extracted VERBATIM so behavior is unchanged.

Import as:  from f.billing._lib.db import get_db_conn, query_one, query_all, execute_sql

Shared across billing AND service_billing (cross-area import works — see
f/billing/_lib/qbo). Port 6543 is the Supabase transaction pooler.
"""

import json
import uuid
from datetime import date, datetime
from decimal import Decimal

import psycopg2
import wmill

SUPABASE_RESOURCE = "u/carter/supabase"


def get_db_conn():
    sb = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(
        host=sb["host"], port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"), user=sb["user"],
        password=sb["password"], sslmode=sb.get("sslmode", "require"),
    )


# ── the three cursor one-liners every engine was redefining locally ──────────

import psycopg2.extras


def query_one(conn, sql, params=()):
    """One row as a dict, or None."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None


def query_all(conn, sql, params=()):
    """All rows as dicts."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]; cur.close()
    return rows


def execute(conn, sql, params=()):
    """Execute WITHOUT committing — the caller owns the transaction boundary.

    This is the default for anything that must be atomic with something else
    (a fact and its event, a write and its echo). Commit once, at the end of
    the unit of work."""
    cur = conn.cursor()
    cur.execute(sql, params)
    cur.close()


def execute_sql(conn, sql, params=()):
    """Execute + commit — a self-contained write with nothing to be atomic
    with. Prefer execute() inside a multi-write unit."""
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit(); cur.close()


def dumps(obj):
    """THE JSON encoder for DB payloads (Decimal / date / datetime / UUID).
    Canonical home — 22 copies of this existed across the repo."""
    return json.dumps(obj, default=_json_default)


def _json_default(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, uuid.UUID):
        return str(o)
    raise TypeError(f"not JSON serializable: {type(o).__name__}")
