# CDC-based reconciler for QBO ↔ cache drift detection.
#
# Architecture (Pattern D, see CLAUDE.md):
#   - Webhooks are the primary low-latency channel for external changes
#   - This reconciler is the truth backstop — catches anything webhooks dropped
#   - Uses QBO's CDC endpoint (incremental) so we only check what actually
#     changed since our last cursor, not the whole table.
#
# CACHE WRITES — single source of truth:
# The CDC response includes the FULL entity body for each changed record.
# Rather than firing async refresh_* scripts (which would re-fetch the same
# record from QBO), we import refresh_invoice / refresh_payment / refresh_customer
# directly and call their main() with qbo_body=<cdc_entity>. The refresh
# scripts skip the QBO GET when given a body, then run their full upsert
# + side-effect logic (memo-edit detection, WO link, CCTransId verification,
# display_name propagation, etc.) under one shared schema map. Concurrency
# is handled by an OCC guard inside each upsert: WHERE existing.qbo_last_updated_*
# < EXCLUDED — so simultaneous writers can't clobber each other.
#
# Field-level diffs: we compute a per-field {before, after} dict using the
# cache row + CDC body and store it on drift_log.field_diff. Lets us answer
# "what specifically changed in QBO that we hadn't mirrored yet?" without
# replaying the entity.
#
# What runs every 15 minutes (Windmill cron):
#   1. Read last cursor from billing.cdc_cursors WHERE source='qbo'
#   2. Call QBO /cdc?entities=Invoice,Payment,Customer&changedSince=<cursor>
#   3. For each returned entity (sorted by qbo_updated ascending so the cursor
#      can advance incrementally):
#        - If our cache is older than QBO's MetaData.LastUpdatedTime → drift
#        - Compute per-field diff (cache value → QBO value) for the canonical set
#        - Call refresh_*.main(qbo_body=...) inline to upsert + side effects
#        - Critical drift (cache_ahead) flagged for human review
#        - Per-entity try/except: a bad row is logged + skipped, never fatal
#        - Cursor advances after every successful entity, so a mid-loop failure
#          only loses the in-flight ones, not the whole 15-min window
#   4. Sweep stale cache_ahead drift entries whose invoices have caught up.
#   5. Flag webhook expectations whose grace window has expired.
#   6. Prune auto_healed drift_log rows older than 30 days.
#
# Severity tiers:
#   soft     — cache stale relative to QBO (most common; auto-heal silently)
#   hard     — webhook missing AND value disagrees, or per-entity processing
#              error (auto-heal where possible, flag in drift_log)
#   critical — cache appears NEWER than QBO (rare; halt + alert)
#
# Identifier handling: table names use psycopg2.sql.Identifier so quoted/
# PascalCase names like public."Customers" work alongside lowercase ones.

import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import psycopg2
import psycopg2.extras
from psycopg2 import sql as psql
import requests
import wmill

# Refresh scripts imported in-process so we don't pay for an async script
# dispatch per drifted entity (1500+ entities/run otherwise = 1500+ jobs).
# These also run the upsert+side-effect pipeline that the webhook handler
# relies on, keeping a single source of truth for the schema mapping.
import f.service_billing.refresh_invoice as refresh_invoice
import f.service_billing.refresh_payment as refresh_payment
import f.service_billing.refresh_customer as refresh_customer

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"

ENTITIES_TO_RECONCILE = ["Invoice", "Payment", "Customer"]
DRIFT_LOG_RETENTION_DAYS = 30

# (schema, table, id_col). Table names are CASE-SENSITIVE — Customers in
# public is created as the quoted identifier "Customers".
ENTITY_TO_TABLE = {
    "Invoice":  ("billing", "invoices",          "qbo_invoice_id"),
    "Payment":  ("billing", "customer_payments", "qbo_payment_id"),
    "Customer": ("public",  "Customers",         "qbo_customer_id"),
}

# Maps entity_type → (refresh_module, id_kwarg_name). Each refresh module
# exposes a main() that accepts the id and an optional qbo_body=.
INLINE_REFRESH_BY_ENTITY = {
    "Invoice":  (refresh_invoice,  "qbo_invoice_id"),
    "Payment":  (refresh_payment,  "qbo_payment_id"),
    "Customer": (refresh_customer, "qbo_customer_id"),
}


# ---------------------------------------------------------------------------
# Field diff schemas
# ---------------------------------------------------------------------------

