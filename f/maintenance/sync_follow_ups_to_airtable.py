# f/maintenance/sync_follow_ups_to_airtable
#
# Single-writer sync: mirrors maintenance.follow_ups rows to the Airtable
# "Maintenance Follow up" table and reads Status back (Done -> closed).
#
# Module: docs/modules/maintenance/operations.md
# Status: [active]
# Concurrency key: airtable_api
#
# Triggered by:
#   - pg_net: AFTER INSERT trigger follow_ups_wake_sync on maintenance.follow_ups
#     (latency only — pg_net is at-most-once)
#   - pg_cron: job 'follow-ups-airtable-heartbeat' every 15 min via pg_net
#     (the delivery guarantee; also drives the Status read-back)
#
# Tables touched:
#   maintenance.follow_ups   [r/w]   drain rows with airtable_record_id IS NULL,
#                                    echo airtable_record_id/synced_at back;
#                                    read Status back for open synced rows
#   public.employees         [read]  tech display name
#   public."Customers"       [read]  customer display name + phone
#
# External APIs:
#   - Airtable: POST/GET https://api.airtable.com/v0/apppQeFQh1Mi6Mv3p/tbltojdp1l9k4xmSN
#   - Supabase Storage: signed URLs for follow-up media (bucket 'follow-ups')
#
# Why this exists:
#   The tech mobile site's Field Follow-Up form (2026-07-13) writes tickets to
#   maintenance.follow_ups as the source of truth, but the office still triages
#   in Airtable. Per ADR 008, the row itself is the outbox (airtable_record_id
#   IS NULL = pending) and this script is the ONLY writer of the sync columns.
#   Airtable copies attachment URLs at ingest, so 1-hour signed storage URLs
#   are sufficient. Status is Airtable-led while the office works there; this
#   script maps Airtable Status containing "Done" to status='closed' so the
#   form can show techs open/closed history. Delete the read-back step when
#   the app becomes the primary triage UI.

# requirements:
# wmill
# requests
# supabase

from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import wmill
from supabase import create_client

BASE_ID = "apppQeFQh1Mi6Mv3p"
TABLE_ID = "tbltojdp1l9k4xmSN"
AIRTABLE_URL = f"https://api.airtable.com/v0/{BASE_ID}/{TABLE_ID}"
MAX_ATTEMPTS = 5
SIGNED_URL_TTL_S = 3600  # Airtable copies attachments at ingest; 1h is ample


def _get_supabase():
    url = wmill.get_variable("f/SUPABASE/URL")
    key = wmill.get_variable("f/SUPABASE/SERVICE_ROLE_KEY")
    return create_client(url, key)


def _get_airtable_key():
    at_resource = wmill.get_resource("u/carter/airtable")
    at_key = at_resource.get("apiKey") if isinstance(at_resource, dict) else at_resource
    if isinstance(at_key, str) and at_key.startswith("$var:"):
        at_key = wmill.get_variable(at_key.replace("$var:", ""))
    return at_key


def _signed_url(sb, path):
    res = sb.storage.from_("follow-ups").create_signed_url(path, SIGNED_URL_TTL_S)
    return res.get("signedURL") or res.get("signedUrl") or res.get("signed_url")


def _push_pending(sb, headers):
    """Mirror unsynced rows to Airtable; echo record id back."""
    rows = (
        sb.schema("maintenance")
        .table("follow_ups")
        .select("*")
        .is_("airtable_record_id", "null")
        .lt("sync_attempts", MAX_ATTEMPTS)
        .order("created_at")
        .execute()
        .data
    )
    if not rows:
        return {"pushed": 0, "failed": 0, "errors": []}

    # Name lookups for the batch (small volume; two IN queries).
    emp_ids = list({r["tech_employee_id"] for r in rows})
    cust_ids = list({r["customer_id"] for r in rows})
    emps = sb.table("employees").select("id, first_name, last_name").in_("id", emp_ids).execute().data
    custs = sb.table("Customers").select("id, display_name, phone").in_("id", cust_ids).execute().data
    emp_name = {e["id"]: " ".join(filter(None, [e.get("first_name"), e.get("last_name")])) for e in emps}
    cust_name = {c["id"]: c.get("display_name") or "" for c in custs}
    cust_phone = {c["id"]: c.get("phone") for c in custs}

    pushed, failed, errors = 0, 0, []
    for r in rows:
        try:
            created_et = datetime.fromisoformat(r["created_at"]).astimezone(ZoneInfo("America/New_York"))
            images, videos = [], []
            for m in r.get("media") or []:
                url = _signed_url(sb, m["path"])
                if not url:
                    raise Exception(f"no signed url for {m['path']}")
                (videos if m.get("type") == "video" else images).append({"url": url})

            fields = {
                "Timestamp": created_et.strftime("%m/%d/%Y %I:%M %p"),
                "Tech Name": emp_name.get(r["tech_employee_id"], ""),
                "Customer Name": cust_name.get(r["customer_id"], ""),
                "Issue": r["issue"],
                "Description of Issue": r["description"],
            }
            phone = cust_phone.get(r["customer_id"])
            if phone:
                fields["Phone Number"] = phone
            if r.get("equipment_off") is not None:
                fields["Equipment Off?"] = "TRUE" if r["equipment_off"] else "FALSE"
            if images:
                fields["Images"] = images
            if videos:
                fields["video"] = videos

            resp = requests.post(
                AIRTABLE_URL,
                headers=headers,
                json={"records": [{"fields": fields}], "typecast": True},
                timeout=30,
            )
            if not resp.ok:
                raise Exception(f"Airtable POST failed ({resp.status_code}): {resp.text[:500]}")
            record_id = resp.json()["records"][0]["id"]

            sb.schema("maintenance").table("follow_ups").update({
                "airtable_record_id": record_id,
                "airtable_synced_at": "now()",
                "sync_error": None,
                "sync_attempts": (r.get("sync_attempts") or 0) + 1,
            }).eq("id", r["id"]).execute()
            pushed += 1
        except Exception as e:
            msg = str(e)[:1000]
            sb.schema("maintenance").table("follow_ups").update({
                "sync_error": msg,
                "sync_attempts": (r.get("sync_attempts") or 0) + 1,
            }).eq("id", r["id"]).execute()
            failed += 1
            errors.append({"id": r["id"], "error": msg})

    return {"pushed": pushed, "failed": failed, "errors": errors}


def _pull_status(sb, headers):
    """Close local rows whose Airtable Status contains 'Done'."""
    rows = (
        sb.schema("maintenance")
        .table("follow_ups")
        .select("id, airtable_record_id")
        .eq("status", "open")
        .not_.is_("airtable_record_id", "null")
        .execute()
        .data
    )
    closed = 0
    for r in rows:
        resp = requests.get(f"{AIRTABLE_URL}/{r['airtable_record_id']}", headers=headers, timeout=30)
        if not resp.ok:
            continue  # deleted/inaccessible record: leave open, next run retries
        statuses = resp.json().get("fields", {}).get("Status") or []
        if "Done" in statuses:
            sb.schema("maintenance").table("follow_ups").update({"status": "closed"}).eq("id", r["id"]).execute()
            closed += 1
    return {"checked": len(rows), "closed": closed}


def main():
    sb = _get_supabase()
    headers = {
        "Authorization": f"Bearer {_get_airtable_key()}",
        "Content-Type": "application/json",
    }
    push = _push_pending(sb, headers)
    pull = _pull_status(sb, headers)
    return {"push": push, "pull": pull}
