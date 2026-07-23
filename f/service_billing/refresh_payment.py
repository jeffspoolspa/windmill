# f/service_billing/refresh_payment
#
# Single-payment QBO -> Supabase refresh.
#
# Callers:
#   - QBO webhook handler:   main(qbo_payment_id)
#                            — fetches the payment from QBO and refreshes
#   - cdc_reconciler:        main(qbo_payment_id, qbo_body=<cdc_entity>)
#                            — passes the body it already has from CDC,
#                              skipping the QBO GET. Single source of truth
#                              for the upsert + side effects.
#
# Concurrency: the upsert uses an OCC guard on qbo_last_updated_time.
# Two concurrent callers writing the same payment never clobber each other —
# whichever has the newer QBO timestamp wins, the other's UPDATE is a no-op.
#
# Side effects (CCTransId verification, linked-invoice rechecks) run even
# when did_write is false — they read current state, not "what we just wrote",
# so they're safe and useful regardless.

import json
from datetime import date, datetime
from decimal import Decimal

import psycopg2
import psycopg2.extras
import requests
import wmill

from f.billing._lib.events import emit

QBO_RESOURCE = "u/carter/quickbooks_api"
SUPABASE_RESOURCE = "u/carter/supabase"


def _json_default(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def _dumps(obj):
    return json.dumps(obj, default=_json_default)


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


def qbo_get(path, access_token, realm_id):
    return requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/{path}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=30,
    )


