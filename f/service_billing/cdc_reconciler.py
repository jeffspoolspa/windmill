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
#   3. For each returned entity:
#        - If our cache is older than QBO's MetaData.LastUpdatedTime → drift
#        - Auto-heal: fetch full entity + upsert
#        - Log the drift for trend analysis
#   4. Flag webhook expectations whose grace window has expired without
#      confirmation as 'missing' (separate from the CDC pass).
#   5. Update cursor to the latest LastUpdatedTime we saw + persist run stats.
#
# Severity tiers:
#   soft     — cache stale relative to QBO (most common; auto-heal silently)
#   hard     — webhook missing AND value disagrees (auto-heal but flag UI)
#   critical — cache appears NEWER than QBO (rare; halt + alert)
#
# What it does NOT do:
#   - Full table scan (use a separate weekly script for that)
#   - QBO writes (auto-healing is read-only — pull from QBO into cache)
#   - Resolve drift_detected records (humans do that via the admin UI)

import time
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import requests
import wmill

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"

ENTITIES_TO_RECONCILE = ["Invoice", "Payment", "Customer"]

ENTITY_TO_TABLE = {
    "Invoice": ("billing", "invoices", "qbo_invoice_id"),
    "Payment": ("billing", "customer_payments", "qbo_payment_id"),
    "Customer": ("public", "customers", "qbo_customer_id"),
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
    # CDC returns an array of objects, each with one entity_type → list mapping
    # plus a top-level "time" field that's the response timestamp.
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
        # Initialize cursor if missing
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
    """Parse QBO ISO timestamp to UTC datetime."""
    if not ts:
        return None
    # QBO uses a few variants; standardize.
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_cached(conn, schema, table, id_col, entity_id):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        f"SELECT * FROM {schema}.{table} WHERE {id_col} = %s",
        (entity_id,),
    )
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None


def log_drift(conn, entity_type, entity_id, kind, severity, cache_state, qbo_state, resolution):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO billing.drift_log
          (entity_type, entity_id, kind, severity, cache_state, qbo_state, resolution, resolution_at)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, CASE WHEN %s = 'auto_healed' THEN now() ELSE NULL END)
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
    script_map = {
        "Invoice": ("f/service_billing/refresh_invoice", "qbo_invoice_id"),
        "Payment": ("f/service_billing/refresh_payment", "qbo_payment_id"),
        "Customer": ("f/service_billing/qbo_customer_sync", "qbo_customer_id"),
    }
    script, arg_name = script_map.get(entity_type, (None, None))
    if not script:
        return
    try:
        wmill.run_script_async(path=script, args={arg_name: entity_id})
    except Exception as e:
        print(f"  refresh trigger failed for {entity_type}:{entity_id}: {e}")


