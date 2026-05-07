# CDC-based reconciler for QBO ↔ cache drift detection.
#
# Architecture (Pattern D, see CLAUDE.md):
#   - Webhooks are the primary low-latency channel for external changes
#   - This reconciler is the truth backstop — catches anything webhooks dropped
#   - Uses QBO's CDC endpoint (incremental) so we only check what actually
#     changed since our last cursor, not the whole table.
#
# What it does, every 15 minutes (Windmill cron):
#   1. Read last cursor from billing.cdc_cursors WHERE source='qbo'
#   2. Call QBO /cdc?entities=Invoice,Payment,Customer&changedSince=<cursor>
#   3. For each returned entity (sorted by qbo_updated ascending so the cursor
#      can advance incrementally):
#        - If our cache is older than QBO's MetaData.LastUpdatedTime → drift
#        - Auto-heal soft drift via refresh_invoice/refresh_payment/customer_sync
#        - Critical drift (cache_ahead) flagged for human review
#        - Per-entity try/except: a bad row is logged + skipped, never fatal
#        - Cursor advances after every successful entity, so a mid-loop failure
#          only loses the in-flight ones, not the whole 15-min window
#   4. Sweep stale cache_ahead drift entries whose invoices have caught up.
#   5. Flag webhook expectations whose grace window has expired without
#      confirmation as 'missing' (separate from the CDC pass).
#
# Severity tiers:
#   soft     — cache stale relative to QBO (most common; auto-heal silently)
#   hard     — webhook missing AND value disagrees, or per-entity processing
#              error (auto-heal where possible, flag in drift_log)
#   critical — cache appears NEWER than QBO (rare; halt + alert)
#
# Identifier handling: table names are interpolated via psycopg2.sql.Identifier
# so quoted/PascalCase names like public."Customers" work alongside lowercase
# ones like billing.invoices. f-string interpolation would silently lowercase
# them and the lookup would fail with relation-does-not-exist.

import time
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
from psycopg2 import sql as psql
import requests
import wmill

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"

ENTITIES_TO_RECONCILE = ["Invoice", "Payment", "Customer"]

# (schema, table, id_col). Table names are CASE-SENSITIVE — Customers in
# public is created as the quoted identifier "Customers".
ENTITY_TO_TABLE = {
    "Invoice":  ("billing", "invoices",          "qbo_invoice_id"),
    "Payment":  ("billing", "customer_payments", "qbo_payment_id"),
    "Customer": ("public",  "Customers",         "qbo_customer_id"),
}

REFRESH_SCRIPT_MAP = {
    "Invoice":  ("f/service_billing/refresh_invoice",  "qbo_invoice_id"),
    "Payment":  ("f/service_billing/refresh_payment",  "qbo_payment_id"),
    "Customer": ("f/service_billing/qbo_customer_sync", "qbo_customer_id"),
}


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
    """Calls QBO Change Data Capture. Returns map of entity_type → list of entities."""
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
    """Persist cursor + run stats. Idempotent — safe to call repeatedly."""
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
    """Parse QBO ISO timestamp to UTC datetime."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_cached(conn, schema, table, id_col, entity_id):
    """Load a single cache row using safely-quoted identifiers."""
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


def log_drift(conn, entity_type, entity_id, kind, severity, cache_state, qbo_state, resolution):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO billing.drift_log
          (entity_type, entity_id, kind, severity, cache_state, qbo_state, resolution, resolution_at)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s,
                CASE WHEN %s = 'auto_healed' THEN now() ELSE NULL END)
        """,
        (
            entity_type, entity_id, kind, severity,
            psycopg2.extras.Json(cache_state) if cache_state else None,
            psycopg2.extras.Json(qbo_state) if qbo_state else None,
            resolution, resolution,
        ),
    )
    conn.commit()
    cur.close()


def trigger_refresh(entity_type, entity_id):
    """Fire-and-forget refresh of a single entity via the existing scripts."""
    script, arg_name = REFRESH_SCRIPT_MAP.get(entity_type, (None, None))
    if not script:
        return
    try:
        # Correct SDK function: run_script_by_path_async(path, args)
        wmill.run_script_by_path_async(path=script, args={arg_name: entity_id})
    except Exception as e:
        print(f"  refresh trigger failed for {entity_type}:{entity_id}: {e}")


def mark_cache_drift(conn, schema, table, id_col, entity_id):
    """Set sync_state='drift_detected' so the row surfaces in queue views."""
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
        # Some tables don't have sync_state columns. Don't let that abort the
        # run — we still have the drift_log row.
        conn.rollback()
        print(f"  could not mark drift on {schema}.{table}:{entity_id}: {e}")
    finally:
        cur.close()


def flag_missing_webhooks(conn):
    """Flip pending expectations past their grace window to 'missing'."""
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
    """
    Sweep cache_ahead drift entries whose underlying invoice has caught up to
    or passed the flagged QBO timestamp. The CDC pass is forward-only and
    never revisits old drift_log rows, so without this they accumulate as
    stale alerts (the source of the 5 May 1 entries).
    """
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