def _ref_value(d, key):
    if not isinstance(d, dict):
        return None
    inner = d.get(key)
    if isinstance(inner, dict):
        return inner.get("value")
    return None


def _bill_addr(qbo, field):
    return ((qbo.get("BillAddr") or {}).get(field)) if qbo.get("BillAddr") else None


INVOICE_DIFF_FIELDS = {
    "doc_number":      lambda q: q.get("DocNumber"),
    "qbo_customer_id": lambda q: _ref_value(q, "CustomerRef"),
    "customer_name":   lambda q: (q.get("CustomerRef") or {}).get("name"),
    "txn_date":        lambda q: q.get("TxnDate"),
    "due_date":        lambda q: q.get("DueDate"),
    "total_amt":       lambda q: q.get("TotalAmt"),
    "balance":         lambda q: q.get("Balance"),
    "email_status":    lambda q: q.get("EmailStatus"),
}

PAYMENT_DIFF_FIELDS = {
    "qbo_customer_id": lambda q: _ref_value(q, "CustomerRef"),
    "type":            lambda q: (q.get("PaymentMethodRef") or {}).get("name"),
    "total_amt":       lambda q: q.get("TotalAmt"),
    "unapplied_amt":   lambda q: q.get("UnappliedAmt"),
    "txn_date":        lambda q: q.get("TxnDate"),
    "ref_num":         lambda q: q.get("PaymentRefNum"),
}

CUSTOMER_DIFF_FIELDS = {
    "display_name": lambda q: q.get("DisplayName"),
    "first_name":   lambda q: q.get("GivenName"),
    "last_name":    lambda q: q.get("FamilyName"),
    "company":      lambda q: q.get("CompanyName"),
    "email":        lambda q: (q.get("PrimaryEmailAddr") or {}).get("Address"),
    "phone":        lambda q: (q.get("PrimaryPhone") or {}).get("FreeFormNumber"),
    "city":         lambda q: _bill_addr(q, "City"),
    "state":        lambda q: _bill_addr(q, "CountrySubDivisionCode"),
    "zip":          lambda q: _bill_addr(q, "PostalCode"),
    "balance":      lambda q: q.get("Balance"),
    "is_active":    lambda q: q.get("Active"),
}

DIFF_FIELDS_BY_ENTITY = {
    "Invoice":  INVOICE_DIFF_FIELDS,
    "Payment":  PAYMENT_DIFF_FIELDS,
    "Customer": CUSTOMER_DIFF_FIELDS,
}


def _normalize_for_compare(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    if isinstance(v, str):
        s = v.strip()
        if s and s.lstrip("-").replace(".", "", 1).isdigit():
            try:
                return float(s)
            except ValueError:
                pass
        return s if s != "" else None
    if isinstance(v, bool):
        return v
    return v


def _values_equal(a, b):
    na, nb = _normalize_for_compare(a), _normalize_for_compare(b)
    if isinstance(na, float) and isinstance(nb, float):
        return abs(na - nb) < 0.005
    return na == nb


def _serialize_for_json(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, UUID):
        return str(v)
    return str(v)


def compute_field_diff(entity_type, cached_row, qbo_entity):
    """{field: {"before": cache, "after": qbo}} for every disagreement."""
    fields = DIFF_FIELDS_BY_ENTITY.get(entity_type, {})
    diff = {}
    for col, extract in fields.items():
        cache_val = (cached_row or {}).get(col)
        qbo_val = extract(qbo_entity)
        if not _values_equal(cache_val, qbo_val):
            diff[col] = {
                "before": _serialize_for_json(cache_val),
                "after":  _serialize_for_json(qbo_val),
            }
    return diff


# ---------------------------------------------------------------------------
# QBO + DB helpers
# ---------------------------------------------------------------------------

def refresh_qbo_token():
    resource = wmill.get_resource(QBO_RESOURCE)
    resp = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"]),
        timeout=30,
    )
    if not resp.ok:
        raise Exception(f"QBO token refresh failed: {resp.status_code} - {resp.text}")
    tokens = resp.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(QBO_RESOURCE, resource)
    return tokens["access_token"], resource["realm_id"]


def get_db_conn():
    sb = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(
        host=sb["host"], port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"), user=sb["user"],
        password=sb["password"], sslmode=sb.get("sslmode", "require"),
    )


