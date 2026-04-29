"""
Quote follow-up cadence for maintenance leads.

Cadence (gap from last_contacted_at):
  attempts 0 -> +2d, 1 -> +3d, 2 -> +5d, >=3 stop.
Channel uses lead.quote_channel. Embeds accept URL via card_collection_request.
Does NOT auto-expire.
"""
import wmill
from datetime import datetime, timezone, timedelta
from supabase import create_client

COMPANY_NAME = "Jeff's Pool & Spa Service"
COMPANY_PHONE = "(912) 459-0160"
ACCEPT_BASE = "https://jeffspoolspa.github.io/perfectpools-redesign/get-started/"
GAP_DAYS = {0: 2, 1: 3, 2: 5}


def _client():
    url = wmill.get_variable("f/SUPABASE/URL")
    key = wmill.get_variable("f/SUPABASE/SERVICE_ROLE_KEY")
    return create_client(url, key)


def _frequency_label(v):
    if v == 0.5: return "Bi-weekly"
    if v == 2:   return "2x / week"
    if v and v >= 3: return f"{int(v)}x / week"
    return "Weekly"


def _service_line(b, n):
    base = (b or "pool").capitalize() + " maintenance"
    if n > 0:
        base += f" + {n} additional bod" + ("ies" if n > 1 else "y")
    return base


def _quote_summary(c):
    return (
        f"Service: {_service_line(c['primary_body_type'], c['additional_count'])}\n"
        f"Frequency: {_frequency_label(c['visits_per_week'])}\n"
        f"Per visit: ${c['per_visit']}\n"
        f"First month deposit: ${c['first_months_deposit']}"
    )


def _followup_email(c, attempt, url):
    if attempt == 1:
        opener = f"Hi {c['first_name']},\n\nJust following up on the pool maintenance quote we sent over.\n\n"
    elif attempt == 2:
        opener = f"Hi {c['first_name']},\n\nChecking in one more time on your pool maintenance quote. Here are the details again:\n\n"
    else:
        opener = f"Hi {c['first_name']},\n\nLast check-in on your pool maintenance quote. If now isn't the right time just let us know. Otherwise we're ready whenever you are:\n\n"
    return {
        "subject": f"Following up on your pool maintenance quote -- {COMPANY_NAME}",
        "text": (
            opener +
            f"{_quote_summary(c)}\n\n"
            f"Accept your quote and save your card on file:\n{url}\n\n"
            f"Or reply to this email or call us at {COMPANY_PHONE}.\n\n"
            f"{COMPANY_NAME}\n{COMPANY_PHONE}"
        ),
    }


def _followup_sms(c, attempt, url):
    lead_in = ["Just following up on your pool maintenance quote",
               "Checking in on your pool maintenance quote",
               "Last check-in on your pool maintenance quote"][min(attempt - 1, 2)]
    return (
        f"Hi {c['first_name']}! {lead_in} from {COMPANY_NAME}: "
        f"{_frequency_label(c['visits_per_week'])} {c['primary_body_type']}: ${c['per_visit']}/visit "
        f"(deposit: ${c['first_months_deposit']}). Accept & start: {url} or call {COMPANY_PHONE}."
    )


def _text_to_html(t):
    import re
    esc = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    linked = re.sub(r"(https?://[^\s]+)", r'<a href="\1">\1</a>', esc)
    return f'<div style="font-family:system-ui,sans-serif;line-height:1.5;color:#222;white-space:pre-wrap">{linked}</div>'


def _build_ctx(d):
    bodies = d.get("bodies") or []
    primary = next((b for b in bodies if b.get("is_primary")), bodies[0] if bodies else {})
    return {
        "first_name": d.get("first_name") or "",
        "primary_body_type": primary.get("body_type") or "pool",
        "additional_count": max(0, len(bodies) - 1),
        "visits_per_week": float(d.get("visits_per_week") or 1),
        "per_visit": int(d.get("quoted_per_visit") or 0),
        "first_months_deposit": int(d.get("first_months_deposit") or 0),
    }


def _due(last, attempts, now):
    if attempts >= 3 or attempts not in GAP_DAYS: return False
    if not last: return True
    try:
        l = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except Exception:
        return True
    return (now - l) >= timedelta(days=GAP_DAYS[attempts])


def main():
    client = _client()
    now = datetime.now(timezone.utc)
    rows = (client.schema("maintenance").table("maintenance_leads")
            .select("id, contact_attempts, last_contacted_at, quote_channel")
            .eq("status", "quoted").lt("contact_attempts", 3).execute().data or [])
    sent, skipped, failed = [], [], []
    for row in rows:
        lead_id = row["id"]
        attempts = int(row.get("contact_attempts") or 0)
        if not _due(row.get("last_contacted_at"), attempts, now):
            skipped.append({"lead_id": lead_id, "reason": "not_due"}); continue
        channel = row.get("quote_channel")
        if channel not in ("email", "sms"):
            skipped.append({"lead_id": lead_id, "reason": "no_channel"}); continue
        try:
            detail = client.rpc("get_maintenance_lead_detail", {"p_lead_id": lead_id}).execute().data
            if not detail:
                skipped.append({"lead_id": lead_id, "reason": "lead_not_found"}); continue
            ctx = _build_ctx(detail)
            office = detail.get("office")
            attempt_number = attempts + 1
            tok = client.rpc("create_card_collection_request", {
                "p_lead_id": lead_id,
                "p_pre_auth_amount": (ctx["first_months_deposit"] * 100) if ctx["first_months_deposit"] else None,
            }).execute().data
            if not tok or not tok.get("token"):
                raise Exception(f"Failed to mint accept token: {tok}")
            accept_url = f"{ACCEPT_BASE}?token={tok['token']}"
            if channel == "email":
                email = detail.get("email")
                if not email:
                    skipped.append({"lead_id": lead_id, "reason": "no_email"}); continue
                msg = _followup_email(ctx, attempt_number, accept_url)
                wmill.run_script_sync("f/comms/send_email", args={
                    "to": email, "subject": msg["subject"], "html": _text_to_html(msg["text"]),
                    "text": msg["text"], "office": office, "lead_id": lead_id,
                })
            else:
                phone = detail.get("phone")
                if not phone:
                    skipped.append({"lead_id": lead_id, "reason": "no_phone"}); continue
                body = _followup_sms(ctx, attempt_number, accept_url)
                wmill.run_script_sync("f/comms/send_sms", args={
                    "to": phone, "body": body, "office": office, "lead_id": lead_id,
                })
            client.schema("maintenance").table("maintenance_leads").update({
                "contact_attempts": attempt_number,
                "last_contacted_at": now.isoformat(),
            }).eq("id", lead_id).execute()
            sent.append({"lead_id": lead_id, "channel": channel, "attempt": attempt_number})
        except Exception as e:
            failed.append({"lead_id": lead_id, "error": str(e)[:300]})
    return {"scanned": len(rows), "sent": sent, "skipped": skipped, "failed": failed, "ran_at": now.isoformat()}
