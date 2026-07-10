# requirements:
# psycopg2-binary
# requests
# wmill

# f/billing/process_maint_period — the maintenance-billing event handler.
#
# Charge / send maintenance billing periods in processing_status =
# 'ready_to_process'. Every external verb imports from f/billing/_lib; the
# idempotent charge core (WAL stage='maint' + fresh-read + charge + QBO
# Payment + receipt) is the shared charge_and_record service. This file keeps
# only maintenance policy: the ready/locked gates, the autopay roster + PM
# fallback, channel decision, group membership, decline-still-delivers,
# verified-echo cache writes, roster health, the processing projection.
#
# Module: docs/flows/monthly-maintenance-billing/index.md
# Status: [active]
# Concurrency key: qbo_writer (concurrent_limit 1 — money movement serializes)
# Triggered by: /api/maintenance-billing/process (dry_run first) + manual.

import psycopg2.extras
from datetime import datetime

from f.billing._lib.db import get_db_conn
from f.billing._lib.qbo import refresh_qbo_token, get_qbo_invoice_details, send_invoice
from f.billing._lib.wal import (
    latest_attempt, create_attempt, update_attempt, dumps as _dumps,
)
from f.billing._lib.payments import charge_and_record, stored_group_lines
from f.billing._lib.cache import echo_balance, mark_emailed

STAGE = "maint"


# ── engine reads + writes ────────────────────────────────────────────────────

def _rowd(conn, sql, params):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    row = cur.fetchone(); cur.close()
    return dict(row) if row else None


def _exec(conn, sql, params):
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit(); cur.close()


LOAD_PERIOD = """
SELECT tbp.id, tbp.billing_month, tbp.qbo_customer_id, tbp.qbo_invoice_id,
       tbp.processing_status, tbp.locked_at,
       to_char(tbp.billing_month, 'YYYY-MM') AS month_key,
       c.display_name AS customer_name, c.email AS customer_email,
       i.doc_number, i.balance, i.email_status,
       ac.id AS autopay_id, ac.email AS autopay_email,
       pm.id AS cpm_id, pm.qbo_payment_method_id, pm.type AS pm_type,
       pm.card_brand AS pm_brand, pm.last_four AS pm_last4,
       pm.is_active AS pm_active, pm.auto_disabled_at, pm.deactivated_at,
       dpm.id AS dpm_id, dpm.qbo_payment_method_id AS dpm_qbo_id,
       dpm.type AS dpm_type, dpm.card_brand AS dpm_brand, dpm.last_four AS dpm_last4
FROM billing_audit.task_billing_periods tbp
LEFT JOIN public."Customers" c ON c.qbo_customer_id = tbp.qbo_customer_id
LEFT JOIN billing.invoices i ON i.qbo_invoice_id = tbp.qbo_invoice_id
LEFT JOIN billing.autopay_customers ac ON ac.id = tbp.autopay_customer_id AND ac.is_active
LEFT JOIN billing.customer_payment_methods pm ON pm.id = ac.payment_method_id
LEFT JOIN LATERAL (
  SELECT pm2.id, pm2.qbo_payment_method_id, pm2.type, pm2.card_brand, pm2.last_four
  FROM billing.customer_payment_methods pm2
  WHERE pm2.qbo_customer_id = tbp.qbo_customer_id
    AND pm2.is_active AND pm2.auto_disabled_at IS NULL AND pm2.deactivated_at IS NULL
  ORDER BY pm2.is_default DESC, pm2.fetched_at DESC
  LIMIT 1
) dpm ON true
WHERE tbp.id = %s
"""


def _load_period(conn, period_id):
    p = _rowd(conn, LOAD_PERIOD, (period_id,))
    if p:
        p["email"] = p.get("autopay_email") or p.get("customer_email")
    return p


def _mark_period_processed(conn, period_id):
    # invoice delivered = the month's processing is done (projection never
    # demotes processed)
    _exec(conn, """UPDATE billing_audit.task_billing_periods
                   SET processing_status = 'processed',
                       processed_at = coalesce(processed_at, now()),
                       updated_at = now()
                   WHERE id = %s""", (period_id,))


