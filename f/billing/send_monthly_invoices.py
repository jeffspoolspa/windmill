import requests
import wmill
import psycopg2
import psycopg2.extras
import time


def refresh_qbo_tokens(resource_path: str) -> tuple:
    """Refresh QBO tokens and SAVE new refresh token. Returns (access_token, realm_id)."""
    resource = wmill.get_resource(resource_path)
    resp = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"]),
    )
    if not resp.ok:
        raise Exception(f"QBO token refresh failed: {resp.status_code} - {resp.text}")
    tokens = resp.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(resource_path, resource)
    return tokens["access_token"], resource["realm_id"]


def get_qbo_invoice_details(invoice_id: str, realm_id: str, access_token: str):
    try:
        resp = requests.get(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/invoice/{invoice_id}?minorversion=65",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        if not resp.ok:
            return None
        inv = resp.json().get("Invoice", {})
        return {
            "balance": float(inv.get("Balance", -1)),
            "email_status": inv.get("EmailStatus"),
        }
    except Exception:
        return None


def send_qbo_invoice_email(invoice_id: str, email: str, realm_id: str, access_token: str) -> tuple:
    try:
        resp = requests.post(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/invoice/{invoice_id}/send?sendTo={requests.utils.quote(email)}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/octet-stream",
            },
        )
        if resp.ok:
            return True, None
        return False, f"QBO {resp.status_code}: {resp.text[:300]}"
    except Exception as e:
        return False, str(e)


