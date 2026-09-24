#extra_requirements:
#requests
#psycopg2-binary

import requests
import wmill
import psycopg2
from datetime import datetime, timezone


def refresh_qbo_token() -> tuple[str, str]:
    """Refresh QBO token, SAVE the rotated refresh token, return (access_token, realm_id)."""
    resource_path = "u/carter/quickbooks_api"
    resource = wmill.get_resource(resource_path)

    response = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": resource["refresh_token"]
        },
        auth=(resource["client_id"], resource["client_secret"])
    )

    if not response.ok:
        raise Exception(f"Token refresh failed: {response.status_code} - {response.text}")

    tokens = response.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(path=resource_path, value=resource)

    return tokens["access_token"], resource["realm_id"]


def get_db_conn():
    """Get psycopg2 connection using Windmill resource."""
    supabase = wmill.get_resource("u/carter/supabase")
    conn = psycopg2.connect(
        host=supabase.get("host"),
        port=supabase.get("port", 6543),
        dbname=supabase.get("dbname", "postgres"),
        user=supabase.get("user"),
        password=supabase.get("password"),
        sslmode=supabase.get("sslmode", "require"),
    )
    conn.autocommit = True
    return conn


def fetch_all(cur, sql: str, params: tuple) -> list[dict]:
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def main(deposit_id: str, dry_run: bool = False) -> dict:
    """
    Link an app deposit to an EXISTING QBO Deposit that already holds its payments,
    instead of creating a new one. Used when create_qbo_deposit failed because the
    payments were already deposited in QBO (by hand or by an earlier run).

    Finds the QBO Deposit via each Payment's LinkedTxn, requires exactly one, and
    links only if its TotalAmt matches our total (same basis as reconcileDepositFromQbo).
    Writes app_checks.* only.
    """
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        deposits = fetch_all(cur, "SELECT id, qbo_deposit_id FROM app_checks.deposits WHERE id = %s::uuid", (deposit_id,))
        if not deposits:
            return {"success": False, "deposit_id": deposit_id, "error": "deposit_not_found"}
        if deposits[0]["qbo_deposit_id"]:
            return {"success": True, "already_linked": True, "deposit_id": deposit_id,
                    "qbo_deposit_id": deposits[0]["qbo_deposit_id"]}

        checks = fetch_all(cur, "SELECT id, check_amount FROM app_checks.scanned_checks WHERE deposit_id = %s::uuid", (deposit_id,))
        check_ids = [str(c["id"]) for c in checks]
        # All check_payments (any entity type) feed the amount basis, as in reconcileDepositFromQbo.
        all_pmts = fetch_all(
            cur,
            "SELECT id, check_id, amount, qbo_entity_type, qbo_txn_id FROM app_checks.check_payments WHERE check_id = ANY(%s::uuid[])",
            (check_ids,),
        ) if check_ids else []
        cash = fetch_all(cur, "SELECT amount FROM app_checks.cash_entries WHERE deposit_id = %s::uuid", (deposit_id,))

        payments = [p for p in all_pmts if p["qbo_entity_type"] == "Payment" and p["qbo_txn_id"]]
        if not payments:
            return {"success": False, "deposit_id": deposit_id, "error": "no_payments"}

        pmt_sum = {}
        for p in all_pmts:
            pmt_sum[str(p["check_id"])] = pmt_sum.get(str(p["check_id"]), 0.0) + float(p["amount"] or 0)
        our_total = round(
            sum(pmt_sum.get(str(c["id"]), float(c["check_amount"] or 0)) for c in checks)
            + sum(float(c["amount"] or 0) for c in cash), 2)

        access_token, realm_id = refresh_qbo_token()
        base_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}"
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

        # ponytail: one GET per payment, sequential; fine for a deposit's handful of checks.
        # Every payment must exist in QBO and sit in the one chosen deposit, or we refuse
        # (same stance as create_qbo_deposit's pre-flight); never stamp a partial set.
        missing, dep_ids_by_pmt = [], {}
        for p in payments:
            r = requests.get(f"{base_url}/payment/{p['qbo_txn_id']}", headers=headers)
            if r.status_code in (400, 404):
                missing.append(str(p["id"]))
                continue
            r.raise_for_status()
            dep_ids_by_pmt[str(p["id"])] = {
                str(lt["TxnId"]) for lt in r.json().get("Payment", {}).get("LinkedTxn", [])
                if lt.get("TxnType") == "Deposit"
            }
        if missing:
            return {"success": False, "deposit_id": deposit_id, "error": "payments_missing_from_qbo", "payment_ids": missing}

        qbo_ids = set().union(*dep_ids_by_pmt.values())
        if not qbo_ids:
            return {"success": False, "deposit_id": deposit_id, "error": "not_found"}
        if len(qbo_ids) > 1:
            return {"success": False, "deposit_id": deposit_id, "error": "multiple", "deposit_ids": sorted(qbo_ids)}
        qbo_deposit_id = qbo_ids.pop()
        not_in = [pid for pid, ids in dep_ids_by_pmt.items() if qbo_deposit_id not in ids]
        if not_in:
            return {"success": False, "deposit_id": deposit_id, "error": "payments_not_in_deposit", "payment_ids": not_in}

        r = requests.get(f"{base_url}/deposit/{qbo_deposit_id}", headers=headers)
        r.raise_for_status()
        qbo_dep = r.json().get("Deposit", {})
        qbo_total = float(qbo_dep.get("TotalAmt", 0))
        if abs(qbo_total - our_total) >= 0.01:
            return {"success": False, "deposit_id": deposit_id, "error": "amount_mismatch",
                    "qbo_deposit_id": qbo_deposit_id, "qbo_total": qbo_total, "our_total": our_total}

        if not dry_run:
            now = datetime.now(timezone.utc).isoformat()
            bank_account_id = (qbo_dep.get("DepositToAccountRef") or {}).get("value")
            # Same writes create_qbo_deposit makes on success. Guarded on qbo_deposit_id IS NULL
            # so a create/link that won a race is not overwritten.
            # deposit_source='api' is deliberate: the UI keys "Awaiting Match / QBO Pending" off it.
            cur.execute("""
                UPDATE app_checks.deposits SET
                    qbo_deposit_id = %s, qbo_deposit_created_at = %s,
                    deposit_source = 'api', bank_account_id = %s, updated_at = %s
                WHERE id = %s::uuid AND qbo_deposit_id IS NULL
            """, (qbo_deposit_id, now, bank_account_id, now, deposit_id))
            if cur.rowcount == 0:
                return {"success": False, "deposit_id": deposit_id, "error": "linked_concurrently"}
            cur.execute("""
                UPDATE app_checks.check_payments SET
                    qbo_deposit_id = %s, qbo_deposit_date = %s, updated_at = %s
                WHERE id = ANY(%s::uuid[])
            """, (qbo_deposit_id, qbo_dep.get("TxnDate"), now, [str(p["id"]) for p in payments]))

        return {"success": True, "deposit_id": deposit_id, "qbo_deposit_id": qbo_deposit_id,
                "qbo_total": qbo_total, "our_total": our_total,
                "payment_count": len(payments), "dry_run": dry_run}
    finally:
        cur.close()
        conn.close()