def qbo_cdc(access_token, realm_id, entities, changed_since):
    url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/cdc"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    params = {
        "entities": ",".join(entities),
        "changedSince": changed_since.isoformat().replace("+00:00", "Z"),
    }
    resp = requests.get(url, headers=headers, params=params, timeout=60)
    if not resp.ok:
        raise Exception(f"QBO CDC failed: {resp.status_code} - {resp.text[:300]}")

    body = resp.json()
    result = {}
    for item in body.get("CDCResponse", []):
        for query_response in item.get("QueryResponse", []):
            for ent_type in entities:
                if ent_type in query_response:
                    result.setdefault(ent_type, []).extend(query_response[ent_type])
    return result


def get_cursor(conn, source="qbo"):
    cur = conn.cursor()
    cur.execute(
        "SELECT cursor_timestamp FROM billing.cdc_cursors WHERE source = %s",
        (source,),
    )
    row = cur.fetchone()
    cur.close()
    if not row:
        cur = conn.cursor()
        initial = datetime.now(timezone.utc) - timedelta(hours=1)
        cur.execute(
            "INSERT INTO billing.cdc_cursors (source, cursor_timestamp) VALUES (%s, %s)",
            (source, initial),
        )
        conn.commit()
        cur.close()
        return initial
    return row[0]


def save_cursor(conn, new_cursor, status, entities_processed, drift_count, notes=None):
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE billing.cdc_cursors
        SET cursor_timestamp = %s,
            last_run_at = now(),
            last_run_status = %s,
            entities_processed = %s,
            drift_detected_count = %s,
            notes = %s
        WHERE source = 'qbo'
        """,
        (new_cursor, status, entities_processed, drift_count, notes),
    )
    conn.commit()
    cur.close()


def parse_qbo_timestamp(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_cached(conn, schema, table, id_col, entity_id):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = psql.SQL("SELECT * FROM {schema}.{table} WHERE {id_col} = %s").format(
        schema=psql.Identifier(schema),
        table=psql.Identifier(table),
        id_col=psql.Identifier(id_col),
    )
    cur.execute(query, (entity_id,))
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None


def log_drift(conn, entity_type, entity_id, kind, severity,
              cache_state, qbo_state, resolution, field_diff=None):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO billing.drift_log
          (entity_type, entity_id, kind, severity,
           cache_state, qbo_state, field_diff,
           resolution, resolution_at)
        VALUES (%s, %s, %s, %s,
                %s::jsonb, %s::jsonb, %s::jsonb,
                %s, CASE WHEN %s = 'auto_healed' THEN now() ELSE NULL END)
        """,
        (
            entity_type, entity_id, kind, severity,
            psycopg2.extras.Json(cache_state) if cache_state else None,
            psycopg2.extras.Json(qbo_state) if qbo_state else None,
            psycopg2.extras.Json(field_diff) if field_diff else None,
            resolution, resolution,
        ),
    )
    conn.commit()
    cur.close()