def _bump_roster_declines(conn, autopay_id):
    # Engine-stamped decline state — ADR 009 §D derives this from the attempt
    # log instead (v_autopay_health); goes away with that migration.
    _exec(conn, """UPDATE billing.autopay_customers
                   SET consecutive_declines = consecutive_declines + 1,
                       payment_status = 'payment_issue', updated_at = now()
                   WHERE id = %s""", (autopay_id,))


def _clear_roster_declines(conn, autopay_id):
    _exec(conn, """UPDATE billing.autopay_customers
                   SET consecutive_declines = 0, payment_status = 'good', updated_at = now()
                   WHERE id = %s""", (autopay_id,))


def _project(conn, billing_month, qbo_customer_id):
    _exec(conn, "SELECT billing_audit.project_maint_processing_status(%s, %s)",
          (billing_month, qbo_customer_id))


def _switch_roster_pm(conn, p, dry_run):
    """The roster's linked method died (card replaced in QBO). Fall back to
    the customer's CURRENT default and re-point the roster so the switch is
    visible and durable. Returns True if switched."""
    if p["dpm_id"] is None:
        return False
    p["cpm_id"], p["qbo_payment_method_id"] = p["dpm_id"], p["dpm_qbo_id"]
    p["pm_type"], p["pm_brand"], p["pm_last4"] = p["dpm_type"], p["dpm_brand"], p["dpm_last4"]
    if not dry_run:
        _exec(conn, "SELECT public.maint_billing_autopay_set_pm(%s, %s)",
              (p["qbo_customer_id"], p["cpm_id"]))
    return True


def _send_invoice_copy(conn, p, access_token, realm_id):
    """Deliver the invoice copy UNLESS already delivered — never resend
    (manual 'Send invoice copies' is the only resend path). Echoes
    email_status on success. Returns {invoice: bool, errors: [...]}."""
    if p["email_status"] == "EmailSent":
        return {"invoice": True, "already": True, "errors": []}
    if not p["email"]:
        return {"invoice": False, "errors": ["no email on file"]}
    r = send_invoice(p["qbo_invoice_id"], p["email"], access_token, realm_id)
    if r["ok"]:
        mark_emailed(conn, p["qbo_invoice_id"])
    return {"invoice": r["ok"], "errors": [r["error"]] if r["error"] else []}


def _echo_after_charge(conn, qbo_invoice_id, delivered, access_token, realm_id):
    """VERIFIED ECHO (ADR 009 §C): write the balance QBO reports on a
    confirming read; on read failure write nothing (never fabricate 0 — the
    auto-promote trigger would fire on a lie). Email echo rides on the send."""
    post = get_qbo_invoice_details(qbo_invoice_id, realm_id, access_token)
    if post is not None:
        echo_balance(conn, qbo_invoice_id, post["balance"])
    if delivered:
        mark_emailed(conn, qbo_invoice_id)


def _res(p, status, **rest):
    return {"period": p["id"], "customer": p.get("customer_name"),
            "status": status, **rest}


def build_intent(p, channel, lines=None, charge_label=None):
    """Policy as data: the service fresh-reads every line and decides the
    amount. Receipt goes to the roster/customer email."""
    month_label = datetime.strptime(p["month_key"], "%Y-%m").strftime("%B")
    label = charge_label or p["doc_number"]
    return {
        "stage": STAGE,
        "qbo_invoice_id": p["qbo_invoice_id"],
        "lines": lines or [p["qbo_invoice_id"]],
        "payment_method_id": p["qbo_payment_method_id"],
        "cpm_id": p["cpm_id"],
        "channel": channel,
        "customer_id": p["qbo_customer_id"],
        "customer_name": p["customer_name"] or "",
        "invoice_number": p["doc_number"],
        "charge_label": label,
        "payment_ref": label,
        "memo_prefix": f"{month_label} Pool Maintenance | Inv# {label}",
        "receipt_email": p["email"],
    }


# ── per-period processing ────────────────────────────────────────────────────

