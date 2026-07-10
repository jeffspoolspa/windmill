# requirements:
# psycopg2-binary
# requests
# wmill

# f/service_billing/process_invoice
#
# Charges cards / sends invoices for invoices in billing_status='ready_to_process'.
# The service-billing EVENT HANDLER (ADR 009 / LIBRARY_COMPOSITION): every
# external verb is imported from f/billing/_lib; the idempotent charge core
# (WAL + fresh-read + charge + QBO Payment + receipt) is the shared
# charge_and_record service. This file keeps only THIS workflow's policy —
# route decision, pre-flight gates, credit halts, delivery, cache echo.
#
# State machine on billing.processing_attempts.status (stage='process'):
#   pending           -> row created, no external calls yet
#   charge_uncertain  -> charge call returned 5xx/timeout, money state unknown.
#                        Retry reuses idempotency_key (Intuit dedupes).
#   charge_declined   -> definitive failure, no money moved. Terminal.
#                        (Also written by pre-charge halts: credits_available,
#                        no_payment_method — distinguished by charge_id IS NULL.)
#   charge_succeeded  -> charge_id received, record_payment not done yet.
#                        Retry skips charge step, retries only record_payment.
#   payment_orphan    -> charge succeeded but record_payment failed. HUMAN ONLY.
#                        Recover via recover_orphan=True after manual verification.
#   email_failed      -> money state ok, only email failed. Auto-retry email up to 3x.
#   succeeded         -> charge + QBO Payment done (emails tracked on the row).
#
# CRITICAL: idempotency_key is generated ONCE per attempt, persisted BEFORE the
# charge call, and reused on every retry — all inside charge_and_record now.

import time
import psycopg2.extras

from f.billing._lib.db import get_db_conn
from f.billing._lib.qbo import (
    refresh_qbo_token, fetch_qbo_invoice, fetch_qbo_customer_email,
    send_invoice_email, send_payment_receipt, bump_invoice_due_date_to_today,
    record_qbo_payment,
)
from f.billing._lib.wal import (
    latest_attempt, create_attempt, update_attempt,
    insert_webhook_expectation, dumps as _dumps,
)
from f.billing._lib.payments import (
    charge_and_record, resolve_payment_method, load_applicable_credits,
)

STAGE = "process"

# Email retry policy for preferred_payment_type='email' send-only path
EMAIL_RETRY_MAX = 3
EMAIL_RETRY_BACKOFF_S = 5


# =============================================================================
# ENGINE READS + STATUS WRITES (this workflow's policy queries)
# =============================================================================

def load_invoice(conn, qbo_invoice_id):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM billing.invoices WHERE qbo_invoice_id = %s", (qbo_invoice_id,))
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None