def main(
    billing_month: str = "2026-02",
    dry_run: bool = True,
    batch_delay_ms: int = 350,
):
    # -- 0. Stamp "{Month} Pool Maintenance" on empty memo fields. Idempotent.
    # Runs even on dry_run because memo stamping is pre-billing setup, not a send action.
    memo_result = wmill.run_script(
        path="f/billing/stamp_invoice_memos",
        args={"billing_month": billing_month, "dry_run": dry_run},
    )
    print(f"[memo stamp] {memo_result}")

    access_token, realm_id = refresh_qbo_tokens("u/carter/quickbooks_api")

    pg = wmill.get_resource("u/carter/supabase")
    conn = psycopg2.connect(
        host=pg["host"], port=pg["port"], dbname=pg["dbname"],
        user=pg["user"], password=pg["password"], sslmode="require",
    )
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    billing_date = f"{billing_month}-01"

    try:
        cur.execute(
            """
            SELECT mi.id, mi.qbo_invoice_id, mi.qbo_customer_id, mi.customer_name,
                   mi.invoice_total, mi.balance_due, c.email
            FROM billing_audit.maintenance_invoices mi
            LEFT JOIN public."Customers" c ON c.qbo_customer_id = mi.qbo_customer_id
            WHERE mi.billing_month = %s AND mi.send_status = 'pending'
            ORDER BY mi.customer_name
            """,
            (billing_date,),
        )
        invoices = cur.fetchall()
        print(f"{len(invoices)} invoices in pending state for {billing_month}")

        if dry_run:
            return {
                "dry_run": True, "billing_month": billing_month,
                "memo_stamp": memo_result,
                "would_send": len(invoices),
                "invoices": [
                    {"customer": inv["customer_name"], "invoice_id": inv["qbo_invoice_id"],
                     "email": inv["email"],
                     "amount": float(inv["invoice_total"]) if inv["invoice_total"] else 0,
                     "balance_due": float(inv["balance_due"]) if inv["balance_due"] else 0}
                    for inv in invoices
                ],
            }

        results = {
            "sent": 0, "skipped_already_sent": 0, "skipped_already_emailed_qbo": 0,
            "skipped_already_paid": 0, "skipped_balance_check_failed": 0,
            "skipped_no_email": 0, "failed": 0, "errors": [],
        }

        for inv in invoices:
            inv_id = inv["id"]
            qbo_invoice_id = inv["qbo_invoice_id"]
            qbo_customer_id = inv["qbo_customer_id"]
            customer_name = inv["customer_name"]
            email = inv["email"]

            if not email or not email.strip():
                print(f"SKIP {customer_name} ({qbo_invoice_id}) -- no email on file")
                cur.execute(
                    """UPDATE billing_audit.maintenance_invoices
                       SET send_status = 'held', send_held_reason = 'no_email' WHERE id = %s""",
                    (inv_id,),
                )
                cur.execute(
                    """INSERT INTO billing.invoice_send_log
                         (billing_month, qbo_invoice_id, qbo_customer_id, customer_name, email_address, status, error_message)
                       VALUES (%s, %s, %s, %s, %s, 'skipped', 'No email address on file')
                       ON CONFLICT (billing_month, qbo_invoice_id) DO UPDATE
                         SET status = 'skipped', error_message = EXCLUDED.error_message""",
                    (billing_month, qbo_invoice_id, qbo_customer_id, customer_name, email),
                )
                results["skipped_no_email"] += 1
                results["errors"].append(f"{customer_name}: No email on file")
                continue

            cur.execute(
                """SELECT id FROM billing.invoice_send_log
                   WHERE billing_month = %s AND qbo_invoice_id = %s AND status = 'sent'""",
                (billing_month, qbo_invoice_id),
            )
            if cur.fetchone():
                print(f"SKIP {customer_name} ({qbo_invoice_id}) -- already in send log")
                cur.execute(
                    "UPDATE billing_audit.maintenance_invoices SET send_status = 'sent' WHERE id = %s",
                    (inv_id,),
                )
                results["skipped_already_sent"] += 1
                continue

            details = get_qbo_invoice_details(qbo_invoice_id, realm_id, access_token)

            if details is None:
                print(f"SKIP {customer_name} ({qbo_invoice_id}) -- could not verify QBO invoice")
                cur.execute(
                    """UPDATE billing_audit.maintenance_invoices
                       SET send_status = 'failed', send_held_reason = 'balance_check_failed' WHERE id = %s""",
                    (inv_id,),
                )
                cur.execute(
                    """INSERT INTO billing.invoice_send_log
                         (billing_month, qbo_invoice_id, qbo_customer_id, customer_name, email_address, status, error_message)
                       VALUES (%s, %s, %s, %s, %s, 'failed', 'Could not verify QBO invoice before send')
                       ON CONFLICT (billing_month, qbo_invoice_id) DO UPDATE
                         SET status = 'failed', error_message = EXCLUDED.error_message""",
                    (billing_month, qbo_invoice_id, qbo_customer_id, customer_name, email),
                )
                results["skipped_balance_check_failed"] += 1
                results["errors"].append(f"{customer_name}: QBO invoice check failed")
                continue

            live_balance = details["balance"]
            email_status = details["email_status"]

            if live_balance <= 0:
                print(f"SKIP {customer_name} ({qbo_invoice_id}) -- balance ${live_balance}, already paid")
                cur.execute(
                    """UPDATE billing_audit.maintenance_invoices
                       SET send_status = 'not_applicable', balance_due = %s, send_held_reason = 'paid_before_send'
                       WHERE id = %s""",
                    (live_balance, inv_id),
                )
                results["skipped_already_paid"] += 1
                continue

            if email_status == "EmailSent":
                print(f"SKIP {customer_name} ({qbo_invoice_id}) -- QBO EmailStatus=EmailSent, already emailed")
                cur.execute(
                    """UPDATE billing_audit.maintenance_invoices
                       SET send_status = 'sent', sent_at = now() WHERE id = %s""",
                    (inv_id,),
                )
                cur.execute(
                    """INSERT INTO billing.invoice_send_log
                         (billing_month, qbo_invoice_id, qbo_customer_id, customer_name, email_address, status, sent_at, error_message)
                       VALUES (%s, %s, %s, %s, %s, 'sent', now(), 'Already emailed in QBO (EmailStatus=EmailSent)')
                       ON CONFLICT (billing_month, qbo_invoice_id) DO UPDATE
                         SET status = 'sent', sent_at = now(), error_message = 'Already emailed in QBO (EmailStatus=EmailSent)'""",
                    (billing_month, qbo_invoice_id, qbo_customer_id, customer_name, email),
                )
                results["skipped_already_emailed_qbo"] += 1
                continue

            success, err_msg = send_qbo_invoice_email(qbo_invoice_id, email, realm_id, access_token)

            if success:
                results["sent"] += 1
                cur.execute(
                    """UPDATE billing_audit.maintenance_invoices
                       SET send_status = 'sent', sent_at = now() WHERE id = %s""",
                    (inv_id,),
                )
                cur.execute(
                    """INSERT INTO billing.invoice_send_log
                         (billing_month, qbo_invoice_id, qbo_customer_id, customer_name, email_address, status, sent_at)
                       VALUES (%s, %s, %s, %s, %s, 'sent', now())
                       ON CONFLICT (billing_month, qbo_invoice_id) DO UPDATE
                         SET status = 'sent', sent_at = now(), error_message = NULL""",
                    (billing_month, qbo_invoice_id, qbo_customer_id, customer_name, email),
                )
                print(f"OK {customer_name} ({qbo_invoice_id}) ${live_balance}")
            else:
                results["failed"] += 1
                results["errors"].append(f"{customer_name}: {err_msg}")
                cur.execute(
                    """INSERT INTO billing.invoice_send_log
                         (billing_month, qbo_invoice_id, qbo_customer_id, customer_name, email_address, status, error_message)
                       VALUES (%s, %s, %s, %s, %s, 'failed', %s)
                       ON CONFLICT (billing_month, qbo_invoice_id) DO UPDATE
                         SET status = 'failed', error_message = EXCLUDED.error_message""",
                    (billing_month, qbo_invoice_id, qbo_customer_id, customer_name, email, err_msg),
                )
                print(f"FAIL {customer_name}: {err_msg}")

            time.sleep(batch_delay_ms / 1000.0)

        cur.execute(
            """UPDATE billing.billing_runs
               SET invoices_emailed = (
                 SELECT COUNT(*) FROM billing_audit.maintenance_invoices
                 WHERE billing_month = %s AND send_status = 'sent'
               ), updated_at = now()
               WHERE billing_month = %s""",
            (billing_date, billing_month),
        )

        cur.execute(
            """SELECT send_status, send_held_reason, COUNT(*) as count
               FROM billing_audit.maintenance_invoices
               WHERE billing_month = %s
               GROUP BY send_status, send_held_reason
               ORDER BY send_status, send_held_reason""",
            (billing_date,),
        )
        summary = [
            {"status": r["send_status"], "reason": r["send_held_reason"], "count": int(r["count"])}
            for r in cur.fetchall()
        ]

        return {
            "billing_month": billing_month,
            "memo_stamp": memo_result,
            "this_run": results,
            "month_summary": summary,
        }

    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