def process_one(conn, period_id, access_token, realm_id, dry_run, force):
    p = _load_period(conn, period_id)
    if not p:
        return {"period": period_id, "status": "error", "error": "period not found"}

    # HARD GATE: only ready periods are chargeable (a flagged customer
    # structurally cannot reach this point)
    if p["processing_status"] != "ready_to_process" and not force:
        return _res(p, "skipped", error=f"not ready_to_process (is {p['processing_status']})")
    if p["locked_at"] is not None:
        return _res(p, "skipped", error="month locked")
    if not p["qbo_invoice_id"] or p["balance"] is None:
        return _res(p, "skipped", error="no linked/cached invoice")

    balance = float(p["balance"])
    on_autopay = p["autopay_id"] is not None
    pm_ok = (p["cpm_id"] is not None and p["pm_active"]
             and p["auto_disabled_at"] is None and p["deactivated_at"] is None)
    switched_pm = False
    if on_autopay and not pm_ok:
        pm_ok = switched_pm = _switch_roster_pm(conn, p, dry_run)
    is_bank = (p["pm_type"] or "").lower() in ("ach", "bank_account") \
        or "bank" in (p["pm_type"] or "").lower()
    channel = ("ach" if on_autopay and pm_ok and is_bank
               else "card" if on_autopay and pm_ok else "email")

    # already settled -> just make sure the invoice went out
    if balance <= 0:
        emails = {"invoice": p["email_status"] == "EmailSent"}
        if p["email_status"] != "EmailSent" and not dry_run:
            emails = _send_invoice_copy(conn, p, access_token, realm_id)
        return _res(p, "already_paid", invoice_sent=emails["invoice"])

    # prior-attempt policy gates (resume mechanics live in the service)
    prior = latest_attempt(conn, p["qbo_invoice_id"], STAGE)
    if prior and prior["status"] == "succeeded":
        return _res(p, "skipped", error="already succeeded")
    if prior and prior["status"] == "payment_orphan":
        return _res(p, "skipped", error="payment_orphan — human recovery required")
    if prior and prior["status"] in ("pending", "charge_uncertain", "charge_succeeded") \
            and stored_group_lines(prior):
        # in-flight GROUP anchor: single-path resume would misapply the
        # combined total to this one invoice
        return _res(p, "skipped",
                    error="in-flight grouped charge anchored on this invoice — "
                          "reprocess the customer's month as a batch")

    if channel == "email":
        # non-autopay: invoice email only, no charge
        already_sent = p["email_status"] == "EmailSent"
        if dry_run:
            return _res(p, "dry_run",
                        plan=(f"invoice #{p['doc_number']} already emailed — move to processed"
                              if already_sent else
                              f"send invoice #{p['doc_number']} to {p['email']} (no autopay)"))
        ok, errors = already_sent, None
        if not already_sent:
            attempt = create_attempt(conn, p["qbo_invoice_id"], STAGE, p["doc_number"],
                                     "email", balance, False)
            emails = _send_invoice_copy(conn, p, access_token, realm_id)
            ok, errors = emails["invoice"], emails["errors"] or None
            update_attempt(conn, attempt["id"],
                           status="succeeded" if ok else "email_failed",
                           email_sent=ok,
                           error_message=None if ok else "; ".join(emails["errors"]),
                           raw_result=_dumps(emails))
        if ok:
            _mark_period_processed(conn, period_id)
        return _res(p, "processed" if already_sent else "invoice_sent" if ok else "email_failed",
                    errors=errors)

    # autopay charge path
    if dry_run:
        return _res(p, "dry_run",
                    plan=f"charge {channel} {(p['pm_brand'] or p['pm_type'] or '')} "
                         f"····{p['pm_last4'] or '?'} {balance:.2f} for invoice #{p['doc_number']}, "
                         + (f"receipt to {p['email']} (invoice already emailed — no resend)"
                            if p["email_status"] == "EmailSent"
                            else f"receipt then invoice to {p['email']}")
                         + (" [roster PM dead — would switch to QBO default]" if switched_pm else ""))

    r = charge_and_record(conn, build_intent(p, channel), access_token, realm_id)
    return _dispatch_single(conn, p, r, access_token, realm_id, switched_pm)