def parse_qbo_timestamp(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _application_lines(raw):
    """The payment's application set from raw Line[].LinkedTxn:
    {invoice_id: {"amount": float, "cm_id": str|None}}. THE ledger facts
    (ADR 010 phase 3) — QBO's whole AR rides on these lines."""
    out = {}
    for line in (raw or {}).get("Line") or []:
        inv, cm = None, None
        for lt in line.get("LinkedTxn") or []:
            if lt.get("TxnType") == "Invoice":
                inv = lt.get("TxnId")
            elif lt.get("TxnType") == "CreditMemo":
                cm = lt.get("TxnId")
        if inv:
            amt = float(line.get("Amount") or 0)
            prev = out.get(inv, {"amount": 0.0, "cm_id": None})
            out[inv] = {"amount": round(prev["amount"] + amt, 2),
                        "cm_id": cm or prev["cm_id"]}
    return out


def _diff_applications(conn, qbo_payment_id, prior_raw, new_raw, ref_num,
                       qbo_customer_id, discovered_via):
    """Diff the application set, emit payment_applied / payment_unapplied,
    and FAN OUT: enqueue every delta invoice into billing.qbo_inbox so
    refresh_invoice fresh-reads the leader (verified echo — we NEVER compute
    an invoice balance from payment lines). Coalesced + drained in the same
    loop; CDC stays the backstop. Empty diff (our own write echoing back, or
    a re-delivered webhook) emits and enqueues nothing.
    Returns the list of delta invoice ids."""
    old_l = _application_lines(prior_raw)
    new_l = _application_lines(new_raw)
    added, removed = [], []
    for inv, d in new_l.items():
        delta = round(d["amount"] - old_l.get(inv, {}).get("amount", 0.0), 2)
        if delta > 0:
            added.append({"invoice_id": inv, "amount": delta,
                          **({"funding": {"kind": "credit_memo", "id": d["cm_id"]}}
                             if d["cm_id"] else {})})
        elif delta < 0:
            removed.append({"invoice_id": inv, "amount": -delta})
    for inv, d in old_l.items():
        if inv not in new_l and d["amount"] > 0:
            removed.append({"invoice_id": inv, "amount": d["amount"]})
    if not added and not removed:
        return []

    # provenance: ours if a WAL attempt recorded this payment (source intent)
    cur = conn.cursor()
    cur.execute("SELECT id FROM billing.processing_attempts "
                "WHERE qbo_payment_id = %s LIMIT 1", (qbo_payment_id,))
    attempt = cur.fetchone()
    prov = ({"source": "intent", "intent_ref": str(attempt[0])} if attempt
            else {"source": "external", "discovered_via": discovered_via})
    actor = "auto" if attempt else "qbo_webhook"
    parts = ([f"invoice:{e['invoice_id']}" for e in added + removed]
             + ([f"customer:{qbo_customer_id}"] if qbo_customer_id else []))
    if added:
        emit(conn, "payment", qbo_payment_id, "payment_applied",
             participants=parts,
             payload={"ref": ref_num, "lines": added, "provenance": prov},
             actor=actor)
    if removed:
        emit(conn, "payment", qbo_payment_id, "payment_unapplied",
             participants=parts,
             payload={"ref": ref_num, "lines": removed, "provenance": prov},
             actor=actor)
    # INCREMENTAL REPLICATION (Carter 2026-07-23): the delta is leader-
    # attested (this payload IS QBO's statement), so apply it to the cached
    # balance NOW — waiting for the read guarantees staleness for nothing.
    # Floored at 0 (QBO invoices don't go negative; overpayment lands as
    # UnappliedAmt on the payment). A balance reaching 0 fires auto-promote
    # in the same transaction. The fan-out read below stays as the ASYNC
    # verify + token audit, snapshotting truth seconds later.
    for e in added:
        cur.execute(
            "UPDATE billing.invoices SET balance = greatest(round((balance - %s)::numeric, 2), 0) "
            "WHERE qbo_invoice_id = %s AND balance IS NOT NULL",
            (e["amount"], e["invoice_id"]))
    for e in removed:
        cur.execute(
            "UPDATE billing.invoices SET balance = round((balance + %s)::numeric, 2) "
            "WHERE qbo_invoice_id = %s AND balance IS NOT NULL",
            (e["amount"], e["invoice_id"]))

    delta_invoices = sorted({e["invoice_id"] for e in added + removed})
    for inv in delta_invoices:
        cur.execute("SELECT public.enqueue_qbo_inbox('Invoice', %s, 'Update', "
                    "'{}'::jsonb, 'payment_fanout', 2)", (inv,))
    cur.close()
    return delta_invoices


def upsert_payment(conn, qbo_pmt):
    """Upsert with OCC guard. Returns (qbo_payment_id, did_write, payment_row).

    OCC: only updates when EXCLUDED.qbo_last_updated_time is strictly newer
    than the existing row's. New inserts (no conflict) always land.
    Race-loser's UPDATE matches zero rows; no harm because their data was
    older anyway.
    """
    customer_ref = qbo_pmt.get("CustomerRef") or {}
    payment_method_ref = qbo_pmt.get("PaymentMethodRef") or {}
    cc_info = qbo_pmt.get("CreditCardPayment") or {}
    cc_response = cc_info.get("CreditChargeResponse") or {}

    qbo_payment_id      = qbo_pmt.get("Id")
    qbo_customer_id     = customer_ref.get("value")
    total_amt           = float(qbo_pmt.get("TotalAmt") or 0)
    unapplied_amt       = float(qbo_pmt.get("UnappliedAmt") or 0)
    txn_date            = qbo_pmt.get("TxnDate")
    ref_num             = qbo_pmt.get("PaymentRefNum")
    memo                = qbo_pmt.get("PrivateNote")
    payment_method_id   = payment_method_ref.get("value")
    payment_method_name = payment_method_ref.get("name")
    qbo_last_updated    = parse_qbo_timestamp(
        (qbo_pmt.get("MetaData") or {}).get("LastUpdatedTime")
    )

    # cc_trans_id and cc_status are GENERATED ALWAYS columns derived from
    # raw->'CreditCardPayment'->'CreditChargeResponse'. We write raw and
    # they auto-populate; writing them directly raises a Postgres error.
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        INSERT INTO billing.customer_payments
          (qbo_payment_id, qbo_customer_id, type, total_amt, unapplied_amt,
           txn_date, ref_num, memo, payment_method_id, payment_method_name,
           raw, fetched_at,
           qbo_last_updated_time, sync_state, sync_state_changed_at)
        VALUES (%s, %s, 'payment', %s, %s, %s, %s, %s, %s, %s,
                %s::jsonb, now(), %s, 'synced', now())
        ON CONFLICT (qbo_payment_id) DO UPDATE SET
          qbo_customer_id       = EXCLUDED.qbo_customer_id,
          total_amt             = EXCLUDED.total_amt,
          unapplied_amt         = EXCLUDED.unapplied_amt,
          txn_date              = EXCLUDED.txn_date,
          ref_num               = EXCLUDED.ref_num,
          memo                  = EXCLUDED.memo,
          payment_method_id     = EXCLUDED.payment_method_id,
          payment_method_name   = EXCLUDED.payment_method_name,
          raw                   = EXCLUDED.raw,
          fetched_at            = now(),
          qbo_last_updated_time = EXCLUDED.qbo_last_updated_time,
          sync_state            = 'synced',
          sync_state_changed_at = now(),
          sync_error            = NULL
        WHERE billing.customer_payments.qbo_last_updated_time IS NULL
           OR EXCLUDED.qbo_last_updated_time IS NULL
           OR billing.customer_payments.qbo_last_updated_time < EXCLUDED.qbo_last_updated_time
        RETURNING *
    """, (
        qbo_payment_id, qbo_customer_id, total_amt, unapplied_amt,
        txn_date, ref_num, memo, payment_method_id, payment_method_name,
        _dumps(qbo_pmt), qbo_last_updated,
    ))
    row = cur.fetchone()
    cur.close()
    return qbo_payment_id, (row is not None), (dict(row) if row else None)


def verify_cc_trans_id(conn, qbo_payment_id, cc_trans_id_from_qbo):
    """Cross-check QBO's CCTransId against our processing_attempts.charge_id.

    Outcomes:
      no_attempt           — payment wasn't created by us (customer-initiated, etc.)
      verified             — match; happy path
      cc_trans_id_missing  — we expected one (we have charge_id) but QBO has none
      cc_trans_id_mismatch — money linked to wrong charge; flag for review
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, charge_id, status, attempted_at
          FROM billing.processing_attempts
         WHERE qbo_payment_id = %s
           AND dry_run = false
         ORDER BY attempted_at DESC
         LIMIT 1
    """, (qbo_payment_id,))
    attempt = cur.fetchone()

    if not attempt:
        cur.close()
        return {"outcome": "no_attempt", "expected": None,
                "actual": cc_trans_id_from_qbo, "attempt_id": None}

    attempt_dict = dict(attempt)
    expected = attempt_dict.get("charge_id")
    attempt_id = str(attempt_dict["id"])

    if not expected:
        cur.close()
        return {"outcome": "no_attempt", "expected": None,
                "actual": cc_trans_id_from_qbo, "attempt_id": attempt_id}

    if not cc_trans_id_from_qbo:
        msg = (f"verify_cc_trans_id: missing on QBO Payment {qbo_payment_id}; "
               f"expected charge_id={expected}.")
        print(f"  WARN  {msg}")
        cur.execute("""
            UPDATE billing.processing_attempts
               SET error_message = COALESCE(error_message, '')
                                   || CASE WHEN COALESCE(error_message, '') = '' THEN '' ELSE ' | ' END
                                   || %s
             WHERE id = %s
        """, (f"cc_trans_id_missing (expected {expected})", attempt_dict["id"]))
        conn.commit()
        cur.close()
        return {"outcome": "cc_trans_id_missing", "expected": expected,
                "actual": None, "attempt_id": attempt_id}

    if cc_trans_id_from_qbo != expected:
        msg = (f"verify_cc_trans_id: MISMATCH on Payment {qbo_payment_id}: "
               f"expected charge_id={expected}, got CCTransId={cc_trans_id_from_qbo}")
        print(f"  ERROR {msg}")
        cur.execute("""
            UPDATE billing.processing_attempts
               SET status = 'needs_reconcile_review',
                   error_message = COALESCE(error_message, '')
                                   || CASE WHEN COALESCE(error_message, '') = '' THEN '' ELSE ' | ' END
                                   || %s
             WHERE id = %s
        """, (f"cc_trans_id_mismatch (expected {expected}, got {cc_trans_id_from_qbo})",
              attempt_dict["id"]))
        conn.commit()
        cur.close()
        return {"outcome": "cc_trans_id_mismatch", "expected": expected,
                "actual": cc_trans_id_from_qbo, "attempt_id": attempt_id}

    cur.close()
    return {"outcome": "verified", "expected": expected,
            "actual": cc_trans_id_from_qbo, "attempt_id": attempt_id}


def main(qbo_payment_id: str, qbo_body: dict | None = None):
    """
    Args:
      qbo_payment_id: Required. QBO Id of the payment.
      qbo_body:       Optional. Pre-fetched QBO Payment body (e.g. from CDC).
                      When provided, skips the QBO GET.
    """
    if not qbo_payment_id:
        return {"status": "error", "error": "qbo_payment_id required"}

    print(f"=== refresh_payment {qbo_payment_id} (body_provided={qbo_body is not None}) ===")

    qbo_pmt = qbo_body
    if qbo_pmt is None:
        access_token, realm_id = refresh_qbo_token()
        resp = qbo_get(f"payment/{qbo_payment_id}", access_token, realm_id)

        # QBO's deleted-entity semantics: a deleted transaction reads back
        # as 400 + Fault "Object Not Found" (code 610), NOT 404. Both mean
        # the leader no longer has it. (Found 2026-07-14: the double-count
        # class survived because this check was 404-only.)
        deleted_in_qbo = resp.status_code == 404 or (
            resp.status_code == 400 and "Object Not Found" in (resp.text or ""))
        if deleted_in_qbo:
            # Deleted in QBO -> the mirror row goes too. The old behavior
            # (stamp sync_error, keep the row) left the dead payment's
            # unapplied_amt offerable as a credit and its application Lines
            # counting in balance derivations — the double-count class the
            # integrity probe surfaced 2026-07-14. The deletion event
            # survives in webhook_log/drift_log; our own ledgers are
            # processing_attempts / payment_invoice_links, not the cache.
            conn = get_db_conn()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT raw, ref_num, qbo_customer_id FROM billing.customer_payments "
                    "WHERE qbo_payment_id = %s", (qbo_payment_id,))
                prior = cur.fetchone()
                if prior:
                    # the void implicitly unapplies its lines: emit + fan out
                    fanned = _diff_applications(conn, qbo_payment_id, prior[0],
                                                None, prior[1], prior[2], "webhook")
                    emit(conn, "payment", qbo_payment_id, "payment_deleted",
                         participants=([f"customer:{prior[2]}"] if prior[2] else []),
                         payload={"ref": prior[1],
                                  "provenance": {"source": "external",
                                                 "discovered_via": "webhook"}},
                         actor="qbo_webhook")
                    print(f"  void: unapplied lines fanned out to {fanned}")
                cur.execute(
                    "DELETE FROM billing.customer_payments WHERE qbo_payment_id = %s",
                    (qbo_payment_id,))
                deleted = cur.rowcount
                # (no decision-table maintenance: undecided credits DERIVE from
                # customer_payments, so this delete unblocks gates by itself —
                # derived readiness v3)
                conn.commit()
                cur.close()
            finally:
                conn.close()
            return {"status": "deleted", "qbo_payment_id": qbo_payment_id,
                    "cache_row_removed": bool(deleted)}

        if not resp.ok:
            return {"status": "error",
                    "error": f"QBO fetch failed: {resp.status_code}",
                    "detail": resp.text[:200]}

        qbo_pmt = (resp.json() or {}).get("Payment")
        if not qbo_pmt:
            return {"status": "error", "error": "QBO returned no Payment"}

    cc_info = qbo_pmt.get("CreditCardPayment") or {}
    cc_trans_id = (cc_info.get("CreditChargeResponse") or {}).get("CCTransId")

    # Linked invoices need rechecks since this payment may have applied to them.
    linked_invoice_ids = []
    for line in qbo_pmt.get("Line") or []:
        for linked_txn in line.get("LinkedTxn") or []:
            if linked_txn.get("TxnType") == "Invoice":
                inv_id = linked_txn.get("TxnId")
                if inv_id and inv_id not in linked_invoice_ids:
                    linked_invoice_ids.append(inv_id)

    conn = get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT raw FROM billing.customer_payments WHERE qbo_payment_id = %s",
                    (qbo_payment_id,))
        prior = cur.fetchone()
        prior_raw = prior[0] if prior else None
        cur.close()

        qbo_payment_id, did_write, upserted = upsert_payment(conn, qbo_pmt)
        conn.commit()

        # ADR 010 phase 3: the application set-diff — emit the ledger facts
        # and fan the delta invoices back into the inbox for a fresh-read
        # (this closes the payment→invoice arc; Kathy Lindsay 2026-07-23).
        fanned_out = []
        if did_write:
            fanned_out = _diff_applications(
                conn, qbo_payment_id, prior_raw, qbo_pmt,
                qbo_pmt.get("PaymentRefNum"),
                (qbo_pmt.get("CustomerRef") or {}).get("value"),
                "cdc" if qbo_body is not None else "webhook")
            conn.commit()

        # No decision-table maintenance (derived readiness v3): "undecided"
        # derives live from customer_payments minus terminal decisions, so the
        # upsert above IS the maintenance — a consumed or externally-applied
        # credit drops out of the open set on its own. Decision rows are
        # append-only events, never touched here.

        # CC verify reads QBO body + our processing_attempts; safe regardless.
        verification = verify_cc_trans_id(conn, qbo_payment_id, cc_trans_id)

        # NO MANUAL RECHECK NEEDED.
        # The upsert_payment write to billing.customer_payments fires the
        # fn_set_credits_ok_from_payment trigger automatically, which fans
        # out to every linked invoice for the affected customer, recomputes
        # credits_ok, and (via the projection trigger) updates billing_status
        # in-place. This used to be a manual loop calling recheck_invoice_status
        # — now the database handles it inside the same transaction as the
        # cache write. See migrations 20260508000003..7.
        recheck_results: list[dict] = []  # kept for return-shape compat

        conn.commit()

        if not did_write:
            print(f"  upsert no-op (OCC blocked — newer state already in cache)")

        return {
            "status":                    "ok",
            "qbo_payment_id":            qbo_payment_id,
            "qbo_customer_id":           (qbo_pmt.get("CustomerRef") or {}).get("value"),
            "total_amt":                 float(qbo_pmt.get("TotalAmt") or 0),
            "unapplied_amt":             float(qbo_pmt.get("UnappliedAmt") or 0),
            "did_write":                 did_write,
            "linked_invoices_rechecked": recheck_results,
            "application_fanout":        fanned_out,
            "verification":              verification,
            "payment_id":                str(upserted["id"]) if upserted else None,
        }
    finally:
        conn.close()