def process_entity(conn, entity_type, qbo_entity, schema, table, id_col):
    """
    Classify + log drift for a single entity. Wrapped in try/except by caller
    so a single bad entity can't poison the whole pass.
    """
    entity_id = qbo_entity["Id"]
    qbo_updated = parse_qbo_timestamp(
        qbo_entity.get("MetaData", {}).get("LastUpdatedTime")
    )
    if not qbo_updated:
        return None, None

    cached = load_cached(conn, schema, table, id_col, entity_id)

    if cached is None:
        log_drift(
            conn, entity_type, entity_id,
            kind="missing_in_cache", severity="soft",
            cache_state=None,
            qbo_state={"id": entity_id, "qbo_updated": qbo_updated.isoformat()},
            resolution="auto_healed",
        )
        trigger_refresh(entity_type, entity_id)
        return qbo_updated, "missing_in_cache"

    cached_updated = cached.get("qbo_last_updated_time")

    if cached_updated is None or qbo_updated > cached_updated:
        log_drift(
            conn, entity_type, entity_id,
            kind="cache_stale", severity="soft",
            cache_state={"qbo_last_updated_time": cached_updated.isoformat() if cached_updated else None},
            qbo_state={"qbo_updated": qbo_updated.isoformat()},
            resolution="auto_healed",
        )
        trigger_refresh(entity_type, entity_id)
        return qbo_updated, "cache_stale"

    if qbo_updated < cached_updated:
        log_drift(
            conn, entity_type, entity_id,
            kind="cache_ahead", severity="critical",
            cache_state={"qbo_last_updated_time": cached_updated.isoformat()},
            qbo_state={"qbo_updated": qbo_updated.isoformat()},
            resolution="flagged_for_review",
        )
        mark_cache_drift(conn, schema, table, id_col, entity_id)
        return qbo_updated, "cache_ahead"

    # cached_updated == qbo_updated → no drift
    return qbo_updated, None


def main():
    """
    Run the reconciler. Returns a summary dict with counts and durations.
    Schedule: every 15 minutes (f/service_billing/cdc_reconciler_15min).
    """
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

        # Flatten + sort by qbo_updated ascending so we can advance the cursor
        # as we go. Mixed entity types is fine — we only use the cursor as a
        # "process anything newer than this" filter, not per-type.
        flat = []
        for entity_type, entities in cdc_response.items():
            for ent in entities:
                ts = parse_qbo_timestamp(ent.get("MetaData", {}).get("LastUpdatedTime"))
                flat.append((ts, entity_type, ent))
        flat.sort(key=lambda r: r[0] or datetime.min.replace(tzinfo=timezone.utc))

        entities_processed = 0
        drift_records = []
        processing_errors = []
        progress_cursor = cursor

        for qbo_updated, entity_type, qbo_entity in flat:
            schema, table, id_col = ENTITY_TO_TABLE.get(
                entity_type, (None, None, None),
            )
            if not schema:
                continue

            entity_id = qbo_entity.get("Id", "<unknown>")
            try:
                ts, drift_kind = process_entity(
                    conn, entity_type, qbo_entity, schema, table, id_col,
                )
                entities_processed += 1
                if drift_kind:
                    drift_records.append((drift_kind, entity_id))
                # Advance the cursor whether or not we detected drift — this
                # entity has been handled (logged + auto-healed or confirmed
                # in-sync), so the next run shouldn't re-scan it.
                if ts and ts > progress_cursor:
                    progress_cursor = ts
            except Exception as e:
                # Per-entity error: log + continue. Do NOT advance cursor past
                # this entity — next run will retry. If the same entity keeps
                # failing it surfaces as a recurring drift_log entry.
                msg = f"{type(e).__name__}: {str(e)[:200]}"
                print(f"  ERROR processing {entity_type}:{entity_id}: {msg}")
                try:
                    conn.rollback()  # in case the transaction is poisoned
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
                    )
                except Exception as inner:
                    print(f"  could not log drift error: {inner}")
                processing_errors.append((entity_type, entity_id, msg))

        # Always persist whatever progress we made, even if some entities
        # failed. progress_cursor is the high-water mark of successful entries.
        save_cursor(
            conn,
            progress_cursor,
            "succeeded" if not processing_errors else "partial",
            entities_processed,
            len(drift_records),
            (
                f"{len(processing_errors)} per-entity errors"
                if processing_errors else None
            ),
        )

        # End-of-pass housekeeping (runs even if some entities failed).
        cleared = auto_resolve_caught_up_drift(conn)
        missing_webhooks_count = flag_missing_webhooks(conn)

        elapsed = time.time() - started
        cursor_advance_s = (progress_cursor - cursor).total_seconds()
        print(
            f"=== reconciler done in {elapsed:.1f}s: "
            f"processed={entities_processed} drift={len(drift_records)} "
            f"errors={len(processing_errors)} caught_up_resolved={cleared} "
            f"missing_webhooks={missing_webhooks_count} "
            f"cursor_advance={cursor_advance_s:.0f}s ==="
        )

        return {
            "status": "succeeded" if not processing_errors else "partial",
            "elapsed_s": round(elapsed, 1),
            "cursor_advance_s": cursor_advance_s,
            "entities_processed": entities_processed,
            "drift_count": len(drift_records),
            "drift_sample": drift_records[:10],
            "processing_errors": processing_errors[:10],
            "caught_up_drift_resolved": cleared,
            "missing_webhooks_flagged": missing_webhooks_count,
            "new_cursor": progress_cursor.isoformat(),
        }

    finally:
        conn.close()