def _dispatch_single(conn, p, r, access_token, realm_id, switched_pm):
    """Maintenance policy on the service outcome."""
    period_id = p["id"]
    if r["status"] == "read_failed":
        return _res(p, "read_failed",
                    error="fresh QBO invoice read failed — charge held (no stale-cache fallback)")
    if r["status"] == "already_paid":
        echo_balance(conn, p["qbo_invoice_id"], r["balances"][p["qbo_invoice_id"]])
        return _res(p, "already_paid",
                    error="invoice balance <= 0 at fresh read (paid upstream)")
    if r["status"] == "uncertain":
        return _res(p, "charge_uncertain", error=r["error"])
    if r["status"] == "payment_orphan":
        return _res(p, "payment_orphan", error=r["error"])

    if r["status"] == "declined":
        _bump_roster_declines(conn, p["autopay_id"])
        # declined -> the customer still gets the invoice (pay-it-yourself);
        # delivered = the month's processing is DONE (collection lives on the
        # invoice balance + roster payment_issue)
        emails = _send_invoice_copy(conn, p, access_token, realm_id)
        if emails["invoice"] and not emails.get("already"):
            update_attempt(conn, r["attempt_id"], email_sent=True)
        if emails["invoice"]:
            _mark_period_processed(conn, period_id)
        return _res(p, "charge_declined", error=r["error"],
                    invoice_sent=emails["invoice"],
                    processed=emails["invoice"] or None)

    # succeeded — deliver the copy, echo, roster health, projection
    emails = _send_invoice_copy(conn, p, access_token, realm_id)
    delivered = emails["invoice"]
    final = "succeeded" if delivered or r["receipt_sent"] else "email_failed"
    update_attempt(conn, r["attempt_id"], status=final,
                   email_sent=bool(delivered and not emails.get("already")))
    _clear_roster_declines(conn, p["autopay_id"])
    _echo_after_charge(conn, p["qbo_invoice_id"], delivered, access_token, realm_id)
    _project(conn, p["billing_month"], p["qbo_customer_id"])
    return _res(p, final, charged=r["amount"], charge_id=r["charge_id"],
                qbo_payment_id=r["payment_id"], receipt_sent=r["receipt_sent"],
                invoice_sent=(("already" if emails.get("already") else True)
                              if delivered else False),
                pm_switched_to_qbo_default=switched_pm or None,
                email_errors=emails["errors"] or None)


# ── customer group: one charge for a multi-invoice month ────────────────────

def process_customer_group(conn, period_ids, access_token, realm_id, dry_run, force):
    """A customer's multi-invoice month: ONE charge for the summed fresh
    balances and ONE QBO Payment applied across the invoices (receipt once).
    Non-chargeable members fall through to process_one unchanged. The WAL
    anchor is the lowest doc number; siblings get their rows AFTER the
    outcome (their keys are never charged)."""
    results, group, resume_anchor = [], [], None
    for pid in period_ids:
        p = _load_period(conn, pid)
        if not p:
            results.append({"period": pid, "status": "error", "error": "period not found"})
            continue
        ready = p["processing_status"] == "ready_to_process" or force
        chargeable = (ready and p["locked_at"] is None and p["qbo_invoice_id"]
                      and p["balance"] is not None and float(p["balance"]) > 0
                      and p["autopay_id"] is not None)
        if chargeable:
            prior = latest_attempt(conn, p["qbo_invoice_id"], STAGE)
            if prior and prior["status"] in ("pending", "charge_uncertain",
                                             "charge_succeeded", "payment_orphan"):
                if prior["status"] != "payment_orphan" and stored_group_lines(prior):
                    # interrupted grouped charge anchored here — resume with its
                    # STORED membership; never fold into a new charge
                    resume_anchor = (p, prior)
                    continue
                chargeable = False  # single-path resume owns it
        if chargeable:
            group.append(p)
        else:
            results.append(process_one(conn, pid, access_token, realm_id, dry_run, force))

    if resume_anchor is not None:
        p_r, prior = resume_anchor
        stored_ids = {inv for inv, _ in stored_group_lines(prior)}
        members = [p_r] + [p for p in group if p["qbo_invoice_id"] in stored_ids]
        results += [_res(p, "skipped",
                         error="deferred: interrupted grouped charge for this customer "
                               "must finish first — re-run after it resolves")
                    for p in group if p["qbo_invoice_id"] not in stored_ids]
        if dry_run:
            return results + [_res(p, "dry_run",
                                   plan=f"RESUME interrupted combined charge "
                                        f"({prior['status']}, {float(prior['charge_amount']):.2f}) "
                                        f"anchored on invoice #{prior['invoice_number']}")
                              for p in members]
        # anchor the intent on the interrupted anchor — the service resumes
        # from the persisted attempt + stored lines
        channel = "ach" if prior["channel"] == "ach" else "card"
        r = charge_and_record(conn, build_intent(p_r, channel), access_token, realm_id)
        return results + _dispatch_group(conn, members, r, channel,
                                         access_token, realm_id, False)

    if len(group) <= 1:
        return results + [process_one(conn, p["id"], access_token, realm_id, dry_run, force)
                          for p in group]

    # ── routing: one roster row (the anchor's) serves the whole group ──
    group.sort(key=lambda p: p["doc_number"] or "")
    a = group[0]
    pm_ok = (a["cpm_id"] is not None and a["pm_active"]
             and a["auto_disabled_at"] is None and a["deactivated_at"] is None)
    switched_pm = False
    if not pm_ok:
        pm_ok = switched_pm = _switch_roster_pm(conn, a, dry_run)
    if not pm_ok:
        # no live method: every invoice goes the email path individually
        return results + [process_one(conn, p["id"], access_token, realm_id, dry_run, force)
                          for p in group]

    is_bank = (a["pm_type"] or "").lower() in ("ach", "bank_account") \
        or "bank" in (a["pm_type"] or "").lower()
    channel = "ach" if is_bank else "card"
    docs_label = ", ".join(p["doc_number"] or "?" for p in group)

    if dry_run:
        total = round(sum(float(p["balance"]) for p in group), 2)
        return results + [_res(p, "dry_run",
                               plan=f"COMBINED charge {channel} {(a['pm_brand'] or a['pm_type'] or '')} "
                                    f"····{a['pm_last4'] or '?'} {total:.2f} covering invoices "
                                    f"#{docs_label} (this invoice's share {float(p['balance']):.2f}), "
                                    f"one QBO payment applied to all"
                                    + (" [roster PM dead — would switch to QBO default]"
                                       if switched_pm else ""))
                          for p in group]

    intent = build_intent(a, channel, lines=[p["qbo_invoice_id"] for p in group],
                          charge_label=docs_label)
    r = charge_and_record(conn, intent, access_token, realm_id)
    if r["status"] == "already_paid":
        # a member got paid upstream — collapse to the per-invoice path;
        # process_one re-guards each member individually
        return results + [process_one(conn, p["id"], access_token, realm_id, False, False)
                          for p in group]
    return results + _dispatch_group(conn, group, r, channel,
                                     access_token, realm_id, switched_pm)