def trigger_inline_refresh(entity_type, entity_id, qbo_body):
    """Run the refresh_* main() in-process with the body in hand.

    No QBO GET, no async script dispatch, no extra DB connection from a
    new job — just calls the refresh module directly. The refresh modules
    open their own conns (they do their own commit lifecycle), which is
    fine: they're idempotent and the OCC guard prevents clobbering.

    Errors are caught + logged; per-entity errors are surfaced via the
    caller's processing_errors list.
    """
    refresh_mod, id_kwarg = INLINE_REFRESH_BY_ENTITY.get(entity_type, (None, None))
    if not refresh_mod:
        return {"skipped": True, "reason": f"no refresh module for {entity_type}"}
    try:
        return refresh_mod.main(**{id_kwarg: entity_id, "qbo_body": qbo_body})
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def mark_cache_drift(conn, schema, table, id_col, entity_id):
    cur = conn.cursor()
    try:
        query = psql.SQL(
            """
            UPDATE {schema}.{table}
            SET sync_state = 'drift_detected',
                sync_state_changed_at = now()
            WHERE {id_col} = %s
            """
        ).format(
            schema=psql.Identifier(schema),
            table=psql.Identifier(table),
            id_col=psql.Identifier(id_col),
        )
        cur.execute(query, (entity_id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"  could not mark drift on {schema}.{table}:{entity_id}: {e}")
    finally:
        cur.close()


def flag_missing_webhooks(conn):
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE billing.webhook_expectations
        SET status = 'missing'
        WHERE status = 'pending' AND expected_by < now()
        RETURNING id
        """
    )
    flagged = cur.fetchall()
    conn.commit()
    cur.close()
    return len(flagged)


def auto_resolve_caught_up_drift(conn):
    cur = conn.cursor()
    cur.execute(
        """
        WITH caught_up AS (
          SELECT d.id
            FROM billing.drift_log d
            JOIN billing.invoices i ON i.qbo_invoice_id = d.entity_id
           WHERE d.entity_type = 'Invoice'
             AND (d.resolution IS NULL OR d.resolution = 'flagged_for_review')
             AND i.sync_state = 'synced'
             AND i.qbo_last_updated_time IS NOT NULL
             AND i.qbo_last_updated_time
                 >= ((d.cache_state->>'qbo_last_updated_time')::timestamptz)
        )
        UPDATE billing.drift_log d
           SET resolution = 'auto_recovered',
               resolution_at = now(),
               resolved_by = 'cdc_reconciler_sweep'
          FROM caught_up c
         WHERE d.id = c.id
         RETURNING d.id
        """
    )
    rows = cur.fetchall()
    conn.commit()
    cur.close()
    return len(rows)


def prune_drift_log(conn, days=DRIFT_LOG_RETENTION_DAYS):
    cur = conn.cursor()
    cur.execute("SELECT public.prune_drift_log(%s)", (days,))
    deleted = cur.fetchone()[0] or 0
    conn.commit()
    cur.close()
    return deleted


# ---------------------------------------------------------------------------
# Per-entity processing
# ---------------------------------------------------------------------------

def process_entity(conn, entity_type, qbo_entity, schema, table, id_col):
    """Classify drift + log it + heal via inline refresh.

    Returns (qbo_updated_for_cursor, drift_kind, refresh_result).

    `qbo_updated_for_cursor` is None when the inline refresh failed —
    that prevents the caller from advancing the cursor past an entity
    we never managed to write, so the next reconciler run will retry.
    """
    entity_id = qbo_entity["Id"]
    qbo_updated = parse_qbo_timestamp(
        qbo_entity.get("MetaData", {}).get("LastUpdatedTime")
    )
    if not qbo_updated:
        return None, None, None

    cached = load_cached(conn, schema, table, id_col, entity_id)

    if cached is None:
        # Brand-new entity — capture an initial snapshot diff for the log.
        snapshot_diff = compute_field_diff(entity_type, {}, qbo_entity)
        log_drift(
            conn, entity_type, entity_id,
            kind="missing_in_cache", severity="soft",
            cache_state=None,
            qbo_state={"id": entity_id, "qbo_updated": qbo_updated.isoformat()},
            resolution="auto_healed",
            field_diff=snapshot_diff or None,
        )
        refresh_result = trigger_inline_refresh(entity_type, entity_id, qbo_entity)
        # On refresh error, hold the cursor at this entity so we retry it.
        ts_for_cursor = None if (refresh_result and refresh_result.get("error")) else qbo_updated
        return ts_for_cursor, "missing_in_cache", refresh_result

    cached_updated = cached.get("qbo_last_updated_time") or cached.get("qbo_last_updated")

    if cached_updated is None or qbo_updated > cached_updated:
        diff = compute_field_diff(entity_type, cached, qbo_entity)
        log_drift(
            conn, entity_type, entity_id,
            kind="cache_stale", severity="soft",
            cache_state={"qbo_last_updated_time": cached_updated.isoformat() if cached_updated else None},
            qbo_state={"qbo_updated": qbo_updated.isoformat()},
            resolution="auto_healed",
            field_diff=diff or None,
        )
        refresh_result = trigger_inline_refresh(entity_type, entity_id, qbo_entity)
        ts_for_cursor = None if (refresh_result and refresh_result.get("error")) else qbo_updated
        return ts_for_cursor, "cache_stale", refresh_result

    if qbo_updated < cached_updated:
        # Cache claims newer than QBO — DO NOT call refresh (would replace
        # newer cache with older QBO data; OCC would block but we don't even
        # want to attempt). Log + flag for human.
        diff = compute_field_diff(entity_type, cached, qbo_entity)
        log_drift(
            conn, entity_type, entity_id,
            kind="cache_ahead", severity="critical",
            cache_state={"qbo_last_updated_time": cached_updated.isoformat()},
            qbo_state={"qbo_updated": qbo_updated.isoformat()},
            resolution="flagged_for_review",
            field_diff=diff or None,
        )
        mark_cache_drift(conn, schema, table, id_col, entity_id)
        return qbo_updated, "cache_ahead", None

    return qbo_updated, None, None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    """Run the reconciler. Schedule: every 15 minutes."""
    started = time.time()
    conn = get_db_conn()

    try:
        cursor = get_cursor(conn)
        print(f"=== CDC reconciler starting (cursor={cursor}) ===")

        access_token, realm_id = refresh_qbo_token()

        try:
            cdc_response = qbo_cdc(
                access_token, realm_id, ENTITIES_TO_RECONCILE, cursor,
            )
        except Exception as e:
            save_cursor(conn, cursor, "failed", 0, 0, f"cdc_fetch: {str(e)[:300]}")
            raise

        # Sort by qbo_updated ascending so cursor advances incrementally.
        flat = []
        for entity_type, entities in cdc_response.items():
            for ent in entities:
                ts = parse_qbo_timestamp(ent.get("MetaData", {}).get("LastUpdatedTime"))
                flat.append((ts, entity_type, ent))
        flat.sort(key=lambda r: r[0] or datetime.min.replace(tzinfo=timezone.utc))

        entities_processed = 0
        drift_records = []
        processing_errors = []
        refresh_failures = []
        progress_cursor = cursor

        for qbo_updated, entity_type, qbo_entity in flat:
            schema, table, id_col = ENTITY_TO_TABLE.get(
                entity_type, (None, None, None),
            )
            if not schema:
                continue

            entity_id = qbo_entity.get("Id", "<unknown>")
            try:
                ts, drift_kind, refresh_result = process_entity(
                    conn, entity_type, qbo_entity, schema, table, id_col,
                )
                entities_processed += 1
                if drift_kind:
                    drift_records.append((drift_kind, entity_id))
                if refresh_result and refresh_result.get("error"):
                    refresh_failures.append({
                        "entity_type": entity_type, "entity_id": entity_id,
                        "error": refresh_result["error"],
                    })
                if ts and ts > progress_cursor:
                    progress_cursor = ts
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e)[:200]}"
                print(f"  ERROR processing {entity_type}:{entity_id}: {msg}")
                try:
                    conn.rollback()
                except Exception:
                    pass
                try:
                    log_drift(
                        conn, entity_type, entity_id,
                        kind="processing_error", severity="hard",
                        cache_state={"error": msg},
                        qbo_state=(
                            {"qbo_updated": qbo_updated.isoformat()}
                            if qbo_updated else None
                        ),
                        resolution="flagged_for_review",
                        field_diff=None,
                    )
                except Exception as inner:
                    print(f"  could not log drift error: {inner}")
                processing_errors.append((entity_type, entity_id, msg))

        save_cursor(
            conn,
            progress_cursor,
            "succeeded" if not processing_errors else "partial",
            entities_processed,
            len(drift_records),
            (
                f"{len(processing_errors)} per-entity errors, "
                f"{len(refresh_failures)} refresh failures"
                if (processing_errors or refresh_failures) else None
            ),
        )

        cleared = auto_resolve_caught_up_drift(conn)
        missing_webhooks_count = flag_missing_webhooks(conn)
        pruned = prune_drift_log(conn)

        elapsed = time.time() - started
        cursor_advance_s = (progress_cursor - cursor).total_seconds()
        print(
            f"=== reconciler done in {elapsed:.1f}s: "
            f"processed={entities_processed} drift={len(drift_records)} "
            f"errors={len(processing_errors)} refresh_failures={len(refresh_failures)} "
            f"caught_up_resolved={cleared} missing_webhooks={missing_webhooks_count} "
            f"pruned={pruned} cursor_advance={cursor_advance_s:.0f}s ==="
        )

        return {
            "status":                   "succeeded" if not processing_errors else "partial",
            "elapsed_s":                round(elapsed, 1),
            "cursor_advance_s":         cursor_advance_s,
            "entities_processed":       entities_processed,
            "drift_count":              len(drift_records),
            "drift_sample":             drift_records[:10],
            "processing_errors":        processing_errors[:10],
            "refresh_failures":         refresh_failures[:10],
            "refresh_failure_count":    len(refresh_failures),
            "caught_up_drift_resolved": cleared,
            "missing_webhooks_flagged": missing_webhooks_count,
            "drift_log_pruned":         pruned,
            "new_cursor":               progress_cursor.isoformat(),
        }

    finally:
        conn.close()
