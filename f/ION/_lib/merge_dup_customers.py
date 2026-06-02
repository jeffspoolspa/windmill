# requirements:
# psycopg2-binary
# requests

"""
f/ION/_lib/merge_dup_customers

Merge a duplicate customer/service_location pair onto the CANONICAL record (the
one QBO actually invoices). Fixes the SHIPWATCH/CHANEY-style split where the real
billing customer has the visits but the ION recurring task landed on an un-billed
duplicate twin.

Per pair {canonical_sl, dup_sl, dup_account, dup_qbo_id}:
  DB (one transaction):
    1. move maintenance.tasks  dup_sl -> canonical_sl
    2. move maintenance.visits dup_sl -> canonical_sl
    3. link canonical's now task-less visits to its open task (by date window)
    4. deactivate the duplicate service_location (is_active=false)
    5. deactivate the duplicate Customers account (is_active=false)
  QBO (AFTER db commit -- external writes can't roll back):
    6. sparse-update the duplicate QBO customer Active=false, so the
       qbo_customer_sync (which sets is_active = QBO.Active) does NOT re-activate it.

WHY both sides: public."Customers".is_active is synced FROM QBO; deactivating only
our row would be overwritten on the next sync. Deactivating the dup in QBO is what
makes it stick (and stops future ION visits/tasks splitting -- the address+name
resolver only matches active service_locations, so they land on canonical).

SAFETY: dry_run=True default. DB work runs in a transaction then ROLLS BACK; QBO is
only READ (reports current Active + SyncToken), never written. dry_run=False commits
the DB then performs the QBO deactivations (only if deactivate_in_qbo=True).

Public API:
    merge(pairs, supabase_connection, dry_run=True, deactivate_in_qbo=True) -> stats
"""

import requests
import wmill

from f.ION._lib.upsert import _connect

QBO_BASE = "https://quickbooks.api.intuit.com/v3/company"
QBO_RESOURCE = "u/carter/quickbooks_api"


def _qbo_headers():
    res = wmill.get_resource(QBO_RESOURCE)
    r = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": res["refresh_token"]},
        auth=(res["client_id"], res["client_secret"]),
    )
    r.raise_for_status()
    tok = r.json()
    res["refresh_token"] = tok["refresh_token"]
    wmill.set_resource(QBO_RESOURCE, res)
    return {
        "Authorization": f"Bearer {tok['access_token']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }, res["realm_id"]


def _qbo_read(headers, realm, qbo_id):
    g = requests.get(f"{QBO_BASE}/{realm}/customer/{qbo_id}?minorversion=73", headers=headers)
    g.raise_for_status()
    c = g.json()["Customer"]
    return {"qbo_id": qbo_id, "display_name": c.get("DisplayName"), "active": c.get("Active"), "sync_token": c["SyncToken"]}


def _qbo_deactivate(headers, realm, qbo_id, sync_token):
    body = {"Id": qbo_id, "SyncToken": sync_token, "sparse": True, "Active": False}
    p = requests.post(f"{QBO_BASE}/{realm}/customer?minorversion=73", headers=headers, json=body)
    p.raise_for_status()
    c = p.json()["Customer"]
    return {"qbo_id": qbo_id, "active": c.get("Active"), "deactivated": True}


def merge(pairs, supabase_connection, dry_run=True, deactivate_in_qbo=True):
    conn = _connect(supabase_connection)
    stats = {
        "pairs": len(pairs), "tasks_moved": 0, "visits_moved": 0, "visits_linked": 0,
        "sls_deactivated": 0, "accounts_deactivated": 0, "errors": [], "qbo": [],
        "dry_run": dry_run, "deactivate_in_qbo": deactivate_in_qbo, "committed": False,
    }
    dup_qbo_ids = []
    try:
        with conn.cursor() as cur:
            for p in pairs:
                can_sl, dup_sl = p["canonical_sl"], p["dup_sl"]
                dup_acct, dup_qbo = p.get("dup_account"), p.get("dup_qbo_id")

                cur.execute("SELECT count(*) FROM maintenance.tasks WHERE service_location_id=%s AND status IN ('active','paused')", (can_sl,))
                can_open = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM maintenance.tasks WHERE service_location_id=%s AND status IN ('active','paused')", (dup_sl,))
                dup_open = cur.fetchone()[0]
                if can_open > 0 and dup_open > 0:
                    stats["errors"].append({"pair": p, "error": "both canonical and dup have an open task; manual review (tasks_one_open_per_loc)"})
                    continue

                cur.execute("UPDATE maintenance.tasks SET service_location_id=%s, updated_at=now() WHERE service_location_id=%s", (can_sl, dup_sl))
                stats["tasks_moved"] += cur.rowcount
                cur.execute("UPDATE maintenance.visits SET service_location_id=%s, updated_at=now() WHERE service_location_id=%s", (can_sl, dup_sl))
                stats["visits_moved"] += cur.rowcount

                cur.execute("SELECT id, starts_on, ends_on FROM maintenance.tasks WHERE service_location_id=%s AND status IN ('active','paused') ORDER BY starts_on DESC LIMIT 1", (can_sl,))
                ot = cur.fetchone()
                if ot:
                    cur.execute(
                        """UPDATE maintenance.visits v SET task_id=%s, updated_at=now()
                           WHERE v.service_location_id=%s AND v.task_id IS NULL
                             AND v.visit_date >= %s AND (%s IS NULL OR v.visit_date <= %s)""",
                        (ot[0], can_sl, ot[1], ot[2], ot[2]),
                    )
                    stats["visits_linked"] += cur.rowcount

                cur.execute("UPDATE public.service_locations SET is_active=false, updated_at=now() WHERE id=%s", (dup_sl,))
                stats["sls_deactivated"] += cur.rowcount
                if dup_acct:
                    cur.execute('UPDATE public."Customers" SET is_active=false WHERE id=%s', (dup_acct,))
                    stats["accounts_deactivated"] += cur.rowcount
                if dup_qbo:
                    dup_qbo_ids.append(dup_qbo)

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
            stats["committed"] = True

        if dup_qbo_ids and (deactivate_in_qbo or dry_run):
            headers, realm = _qbo_headers()
            for qid in dup_qbo_ids:
                info = _qbo_read(headers, realm, qid)
                if dry_run or not deactivate_in_qbo:
                    info["would_set_active"] = False
                    stats["qbo"].append(info)
                else:
                    stats["qbo"].append(_qbo_deactivate(headers, realm, qid, info["sync_token"]))

        return stats
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


DEFAULT_PAIRS = [
    {"label": "SHIPWATCH", "canonical_sl": 4695, "canonical_account": 7076,
     "dup_sl": 4696, "dup_account": 7077, "dup_qbo_id": "8219"},
    {"label": "CHANEY", "canonical_sl": 78174, "canonical_account": 544101,
     "dup_sl": 12360, "dup_account": 546653, "dup_qbo_id": "9808"},
]


def main(supabase_connection, pairs=None, dry_run=True, deactivate_in_qbo=True):
    """Merge duplicate customer pairs onto the canonical (invoiced) record + deactivate
    the dup in QBO. Defaults to the SHIPWATCH/CHANEY pairs. dry_run rolls back DB +
    only reads QBO."""
    return merge(pairs or DEFAULT_PAIRS, supabase_connection, dry_run=dry_run, deactivate_in_qbo=deactivate_in_qbo)