def _dispatch_group(conn, group, r, channel, access_token, realm_id, switched_pm):
    """Group policy on the ONE service outcome: per-member delivery, sibling
    WAL rows (never 'pending' — their keys are never charged), echo, roster."""
    a = group[0]
    results = []

    if r["status"] == "read_failed":
        return [_res(p, "read_failed",
                     error="fresh QBO read failed for a group member — whole group held")
                for p in group]
    if r["status"] == "uncertain":
        return [_res(p, "charge_uncertain", error=r["error"], group_total=r["amount"])
                for p in group]
    if r["status"] == "payment_orphan":
        return [_res(p, "payment_orphan", error=r["error"], group_total=r["amount"])
                for p in group]

    if r["status"] == "declined":
        _bump_roster_declines(conn, a["autopay_id"])
        for i, p in enumerate(group):
            emails = _send_invoice_copy(conn, p, access_token, realm_id)
            if i > 0:
                sib = create_attempt(conn, p["qbo_invoice_id"], STAGE, p["doc_number"],
                                     channel, round(float(p["balance"]), 2),
                                     False, cpm_id=a["cpm_id"], status="charge_declined")
                update_attempt(conn, sib["id"], error_message=r["error"],
                               email_sent=emails["invoice"] and not emails.get("already"),
                               raw_result=_dumps({"group_anchor": a["doc_number"]}))
            elif emails["invoice"] and not emails.get("already"):
                update_attempt(conn, r["attempt_id"], email_sent=True)
            if emails["invoice"]:
                _mark_period_processed(conn, p["id"])
            results.append(_res(p, "charge_declined", error=r["error"],
                                invoice_sent=emails["invoice"], group_total=r["amount"]))
        return results

    # succeeded — receipt already sent ONCE by the service (anchor email)
    for i, p in enumerate(group):
        emails = _send_invoice_copy(conn, p, access_token, realm_id)
        delivered = emails["invoice"]
        member_share = round(float(p["balance"]), 2)
        final = "succeeded" if delivered or r["receipt_sent"] else "email_failed"
        if i == 0:
            update_attempt(conn, r["attempt_id"], status=final,
                           email_sent=bool(delivered and not emails.get("already")))
        else:
            sib = create_attempt(conn, p["qbo_invoice_id"], STAGE, p["doc_number"],
                                 channel, member_share, False,
                                 cpm_id=a["cpm_id"], status=final)
            update_attempt(conn, sib["id"], charge_id=r["charge_id"],
                           qbo_payment_id=r["payment_id"],
                           email_sent=bool(delivered and not emails.get("already")),
                           raw_result=_dumps({"group_anchor": a["doc_number"]}))
        _echo_after_charge(conn, p["qbo_invoice_id"], delivered, access_token, realm_id)
        results.append(_res(p, final, charged=member_share, group_total=r["amount"],
                            charge_id=r["charge_id"], qbo_payment_id=r["payment_id"],
                            receipt_sent=r["receipt_sent"] if i == 0 else None,
                            invoice_sent=(("already" if emails.get("already") else True)
                                          if delivered else False),
                            pm_switched_to_qbo_default=switched_pm or None))
    _clear_roster_declines(conn, a["autopay_id"])
    _project(conn, a["billing_month"], a["qbo_customer_id"])
    return results


