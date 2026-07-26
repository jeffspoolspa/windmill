# requirements:
# psycopg2-binary
# requests
# wmill

# f/service_billing/process_invoice — the service-billing queue worker:
# claim one ready invoice, run the sentence (process_one), repeat until
# empty. Called with no args by the wake trigger / heartbeat; with ids by
# the UI (singles and batches both enqueue — the queue is the only door).
# Rate is governed by the _lib/qbo token bucket, nothing else. Force /
# recovery are process_one / recover_payment's own entries.

from f.billing._lib.db import get_db_conn, query_one, execute_sql
from f.billing._lib.qbo import set_rate_limiter, refresh_qbo_token
from f.service_billing.process_one import process_one

MAX_UNITS = 1000  # runaway backstop; a drain normally ends at queue-empty

# THE GATE LIVES IN THE CLAIM: readiness is checked atomically with the row
# lock, so a state change can never race a claim (the dequeue trigger clears
# waiting rows; this closes the delete-vs-claim window for good).
CLAIM = """
UPDATE billing.service_charge_queue
SET started_at = now(), attempts = attempts + 1
WHERE id = (SELECT id FROM billing.service_charge_queue
            WHERE finished_at IS NULL AND attempts < 3
              AND billing.invoice_ready(qbo_invoice_id)
            ORDER BY priority, received_at
            FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id, qbo_invoice_id
"""


def main(qbo_invoice_id: str = None, qbo_invoice_ids: list = None):
    conn = get_db_conn()
    set_rate_limiter(conn)
    try:
        access_token, realm_id = refresh_qbo_token()

        ids = list(dict.fromkeys(([qbo_invoice_id] if qbo_invoice_id else [])
                                 + (qbo_invoice_ids or [])))
        if ids:
            execute_sql(conn, """INSERT INTO billing.service_charge_queue
                                   (qbo_invoice_id, priority)
                                 SELECT unnest(%s::text[]), 1
                                 ON CONFLICT (qbo_invoice_id)
                                   WHERE finished_at IS NULL DO NOTHING""", (ids,))

        drained = 0
        while drained < MAX_UNITS:
            claimed = query_one(conn, CLAIM)
            conn.commit()
            if not claimed:
                break
            drained += 1
            try:
                outcome = process_one(conn, claimed["qbo_invoice_id"],
                                      access_token, realm_id)
                if outcome["status"] == "retry":
                    # balance drift: cache resynced; re-open the claim so the
                    # atomic gate re-asks on the NEW state (3 drifts dead-letter)
                    execute_sql(conn, "UPDATE billing.service_charge_queue "
                                      "SET started_at = NULL, error = %s "
                                      "WHERE id = %s",
                                (outcome["reason"][:300], claimed["id"]))
                else:
                    # error preserved: a success must not erase the failure before it
                    execute_sql(conn, "UPDATE billing.service_charge_queue "
                                      "SET finished_at = now() "
                                      "WHERE id = %s", (claimed["id"],))
            except Exception as e:
                conn.rollback()  # poison unit: stays claimable until 3 attempts
                outcome = {"status": "error", "error": str(e)[:300]}
                execute_sql(conn, "UPDATE billing.service_charge_queue "
                                  "SET started_at = NULL, error = %s "
                                  "WHERE id = %s", (str(e)[:300], claimed["id"]))
            print(f"  [{drained}] {claimed['qbo_invoice_id']} -> {outcome['status']}"
                  + (f" ({outcome.get('reason') or outcome.get('error') or ''})"
                     if outcome["status"] != "succeeded" else ""))
        print(f"=== drained {drained} ===")
        return {"status": "success", "drained": drained}
    finally:
        conn.close()
