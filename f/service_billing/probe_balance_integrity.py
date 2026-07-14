# requirements:
# psycopg2-binary
# requests
# wmill

# f/service_billing/probe_balance_integrity — the ledger-healing probe.
#
# "Healed" means the DERIVED balance converges to the leader's because the
# INPUT ENTITIES became complete — never because we copied the number
# (Carter 2026-07-14). For each mismatch in v_invoice_balance_integrity:
#   1. fetch the invoice fresh; walk its LinkedTxn (QBO lists every
#      transaction applied to the invoice — the ids we can't know locally)
#   2. diff against the applications our cache holds
#   3. enqueue missing Payments/CreditMemos into billing.qbo_inbox
#      (source='probe') — the drainer's refresh handlers land the missing
#      lines and the derivation converges on its own
#   4. any LinkedTxn type we don't model (JournalEntry, Deposit, ...) is
#      logged to drift_log as kind='unmodeled_application' — the EVIDENCE
#      for which entity class earns cache status next
#
# Read + enqueue only: this probe never writes cache values itself.
# Schedule: [pending schedules scope] — run manually / after sweeps for now.

import psycopg2.extras

from f.billing._lib.db import get_db_conn
from f.billing._lib.qbo import set_rate_limiter, refresh_qbo_token, fetch_qbo_invoice

MODELED_TYPES = {"Payment", "CreditMemo"}

MISMATCHES = """
SELECT qbo_invoice_id, doc_number, leader_balance, derived_balance, diff
FROM billing.v_invoice_balance_integrity
WHERE mismatch
ORDER BY diff DESC
LIMIT %s
"""

KNOWN_APPLICATIONS = """
SELECT DISTINCT p.qbo_payment_id
FROM billing.customer_payments p
CROSS JOIN LATERAL jsonb_array_elements(coalesce(p.raw -> 'Line', '[]'::jsonb)) line
CROSS JOIN LATERAL jsonb_array_elements(coalesce(line.value -> 'LinkedTxn', '[]'::jsonb)) lt
WHERE lt.value ->> 'TxnType' = 'Invoice' AND lt.value ->> 'TxnId' = %s
"""


def _rows(conn, sql, params):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]; cur.close()
    return rows


def _exec(conn, sql, params):
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit(); cur.close()


def _enqueue(conn, entity_type, entity_id):
    _exec(conn, "SELECT public.enqueue_qbo_inbox(%s, %s, 'Update', NULL, 'probe', 2)",
          (entity_type, entity_id))


def _log_unmodeled(conn, qbo_invoice_id, txn_type, txn_id):
    _exec(conn, """
        INSERT INTO billing.drift_log
          (entity_type, entity_id, kind, severity, field_diff)
        VALUES ('Invoice', %s, 'unmodeled_application', 'hard',
                jsonb_build_object('txn_type', %s, 'txn_id', %s))
    """, (qbo_invoice_id, txn_type, txn_id))


def main(limit: int = 25):
    """Discover and enqueue the missing balance inputs for every mismatched
    invoice. Returns per-invoice discovery results."""
    limit = limit or 25
    conn = get_db_conn()
    set_rate_limiter(conn)  # ADR 008 §4
    try:
        mismatches = _rows(conn, MISMATCHES, (limit,))
        if not mismatches:
            return {"status": "ok", "mismatches": 0, "note": "ledger fully converged"}

        access_token, realm_id = refresh_qbo_token()
        results, stats = [], {"enqueued": 0, "unmodeled": 0, "fetch_failed": 0}
        for m in mismatches:
            inv_id = m["qbo_invoice_id"]
            inv, err = fetch_qbo_invoice(inv_id, access_token, realm_id)
            if not inv:
                stats["fetch_failed"] += 1
                results.append({"invoice": inv_id, "doc": m["doc_number"],
                                "outcome": f"fetch_failed: {err}"})
                continue

            known = {r["qbo_payment_id"] for r in _rows(conn, KNOWN_APPLICATIONS, (inv_id,))}
            missing, unmodeled = [], []
            for lt in inv.get("LinkedTxn") or []:
                t, tid = lt.get("TxnType"), lt.get("TxnId")
                if not t or not tid:
                    continue
                if t in MODELED_TYPES:
                    cache_key = f"CM-{tid}" if t == "CreditMemo" else tid
                    if cache_key not in known:
                        _enqueue(conn, t, tid)
                        missing.append(f"{t}:{tid}")
                        stats["enqueued"] += 1
                else:
                    _log_unmodeled(conn, inv_id, t, tid)
                    unmodeled.append(f"{t}:{tid}")
                    stats["unmodeled"] += 1

            # refresh the invoice snapshot too (cheap, coalesced) so
            # leader_balance/raw are current for the next integrity read
            _enqueue(conn, "Invoice", inv_id)

            results.append({"invoice": inv_id, "doc": m["doc_number"],
                            "diff": float(m["diff"]),
                            "missing_enqueued": missing or None,
                            "unmodeled_types": unmodeled or None,
                            "linked_txn_count": len(inv.get("LinkedTxn") or [])})
            print(f"  {inv_id} (#{m['doc_number']}, diff {m['diff']}): "
                  f"+{len(missing)} enqueued, {len(unmodeled)} unmodeled")

        return {"status": "ok", "mismatches": len(mismatches), "stats": stats,
                "results": results}
    finally:
        conn.close()