# ── main: the event ──────────────────────────────────────────────────────────

def main(period_ids: list = None,
         qbo_customer_ids: list = None,
         billing_month: str = None,
         dry_run: bool = True,
         force: bool = False):
    """period_ids: explicit periods; OR qbo_customer_ids + billing_month
    ('YYYY-MM'): all their ready periods that month."""
    conn = get_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        ids = list(period_ids or [])
        if qbo_customer_ids:
            if not billing_month:
                return {"status": "error", "error": "billing_month required with qbo_customer_ids"}
            cur.execute(
                """SELECT id FROM billing_audit.task_billing_periods
                   WHERE qbo_customer_id = ANY(%s) AND billing_month = %s
                     AND processing_status = 'ready_to_process' AND locked_at IS NULL""",
                (qbo_customer_ids, f"{billing_month}-01"))
            ids += [r["id"] for r in cur.fetchall()]
        if not ids:
            return {"status": "noop", "error": "no ready periods matched"}

        # live runs: seed the processing queue up front so the UI shows the
        # whole batch (queued -> in flight -> resolved) from the first second
        if not dry_run:
            cur.execute(
                """INSERT INTO billing_audit.maint_process_queue (period_id)
                   SELECT unnest(%s::uuid[]) ON CONFLICT DO NOTHING""", (ids,))
            conn.commit()

        access_token, realm_id = refresh_qbo_token()

        # group the batch by customer: a multi-invoice customer gets ONE charge
        cur.execute(
            """SELECT id, qbo_customer_id FROM billing_audit.task_billing_periods
               WHERE id = ANY(%s::uuid[])""", (ids,))
        cust_by_period = {r["id"]: r["qbo_customer_id"] for r in cur.fetchall()}
        buckets, order = {}, []
        for pid in ids:
            key = cust_by_period.get(pid) or f"?{pid}"
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(pid)

        results = []
        for key in order:
            bucket = buckets[key]
            if not dry_run:
                cur.execute(
                    """UPDATE billing_audit.maint_process_queue
                       SET started_at = now() WHERE period_id = ANY(%s::uuid[]) AND finished_at IS NULL""",
                    (bucket,))
                conn.commit()
            try:
                if len(bucket) == 1:
                    results.append(process_one(conn, bucket[0], access_token,
                                               realm_id, dry_run, force))
                else:
                    results += process_customer_group(conn, bucket, access_token,
                                                      realm_id, dry_run, force)
            finally:
                if not dry_run:
                    # a raise can leave an aborted txn; committed work is safe
                    conn.rollback()
                    cur.execute(
                        """UPDATE billing_audit.maint_process_queue
                           SET finished_at = now() WHERE period_id = ANY(%s::uuid[]) AND finished_at IS NULL""",
                        (bucket,))
                    conn.commit()
        by_status = {}
        for r in results:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        return {"dry_run": dry_run, "periods": len(ids), "by_status": by_status,
                "results": results}
    finally:
        conn.close()
