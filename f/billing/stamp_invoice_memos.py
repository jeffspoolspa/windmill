import requests
import wmill
import psycopg2
import psycopg2.extras
import time
import calendar


def refresh_qbo_tokens(resource_path: str) -> tuple:
    """Refresh QBO tokens and persist the new refresh token. Returns (access_token, realm_id)."""
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


def get_invoice(invoice_id: str, realm_id: str, access_token: str):
    """Fetch invoice. Returns dict with id, sync_token, private_note, customer_memo. None on error."""
    try:
        resp = requests.get(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/invoice/{invoice_id}?minorversion=65",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        if not resp.ok:
            return None
        inv = resp.json().get("Invoice", {})
        cm = inv.get("CustomerMemo") or {}
        return {
            "id": inv.get("Id"),
            "sync_token": inv.get("SyncToken"),
            "private_note": (inv.get("PrivateNote") or "").strip(),
            "customer_memo": (cm.get("value") or "").strip(),
        }
    except Exception:
        return None


def update_invoice_memos(
    invoice_id: str,
    sync_token: str,
    set_private_note: str | None,
    set_customer_memo: str | None,
    realm_id: str,
    access_token: str,
):
    """Sparse update — only fields with non-None values are sent in the body."""
    body = {"sparse": True, "Id": invoice_id, "SyncToken": sync_token}
    if set_private_note is not None:
        body["PrivateNote"] = set_private_note
    if set_customer_memo is not None:
        body["CustomerMemo"] = {"value": set_customer_memo}
    try:
        resp = requests.post(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/invoice?minorversion=65",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=body,
        )
        if resp.ok:
            return True, None
        return False, f"QBO {resp.status_code}: {resp.text[:300]}"
    except Exception as e:
        return False, str(e)


def main(
    billing_month: str = "2026-04",
    dry_run: bool = True,
    batch_delay_ms: int = 200,
):
    # Normalize "YYYY-MM-01" → "YYYY-MM" so callers can pass either form.
    if len(billing_month) == 10 and billing_month.endswith("-01"):
        billing_month = billing_month[:7]

    year, month = map(int, billing_month.split("-"))
    month_name = calendar.month_name[month]
    memo_text = f"{month_name} Pool Maintenance"

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
            SELECT qbo_invoice_id, customer_name
            FROM billing_audit.maintenance_invoices
            WHERE billing_month = %s
            ORDER BY customer_name
            """,
            (billing_date,),
        )
        invoices = cur.fetchall()
        print(f"{len(invoices)} maintenance invoices for {billing_month}; memo text='{memo_text}'; dry_run={dry_run}")

        results = {
            "total": len(invoices),
            "stamped_both": 0,           # both fields empty → stamped both
            "stamped_private_only": 0,   # CustomerMemo had content → stamped PrivateNote only
            "stamped_customer_only": 0,  # PrivateNote had content → stamped CustomerMemo only
            "skipped_both_present": 0,   # both fields already had content → no-op
            "qbo_fetch_failed": 0,
            "update_failed": 0,
            "errors": [],
        }
        examples = {
            "stamped_both": [],
            "stamped_private_only": [],
            "stamped_customer_only": [],
            "skipped_both_present": [],
        }

        for row in invoices:
            qbo_invoice_id = row["qbo_invoice_id"]
            customer = row["customer_name"]

            inv = get_invoice(qbo_invoice_id, realm_id, access_token)
            if inv is None:
                results["qbo_fetch_failed"] += 1
                results["errors"].append(f"{customer} ({qbo_invoice_id}): QBO fetch failed")
                continue

            need_private = not inv["private_note"]
            need_customer = not inv["customer_memo"]

            if need_private and need_customer:
                bucket = "stamped_both"
                set_pn = memo_text
                set_cm = memo_text
            elif need_private:
                bucket = "stamped_private_only"
                set_pn = memo_text
                set_cm = None
            elif need_customer:
                bucket = "stamped_customer_only"
                set_pn = None
                set_cm = memo_text
            else:
                bucket = "skipped_both_present"
                set_pn = None
                set_cm = None

            if len(examples[bucket]) < 5:
                examples[bucket].append({
                    "customer": customer,
                    "invoice_id": qbo_invoice_id,
                    "existing_private": (inv["private_note"][:80] if inv["private_note"] else ""),
                    "existing_customer_memo": (inv["customer_memo"][:80] if inv["customer_memo"] else ""),
                })

            if bucket == "skipped_both_present":
                results[bucket] += 1
                continue

            if dry_run:
                results[bucket] += 1
                time.sleep(batch_delay_ms / 1000.0)
                continue

            ok, err = update_invoice_memos(
                qbo_invoice_id, inv["sync_token"], set_pn, set_cm, realm_id, access_token
            )
            if ok:
                results[bucket] += 1
            else:
                results["update_failed"] += 1
                results["errors"].append(f"{customer} ({qbo_invoice_id}): {err}")
            time.sleep(batch_delay_ms / 1000.0)

        print(
            f"=== Memo stamp complete === "
            f"total={results['total']} both={results['stamped_both']} "
            f"pn_only={results['stamped_private_only']} cm_only={results['stamped_customer_only']} "
            f"skipped={results['skipped_both_present']} "
            f"fetch_fail={results['qbo_fetch_failed']} update_fail={results['update_failed']}"
        )

        return {
            "billing_month": billing_month,
            "memo_text": memo_text,
            "dry_run": dry_run,
            **results,
            "examples": examples,
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