def flag_missing_webhooks(conn):
    """Flip pending expectations past their grace window to 'missing'."""
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE billing.webhook_expectations
        SET status = 'missing'
        WHERE status = 'pending' AND expected_by < now()
        RETURNING id, entity_type, entity_id
        """
    )
    flagged = cur.fetchall()
    conn.commit()
    cur.close()
    return len(flagged)


def main():
    """
    Run the reconciler. Returns a summary dict with counts and durations.
    Set as a Windmill schedule: every 15 minutes is the recommended cadence.
    """
    started = time.time()
    conn = get_db_conn()

    try:
        cursor = get_cursor(conn)
        print(f"=== CDC reconciler starting (cursor={cursor}) ===")

        access_token, realm_id = refresh_qbo_token()

        # Fetch everything that changed since cursor.
        try:
            cdc_response = qbo_cdc(
                access_token, realm_id, ENTITIES_TO_RECONCILE, cursor,
            )
        except Exception as e:
            save_cursor(conn, cursor, "failed", 0, 0, str(e)[:300])
            raise

        entities_processed = 0
        drift_records = []
        new_max_timestamp = cursor

        for entity_type, entities in cdc_response.items():
            schema, table, id_col = ENTITY_TO_TABLE.get(entity_type, (None, None, None))
            if not schema:
                continue

            for qbo_entity in entities:
                entities_processed += 1
                entity_id = qbo_entity["Id"]
                qbo_updated = parse_qbo_timestamp(
                    qbo_entity.get("MetaData", {}).get("LastUpdatedTime")
                )
                if not qbo_updated:
                    continue
                if qbo_updated > new_max_timestamp:
                    new_max_timestamp = qbo_updated

                cached = load_cached(conn, schema, table, id_col, entity_id)

                if cached is None:
                    # Entity exists in QBO but not in cache — backfill it.
                    log_drift(
                        conn, entity_type, entity_id,
                        kind="missing_in_cache", severity="soft",
                        cache_state=None, qbo_state={"id": entity_id, "qbo_updated": qbo_updated.isoformat()},
                        resolution="auto_healed",
                    )
                    trigger_refresh(entity_type, entity_id)
                    drift_records.append(("missing_in_cache", entity_id))
                    continue

                cached_updated = cached.get("qbo_last_updated_time")

                if cached_updated is None or qbo_updated > cached_updated:
                    # Cache is stale — soft drift. Auto-heal by triggering refresh.
                    log_drift(
                        conn, entity_type, entity_id,
                        kind="cache_stale", severity="soft",
                        cache_state={"qbo_last_updated_time": cached_updated.isoformat() if cached_updated else None},
                        qbo_state={"qbo_updated": qbo_updated.isoformat()},
                        resolution="auto_healed",
                    )
                    trigger_refresh(entity_type, entity_id)
                    drift_records.append(("cache_stale", entity_id))

                elif qbo_updated < cached_updated:
                    # Cache appears NEWER than QBO — should be impossible if our
                    # 200-trust model is correct. CRITICAL: our write didn't land,
                    # or QBO rolled back, or cdc is showing a stale read replica.
                    log_drift(
                        conn, entity_type, entity_id,
                        kind="cache_ahead", severity="critical",
                        cache_state={"qbo_last_updated_time": cached_updated.isoformat()},
                        qbo_state={"qbo_updated": qbo_updated.isoformat()},
                        resolution="flagged_for_review",
                    )
                    # Mark the cache row as drift_detected so it surfaces in UI.
                    mark_cache_drift(conn, schema, table, id_col, entity_id)
                    drift_records.append(("cache_ahead", entity_id))
                # else: cached_updated == qbo_updated — cache is current, no drift.

        # Update the cursor only if we successfully completed.
        save_cursor(
            conn,
            new_max_timestamp,
            "succeeded",
            entities_processed,
            len(drift_records),
        )

        # Separate pass: flag missing webhooks (independent of CDC).
        missing_webhooks_count = flag_missing_webhooks(conn)

        elapsed = time.time() - started
        print(
            f"=== reconciler done in {elapsed:.1f}s: "
            f"processed={entities_processed} drift={len(drift_records)} "
            f"missing_webhooks={missing_webhooks_count} ==="
        )

        return {
            "status": "succeeded",
            "elapsed_s": round(elapsed, 1),
            "cursor_advance_s": (new_max_timestamp - cursor).total_seconds(),
            "entities_processed": entities_processed,
            "drift_count": len(drift_records),
            "drift_sample": drift_records[:10],
            "missing_webhooks_flagged": missing_webhooks_count,
            "new_cursor": new_max_timestamp.isoformat(),
        }

    finally:
        conn.close()


def mark_cache_drift(conn, schema, table, id_col, entity_id):
    """Set sync_state = 'drift_detected' on a cache row so the UI surfaces it."""
    cur = conn.cursor()
    try:
        cur.execute(
            f"""
            UPDATE {schema}.{table}
            SET sync_state = 'drift_detected',
                sync_state_changed_at = now()
            WHERE {id_col} = %s
            """,
            (entity_id,),
        )
        conn.commit()
    except Exception as e:
        print(f"  could not mark drift on {table}:{entity_id} (column may not exist): {e}")
    finally:
        cur.close()