def load_linked_wo(conn, qbo_invoice_id):
    """Loads the WO matched to this invoice. wo_number is NOT NULL on processing_attempts."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM public.work_orders WHERE qbo_invoice_id = %s LIMIT 1",
                (qbo_invoice_id,))
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None


def mark_invoice_processed(conn, qbo_invoice_id):
    # Stamped status — Phase 3 (runbooks/service-billing-cleanup.md) derives
    # this from the attempt log instead.
    cur = conn.cursor()
    cur.execute("""
        UPDATE billing.invoices
        SET billing_status = 'processed', processed_at = now()
        WHERE qbo_invoice_id = %s
    """, (qbo_invoice_id,))
    conn.commit(); cur.close()


def mark_invoice_needs_review(conn, qbo_invoice_id, reason):
    """Flip the invoice back to needs_review so it surfaces in the queue.
    Called from every halt-state transition. Skips terminal 'processed'
    invoices defensively (unwinding a successful payment would be wrong)."""
    cur = conn.cursor()
    cur.execute("""
        UPDATE billing.invoices
        SET billing_status = 'needs_review',
            needs_review_reason = %s
        WHERE qbo_invoice_id = %s
          AND billing_status != 'processed'
    """, (reason, qbo_invoice_id))
    conn.commit(); cur.close()


def refresh_invoice_cache(conn, qbo_invoice_id, qbo_invoice):
    """Verified echo (ADR 009 §C): write the state QBO actually reports."""
    def _subtotal(inv):
        for line in inv.get("Line", []) or []:
            if line.get("DetailType") == "SubTotalLineDetail":
                try:
                    return round(float(line.get("Amount", 0) or 0), 2)
                except (TypeError, ValueError):
                    pass
        total = float(inv.get("TotalAmt", 0) or 0)
        tax = float((inv.get("TxnTaxDetail") or {}).get("TotalTax", 0) or 0)
        return round(total - tax, 2)

    cur = conn.cursor()
    cur.execute("""
        UPDATE billing.invoices
        SET subtotal = %s, balance = %s, total_amt = %s,
            email_status = %s, raw = %s::jsonb, fetched_at = now()
        WHERE qbo_invoice_id = %s
    """, (
        _subtotal(qbo_invoice),
        float(qbo_invoice.get("Balance", 0) or 0),
        float(qbo_invoice.get("TotalAmt", 0) or 0),
        qbo_invoice.get("EmailStatus"),
        _dumps(qbo_invoice),
        qbo_invoice_id,
    ))
    conn.commit(); cur.close()


def _refresh_cache_fresh(conn, qbo_invoice_id, access_token, realm_id):
    fresh, _ = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
    if fresh:
        refresh_invoice_cache(conn, qbo_invoice_id, fresh)


# =============================================================================
# CORE PROCESSING — the event handler
# =============================================================================

def _result(qbo_invoice_id, status, **rest):
    return {"qbo_invoice_id": qbo_invoice_id, "status": status, **rest}


def _route(invoice):
    """The route decision (charge vs email) lives on preferred_payment_type:
    'email' -> email path, 'ach'/'credit_card' -> charge path. Falls back to
    the legacy dual-written payment_method for never-re-pre-processed
    invoices; None means neither is usable (re-run pre_process_invoice)."""
    preferred_type = invoice.get("preferred_payment_type")
    if preferred_type in ("email", "ach", "credit_card"):
        return preferred_type
    legacy = invoice.get("payment_method")
    if legacy == "invoice":
        return "email"
    if legacy == "on_file":
        return "credit_card"  # pessimistic default — the PM picker refines
    return None


def build_intent(invoice, wo_number, pm, receipt_email):
    """Policy as data (ADR 009 §B): the service reads the balance fresh —
    no amount is passed. Memo/ref carry the WO framing; receipt_email None
    would mean no receipt (service is segment-blind either way)."""
    invoice_number = invoice.get("doc_number")
    return {
        "stage": STAGE,
        "qbo_invoice_id": invoice["qbo_invoice_id"],
        "lines": [invoice["qbo_invoice_id"]],
        "payment_method_id": pm["method_id"],
        "cpm_id": pm["cpm_id"],
        "channel": "card" if pm["payment_type"] in ("credit_card", "card") else "ach",
        "customer_id": invoice.get("qbo_customer_id"),
        "customer_name": invoice.get("customer_name") or "",
        "invoice_number": invoice_number,
        "wo_number": wo_number,
        "payment_method": invoice.get("payment_method"),  # legacy dual-write
        "payment_ref": wo_number,
        "memo_prefix": f"Auto-charge | WO# {wo_number} | Inv# {invoice_number}",
        "receipt_email": receipt_email,
    }


def deliver(conn, qbo_invoice_id, customer_id, access_token, realm_id):
    """Post-charge delivery policy: the customer gets a paid invoice copy
    alongside the receipt (complementary documents). send_invoice_email is
    idempotent — QBO EmailStatus=EmailSent makes it a skip."""
    inv_email = send_invoice_email(qbo_invoice_id, customer_id, access_token, realm_id)
    if inv_email.get("success") and not inv_email.get("skipped"):
        # QBO fires Invoice.Emailed only when an actual send happens.
        insert_webhook_expectation(conn, "Invoice", qbo_invoice_id)
    return inv_email


def process_one(conn, qbo_invoice_id, access_token, realm_id,
                dry_run=False, recover_orphan=False, force=False):
    """Main per-invoice flow. Returns dict with status + diagnostics."""
    invoice = load_invoice(conn, qbo_invoice_id)
    if not invoice:
        return _result(qbo_invoice_id, "error", error="invoice not found in billing.invoices")

    wo = load_linked_wo(conn, qbo_invoice_id)
    if not wo:
        return _result(qbo_invoice_id, "error", error="no linked work order — cannot process")
    wo_number = wo["wo_number"]
    invoice_number = invoice.get("doc_number")
    customer_id = invoice.get("qbo_customer_id")

    route = _route(invoice)
    if route is None:
        # Pre-flight error: write a stub attempt so the batch progress modal
        # sees this invoice was touched, and surface it in needs_review.
        err = (f"invalid preferred_payment_type "
               f"'{invoice.get('preferred_payment_type')}' and no legacy "
               f"payment_method to fall back to (re-run pre_process_invoice)")
        attempt = create_attempt(conn, qbo_invoice_id, STAGE, invoice_number,
                                 "email", 0, dry_run, wo_number=wo_number,
                                 status="error")
        update_attempt(conn, attempt["id"], error_message=err[:500])
        mark_invoice_needs_review(conn, qbo_invoice_id, err[:200])
        return _result(qbo_invoice_id, "error", attempt_id=str(attempt["id"]), error=err)

    if invoice.get("billing_status") != "ready_to_process" and not (force or recover_orphan):
        return _result(qbo_invoice_id, "skipped",
                       reason=f"billing_status='{invoice.get('billing_status')}' "
                              f"(need ready_to_process or force=True)")

    # 1. PRE-FLIGHT: prior-attempt policy gates
    prior = latest_attempt(conn, qbo_invoice_id, STAGE)

    # Recover-orphan path: explicit human action, requires prior payment_orphan.
    if recover_orphan:
        if not prior or prior["status"] != "payment_orphan":
            return _result(qbo_invoice_id, "error",
                           error=f"recover_orphan called but no payment_orphan attempt found "
                                 f"(prior status: {prior['status'] if prior else 'none'})")
        return _recover_orphan(conn, prior, invoice, wo_number, access_token, realm_id)

    # Already done — but with force, allow a new attempt for a remaining
    # open balance (the "charge balance" recovery flow on the WO page).
    if prior and prior["status"] == "succeeded":
        qbo_inv_chk, _err = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
        if qbo_inv_chk:
            refresh_invoice_cache(conn, qbo_invoice_id, qbo_inv_chk)
            chk_balance = float(qbo_inv_chk.get("Balance", 0) or 0)
            if chk_balance == 0:
                mark_invoice_processed(conn, qbo_invoice_id)
                return _result(qbo_invoice_id, "already_succeeded",
                               attempt_id=str(prior["id"]))
            if not force:
                return _result(qbo_invoice_id, "already_succeeded",
                               attempt_id=str(prior["id"]),
                               note=f"prior succeeded; open balance ${chk_balance:.2f} "
                                    f"— pass force=true to create a recovery attempt")
            # force: fall through — the service fresh-reads and charges the remainder
        elif not force:
            return _result(qbo_invoice_id, "already_succeeded",
                           attempt_id=str(prior["id"]),
                           note="prior succeeded but QBO state could not be verified")

    if prior and prior["status"] == "payment_orphan":
        mark_invoice_needs_review(
            conn, qbo_invoice_id,
            f"payment_orphan (charged ${float(prior['charge_amount'] or 0):.2f}, "
            f"ledger write failed; verify in QBO + Intuit before retrying)",
        )
        return _result(qbo_invoice_id, "needs_human", reason="payment_orphan",
                       charge_id=prior["charge_id"],
                       amount=float(prior["charge_amount"] or 0),
                       attempt_id=str(prior["id"]))

    if prior and prior["status"] == "charge_declined" and not force:
        # Real card declines carry a charge_id (Intuit assigns one even on
        # decline); pre-charge halts (credits_available, no_payment_method)
        # never call Intuit so charge_id stays NULL and retry freely. A real
        # decline only halts a retry of the SAME (channel, PM) path.
        if bool(prior.get("charge_id")):
            new_pm = invoice.get("target_payment_method_id")
            same_attempt_path = (
                prior.get("channel") == route
                and (str(prior["customer_payment_method_id"])
                     if prior.get("customer_payment_method_id") else None)
                == (str(new_pm) if new_pm else None)
                and route != "email"
            )
            if same_attempt_path:
                mark_invoice_needs_review(
                    conn, qbo_invoice_id,
                    f"charge_declined ({(prior.get('error_message') or 'declined')[:120]})",
                )
                return _result(qbo_invoice_id, "needs_human", reason="charge_declined",
                               error=prior.get("error_message"),
                               attempt_id=str(prior["id"]),
                               note="prior attempt declined this same PM; "
                                    "change channel/PM or pass force=true to retry")

    if prior and prior["status"] == "needs_reconcile_review" and not force:
        mark_invoice_needs_review(
            conn, qbo_invoice_id,
            f"needs_reconcile_review ({(prior.get('error_message') or 'reconciler could not determine state')[:120]})",
        )
        return _result(qbo_invoice_id, "needs_human", reason="needs_reconcile_review",
                       error=prior.get("error_message"),
                       attempt_id=str(prior["id"]))

    # 2. Refresh QBO state — may have been paid/sent externally
    qbo_inv, err = fetch_qbo_invoice(qbo_invoice_id, access_token, realm_id)
    if not qbo_inv:
        return _result(qbo_invoice_id, "error", error=f"qbo_fetch_failed: {err}")
    refresh_invoice_cache(conn, qbo_invoice_id, qbo_inv)

    qbo_balance = float(qbo_inv.get("Balance", 0) or 0)
    qbo_email_sent = qbo_inv.get("EmailStatus") == "EmailSent"

    if qbo_balance == 0 and qbo_email_sent:
        mark_invoice_processed(conn, qbo_invoice_id)
        return _result(qbo_invoice_id, "already_paid_and_sent")

    # 3. DRY-RUN: sandbox plan on its OWN dry_run=true attempt row. (The old
    # engine reused a live pending row here and stamped it succeeded — a
    # dry-run must never touch live WAL state.)
    if dry_run:
        attempt = create_attempt(conn, qbo_invoice_id, STAGE, invoice_number,
                                 route if route != "email" else "email",
                                 qbo_balance, True, wo_number=wo_number,
                                 payment_method=invoice.get("payment_method"),
                                 cpm_id=(str(invoice["target_payment_method_id"])
                                         if invoice.get("target_payment_method_id") else None))
        plan = _build_dry_run_plan(conn, invoice, route, qbo_balance, qbo_email_sent,
                                   customer_id, attempt)
        update_attempt(conn, attempt["id"], status="succeeded", raw_result=_dumps(plan))
        return _result(qbo_invoice_id, "dry_run_complete",
                       attempt_id=str(attempt["id"]), plan=plan)

    # 4. ROUTE
    if route == "email":
        return _process_email_only(conn, prior, invoice, wo_number, customer_id,
                                   access_token, realm_id)
    return _process_charge_path(conn, prior, invoice, wo_number, route,
                                qbo_balance, access_token, realm_id)


def _build_dry_run_plan(conn, invoice, route, balance, email_already_sent,
                        customer_id, attempt):
    """Predicts what a live run would do without external calls — mirrors the
    live halts (credits, missing PM) so the plan is honest."""
    is_charge = route in ("ach", "credit_card")
    plan = {
        "payment_method": invoice.get("payment_method"),
        "preferred_payment_type": route,
        "amount_to_charge": balance if is_charge and balance > 0 else 0,
        "would_send_invoice_email": not email_already_sent,
        "would_send_receipt": is_charge and balance > 0,
        "idempotency_key": attempt["idempotency_key"],
    }
    if is_charge and balance > 0:
        remaining_credits = load_applicable_credits(conn, customer_id)
        if remaining_credits:
            plan["would_halt"] = "credits_available"
            plan["credits_found"] = [
                {"qbo_payment_id": c.get("qbo_payment_id"),
                 "unapplied_amt": float(c.get("unapplied_amt") or 0),
                 "txn_date": str(c.get("txn_date")) if c.get("txn_date") else None,
                 "memo": c.get("memo")}
                for c in remaining_credits
            ]
            plan["credits_total_unapplied"] = sum(
                float(c.get("unapplied_amt") or 0) for c in remaining_credits)
        target_pm_id = invoice.get("target_payment_method_id")
        pm = resolve_payment_method(conn, customer_id,
                                    preferred_type=route,
                                    cpm_id=str(target_pm_id) if target_pm_id else None)
        plan["payment_method_on_file"] = pm
        if not pm.get("has_method"):
            plan["would_fail"] = pm.get("error") or "no_payment_method"
    return plan


def _halt_attempt(conn, prior, invoice, wo_number, channel, amount, reason, extra=None):
    """Record a pre-charge halt on the WAL (status charge_declined, charge_id
    NULL = never reached Intuit). Reuses a prior pending row instead of
    stranding it."""
    if prior and prior["status"] == "pending":
        attempt = prior
    else:
        attempt = create_attempt(conn, invoice["qbo_invoice_id"], STAGE,
                                 invoice.get("doc_number"), channel, amount, False,
                                 wo_number=wo_number,
                                 payment_method=invoice.get("payment_method"))
    update_attempt(conn, attempt["id"], status="charge_declined",
                   error_message=reason,
                   charge_result=_dumps(extra) if extra else None)
    return attempt


def _send_only(conn, prior, invoice, wo_number, channel, balance, note,
               access_token, realm_id):
    """No-charge outcome (zero balance / paid upstream): invoice copy only,
    then done. Reuses a prior pending WAL row rather than stranding it."""
    qbo_invoice_id = invoice["qbo_invoice_id"]
    email = send_invoice_email(qbo_invoice_id, invoice.get("qbo_customer_id"),
                               access_token, realm_id)
    if prior and prior["status"] == "pending":
        attempt = prior
    else:
        attempt = create_attempt(conn, qbo_invoice_id, STAGE, invoice.get("doc_number"),
                                 channel, balance, False, wo_number=wo_number,
                                 payment_method=invoice.get("payment_method"))
    if not email["success"] and not email.get("skipped"):
        update_attempt(conn, attempt["id"], status="email_failed",
                       email_sent=False, error_message=email.get("error"),
                       raw_result=_dumps({"email": email,
                                          "skipped_charge_zero_balance": True}))
        return _result(qbo_invoice_id, "email_failed",
                       attempt_id=str(attempt["id"]), error=email.get("error"))
    update_attempt(conn, attempt["id"], status="succeeded",
                   email_sent=email["success"],
                   raw_result=_dumps({"email": email,
                                      "skipped_charge_zero_balance": True}))
    mark_invoice_processed(conn, qbo_invoice_id)
    return _result(qbo_invoice_id, "succeeded",
                   attempt_id=str(attempt["id"]), note=note)


def _process_charge_path(conn, prior, invoice, wo_number, route, balance,
                         access_token, realm_id):
    qbo_invoice_id = invoice["qbo_invoice_id"]
    customer_id = invoice.get("qbo_customer_id")

    # Zero balance (covered by credits in pre_process): no charge needed.
    if balance == 0:
        return _send_only(conn, prior, invoice, wo_number, route, 0,
                          "balance was zero — sent invoice only",
                          access_token, realm_id)

    # Credit re-check — catches credits that landed between pre_process and
    # process. Override semantics: credits visible at review time (txn_date
    # on/before credit_review_overridden_at) are waved through; NEWER credits
    # still halt.
    remaining_credits = load_applicable_credits(conn, customer_id)
    override_at = invoice.get("credit_review_overridden_at")
    if remaining_credits and override_at is not None:
        cutoff = override_at.date() if hasattr(override_at, "date") else override_at
        kept = [c for c in remaining_credits
                if c.get("txn_date") is None or c.get("txn_date") > cutoff]
        if len(kept) < len(remaining_credits):
            print(f"  credit override active (overridden_at={override_at.isoformat()}): "
                  f"ignored {len(remaining_credits) - len(kept)} pre-override credit(s); "
                  f"{len(kept)} new credit(s) remaining")
        remaining_credits = kept
    if remaining_credits:
        total_unapplied = sum(float(c.get("unapplied_amt") or 0) for c in remaining_credits)
        reason = f"credits_available ({len(remaining_credits)} credit(s), ${total_unapplied:.2f} unapplied)"
        attempt = _halt_attempt(conn, prior, invoice, wo_number, route, balance,
                                reason, extra={"credits_found": remaining_credits})
        mark_invoice_needs_review(conn, qbo_invoice_id, reason)
        return _result(qbo_invoice_id, "needs_human", reason="credits_available",
                       attempt_id=str(attempt["id"]), error=reason,
                       credits_found=len(remaining_credits),
                       total_unapplied=total_unapplied)

    # The payment method pre_process picked (stable across the UI session);
    # legacy invoices without a target fall back to the default picker.
    target_pm_id = invoice.get("target_payment_method_id")
    pm = resolve_payment_method(conn, customer_id,
                                preferred_type=invoice.get("preferred_payment_type"),
                                cpm_id=str(target_pm_id) if target_pm_id else None)
    if not pm.get("has_method"):
        attempt = _halt_attempt(conn, prior, invoice, wo_number, route, balance,
                                pm.get("error", "no payment method"), extra=pm)
        mark_invoice_needs_review(
            conn, qbo_invoice_id,
            f"no_payment_method ({pm.get('error', 'no PM on file')[:120]})",
        )
        return _result(qbo_invoice_id, "needs_human", reason="no_payment_method",
                       attempt_id=str(attempt["id"]), error=pm.get("error"))

    # THE CHARGE — one call to the shared service (WAL + fresh-read + charge +
    # payment + receipt live there; see f/billing/_lib/payments).
    receipt_email = fetch_qbo_customer_email(customer_id, access_token, realm_id)
    r = charge_and_record(conn, build_intent(invoice, wo_number, pm, receipt_email),
                          access_token, realm_id)

    # Dispatch on the outcome — THIS workflow's policy.
    if r["status"] == "read_failed":
        return _result(qbo_invoice_id, "error", error=f"qbo_fetch_failed: {r['error']}")

    if r["status"] == "already_paid":
        # Paid between our step-2 read and the service's fresh read.
        out = _send_only(conn, prior, invoice, wo_number, route, 0,
                         "balance reached 0 before the charge fired (paid upstream)",
                         access_token, realm_id)
        _refresh_cache_fresh(conn, qbo_invoice_id, access_token, realm_id)
        return out

    if r["status"] == "uncertain":
        return _result(qbo_invoice_id, "uncertain",
                       attempt_id=r["attempt_id"], error=r["error"],
                       note="charge state unknown — reconcile_payments will resolve, "
                            "or retry safely (idempotency_key reused)")

    if r["status"] == "declined":
        mark_invoice_needs_review(
            conn, qbo_invoice_id,
            f"charge_declined ({(r.get('error') or 'declined')[:120]})",
        )
        return _result(qbo_invoice_id, "needs_human", reason="charge_declined",
                       attempt_id=r["attempt_id"], error=r["error"])

    if r["status"] == "payment_orphan":
        mark_invoice_needs_review(
            conn, qbo_invoice_id,
            f"payment_orphan (charged ${float(r.get('amount') or 0):.2f}, "
            f"ledger write failed; verify in QBO + Intuit before retrying)",
        )
        return _result(qbo_invoice_id, "needs_human", reason="payment_orphan",
                       attempt_id=r["attempt_id"], charge_id=r["charge_id"],
                       amount=r["amount"], error=r["error"])

    # succeeded — deliver + verified-echo cache + done
    inv_email = deliver(conn, qbo_invoice_id, customer_id, access_token, realm_id)
    update_attempt(conn, r["attempt_id"], email_sent=r["receipt_sent"])
    _refresh_cache_fresh(conn, qbo_invoice_id, access_token, realm_id)
    mark_invoice_processed(conn, qbo_invoice_id)
    return _result(qbo_invoice_id, "succeeded",
                   attempt_id=r["attempt_id"], charge_id=r["charge_id"],
                   qbo_payment_id=r["payment_id"],
                   receipt_sent=r["receipt_sent"],
                   invoice_email_sent=inv_email["success"],
                   invoice_email_skipped=inv_email.get("skipped", False),
                   resumed=r.get("resumed"))


def _recover_orphan(conn, prior, invoice, wo_number, access_token, realm_id):
    """Human-verified orphan recovery: retry ONLY record_payment with the
    persisted charge_id (never charges again). An engine sentence over shared
    primitives — charge_and_record refuses orphans by design because a blind
    record retry can double-record."""
    import json
    qbo_invoice_id = invoice["qbo_invoice_id"]
    customer_id = invoice.get("qbo_customer_id")
    invoice_number = invoice.get("doc_number")

    charge_result = prior.get("charge_result") or {}
    if isinstance(charge_result, str):
        charge_result = json.loads(charge_result)
    charge_id = prior.get("charge_id") or charge_result.get("charge_id")
    if not charge_id:
        return _result(qbo_invoice_id, "error",
                       error="orphan recovery requested but no charge_id on prior attempt",
                       attempt_id=str(prior["id"]))
    charge_result.setdefault("charge_id", charge_id)

    amount = float(prior["charge_amount"] or 0)
    pay = record_qbo_payment(customer_id, amount, charge_result, wo_number,
                             f"Auto-charge | WO# {wo_number} | Inv# {invoice_number}",
                             access_token, realm_id, [(qbo_invoice_id, amount)])
    if not pay["success"]:
        update_attempt(conn, prior["id"], status="payment_orphan",
                       error_message=f"orphan recovery: record_payment still failing: "
                                     f"{str(pay.get('error', ''))[:300]}")
        mark_invoice_needs_review(
            conn, qbo_invoice_id,
            f"payment_orphan (charged ${amount:.2f}, ledger retry still failing)",
        )
        return _result(qbo_invoice_id, "needs_human", reason="payment_orphan",
                       attempt_id=str(prior["id"]), charge_id=charge_id,
                       amount=amount, error=pay.get("error"),
                       note="record_payment retry failed — verify in QBO/Intuit")

    update_attempt(conn, prior["id"], qbo_payment_id=pay["payment_id"])
    insert_webhook_expectation(conn, "Payment", pay["payment_id"])

    inv_email = deliver(conn, qbo_invoice_id, customer_id, access_token, realm_id)
    receipt = send_payment_receipt(pay["payment_id"], customer_id, access_token, realm_id)
    update_attempt(conn, prior["id"], email_sent=receipt["success"])

    _refresh_cache_fresh(conn, qbo_invoice_id, access_token, realm_id)
    update_attempt(conn, prior["id"], status="succeeded",
                   raw_result=_dumps({"orphan_recovery": True, "payment": pay,
                                      "receipt": receipt, "invoice_email": inv_email}))
    mark_invoice_processed(conn, qbo_invoice_id)
    return _result(qbo_invoice_id, "succeeded",
                   attempt_id=str(prior["id"]), charge_id=charge_id,
                   qbo_payment_id=pay["payment_id"],
                   recovered_from="payment_orphan",
                   invoice_email_sent=inv_email["success"],
                   invoice_email_skipped=inv_email.get("skipped", False))


def _process_email_only(conn, prior, invoice, wo_number, customer_id,
                        access_token, realm_id):
    """preferred_payment_type='email' — the email IS the deliverable.
    Auto-retry up to EMAIL_RETRY_MAX."""
    qbo_invoice_id = invoice["qbo_invoice_id"]

    # Reuse a prior pending row (crash between create and send), else create.
    if prior and prior["status"] == "pending":
        attempt = prior
    else:
        attempt = create_attempt(conn, qbo_invoice_id, STAGE, invoice.get("doc_number"),
                                 "email", float(invoice.get("balance") or 0), False,
                                 wo_number=wo_number,
                                 payment_method=invoice.get("payment_method"))

    # Bump DueDate so a long-parked invoice doesn't arrive showing OVERDUE.
    # Best-effort — a failed PATCH never blocks the deliverable.
    due_update = bump_invoice_due_date_to_today(qbo_invoice_id, access_token, realm_id)
    if due_update.get("success") and not due_update.get("skipped"):
        insert_webhook_expectation(conn, "Invoice", qbo_invoice_id)
        print(f"  bumped DueDate {due_update.get('old_due_date')} → {due_update.get('new_due_date')}")
    elif due_update.get("skipped"):
        print(f"  DueDate already current ({due_update.get('current_due_date')}), no PATCH")
    else:
        print(f"  DueDate bump failed (continuing with email): {due_update.get('error')}")

    last_err = None
    for i in range(EMAIL_RETRY_MAX):
        email = send_invoice_email(qbo_invoice_id, customer_id, access_token, realm_id)
        if email["success"]:
            update_attempt(conn, attempt["id"], status="succeeded", email_sent=True,
                           raw_result=_dumps({"email": email, "attempts": i + 1}))
            if not email.get("skipped"):
                insert_webhook_expectation(conn, "Invoice", qbo_invoice_id)
            mark_invoice_processed(conn, qbo_invoice_id)
            _refresh_cache_fresh(conn, qbo_invoice_id, access_token, realm_id)
            return _result(qbo_invoice_id, "succeeded",
                           attempt_id=str(attempt["id"]),
                           sent_to=email.get("sent_to"),
                           skipped=email.get("skipped", False))
        last_err = email.get("error")
        if i + 1 < EMAIL_RETRY_MAX:
            time.sleep(EMAIL_RETRY_BACKOFF_S)

    update_attempt(conn, attempt["id"], status="email_failed",
                   error_message=last_err,
                   raw_result=_dumps({"attempts": EMAIL_RETRY_MAX, "last_error": last_err}))
    mark_invoice_needs_review(
        conn, qbo_invoice_id,
        f"email_failed (after {EMAIL_RETRY_MAX} retries: {(last_err or 'unknown')[:120]})",
    )
    return _result(qbo_invoice_id, "email_failed",
                   attempt_id=str(attempt["id"]), error=last_err)


# =============================================================================
# MAIN
# =============================================================================

def main(qbo_invoice_id: str = None,
         qbo_invoice_ids: list = None,
         dry_run: bool = False,
         recover_orphan: bool = False,
         force: bool = False,
         bulk_all: bool = False,
         limit: int = None,
         sleep_ms: int = 800):
    """
    Modes:
      - Single: pass qbo_invoice_id
      - List: pass qbo_invoice_ids=[...]  (used by Process Selected button)
      - Bulk-all: pass bulk_all=True (processes everything in ready_to_process)

    Flags:
      - dry_run=True: log what would happen, NO external API calls. Writes attempt row with dry_run=true.
      - recover_orphan=True: requires qbo_invoice_id + prior status='payment_orphan'. Retries record_payment with persisted charge_id.
      - force=True: bypass billing_status='ready_to_process' guard (e.g. retry charge_declined invoices)
    """
    if not qbo_invoice_id and not qbo_invoice_ids and not bulk_all:
        return {"status": "error", "error": "pass qbo_invoice_id, qbo_invoice_ids=[...], or bulk_all=True"}

    print(f"=== process_invoice (dry_run={dry_run}, recover_orphan={recover_orphan}, "
          f"force={force}, bulk_all={bulk_all}) ===")

    conn = get_db_conn()
    try:
        access_token, realm_id = refresh_qbo_token()

        if qbo_invoice_id and not qbo_invoice_ids:
            return process_one(conn, qbo_invoice_id, access_token, realm_id,
                               dry_run=dry_run, recover_orphan=recover_orphan, force=force)

        if qbo_invoice_ids:
            targets = list(qbo_invoice_ids)
        else:  # bulk_all
            cur = conn.cursor()
            sql = ("SELECT qbo_invoice_id FROM billing.invoices "
                   "WHERE billing_status = 'ready_to_process' "
                   "ORDER BY txn_date DESC NULLS LAST")
            if limit:
                sql += f" LIMIT {int(limit)}"
            cur.execute(sql)
            targets = [r[0] for r in cur.fetchall()]
            cur.close()

        print(f"Processing {len(targets)} invoice(s)")
        stats = {"succeeded": 0, "needs_human": 0, "uncertain": 0, "email_failed": 0,
                 "already_succeeded": 0, "already_paid_and_sent": 0,
                 "skipped": 0, "error": 0, "dry_run_complete": 0}
        sample = []

        for i, qid in enumerate(targets):
            try:
                res = process_one(conn, qid, access_token, realm_id,
                                  dry_run=dry_run, recover_orphan=recover_orphan, force=force)
            except Exception as e:
                res = _result(qid, "error", error=str(e)[:300])

            status = res.get("status", "error")
            stats[status] = stats.get(status, 0) + 1
            if i < 20:
                sample.append(res)
            print(f"  [{i+1}/{len(targets)}] {qid} -> {status}"
                  + (f"  ({res.get('reason') or res.get('error') or ''})"
                     if status not in ('succeeded', 'dry_run_complete') else ''))
            if sleep_ms and i + 1 < len(targets):
                time.sleep(sleep_ms / 1000.0)

        print(f"=== done: {stats} ===")
        return {"status": "success", "total": len(targets), "stats": stats, "sample": sample,
                "dry_run": dry_run}
    finally:
        conn.close()
