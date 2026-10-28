"""
Daily field-app sign-out report.

Source:  public.inventory_sign_outs (one row per item per sign-out)
Joins:   employees (tech name), items (item name, sku)
Window:  one calendar day in America/New_York
Send:    Gmail API, service account with domain-wide delegation, as jpsbilling@
"""
# requirements:
# wmill
# google-auth
# requests
# supabase
import base64
import html as htmllib
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests
import wmill
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from supabase import create_client

ET = ZoneInfo("America/New_York")
SENDER = "jpsbilling@jeffspoolspa.com"
FROM_NAME = "Jeff's Pool & Spa Service"


def _fetch(day: date):
    start = datetime.combine(day, time.min, ET)
    end = start + timedelta(days=1)
    client = create_client(
        wmill.get_variable("f/SUPABASE/URL"),
        wmill.get_variable("f/SUPABASE/SERVICE_ROLE_KEY"),
    )
    rows, page, size = [], 0, 1000
    while True:
        res = (
            client.table("inventory_sign_outs")
            .select("quantity,signed_out_at,employees(first_name,last_name),items(item_name,sku)")
            .gte("signed_out_at", start.isoformat())
            .lt("signed_out_at", end.isoformat())
            .order("signed_out_at")
            .range(page * size, page * size + size - 1)
            .execute()
        )
        rows += res.data or []
        if len(res.data or []) < size:
            return rows
        page += 1


def _num(q):
    q = float(q or 0)
    return int(q) if q.is_integer() else round(q, 2)


def _build(rows, day: date):
    by_tech = defaultdict(lambda: defaultdict(float))
    tech_first = {}
    by_item = defaultdict(lambda: {"qty": 0.0, "techs": set()})

    for r in rows:
        e = r.get("employees") or {}
        i = r.get("items") or {}
        tech = f"{e.get('first_name') or ''} {e.get('last_name') or ''}".strip() or "Unknown tech"
        item = i.get("item_name") or "Unknown item"
        sku = i.get("sku") or ""
        qty = float(r.get("quantity") or 0)
        ts = datetime.fromisoformat(r["signed_out_at"].replace("Z", "+00:00")).astimezone(ET)
        tech_first[tech] = min(tech_first.get(tech, ts), ts)
        by_tech[tech][(item, sku)] += qty
        by_item[(item, sku)]["qty"] += qty
        by_item[(item, sku)]["techs"].add(tech)

    esc = htmllib.escape
    th = 'style="text-align:left;padding:6px 10px;border-bottom:2px solid #ccc;"'
    td = 'style="padding:6px 10px;border-bottom:1px solid #eee;"'
    tdr = 'style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right;"'
    day_label = day.strftime("%A, %B %-d, %Y")

    parts = [f'<div style="font-family:Arial,sans-serif;font-size:14px;color:#222;">',
             f"<h2 style=\"margin:0 0 4px;\">Daily Sign-Out Report</h2>",
             f"<p style=\"margin:0 0 16px;color:#555;\">{esc(day_label)}</p>"]
    text = [f"Daily Sign-Out Report: {day_label}", ""]

    if not rows:
        parts.append("<p>No items were signed out today.</p></div>")
        text.append("No items were signed out today.")
        return "".join(parts), "\n".join(text), 0, 0

    parts.append(f"<p>{len(by_tech)} tech(s) signed out {len(by_item)} different item(s).</p>")
    parts.append('<h3 style="margin:20px 0 6px;">By tech</h3>')
    text.append("BY TECH")
    for tech in sorted(by_tech, key=lambda t: tech_first[t]):
        first = tech_first[tech].strftime("%-I:%M %p")
        parts.append(f'<p style="margin:14px 0 4px;"><b>{esc(tech)}</b> <span style="color:#777;">(first sign-out {first})</span></p>')
        parts.append(f'<table style="border-collapse:collapse;min-width:420px;"><tr><th {th}>Item</th><th {th}>SKU</th><th {th}>Qty</th></tr>')
        text.append(f"{tech} (first sign-out {first})")
        for (item, sku), q in sorted(by_tech[tech].items()):
            parts.append(f"<tr><td {td}>{esc(item)}</td><td {td}>{esc(sku)}</td><td {tdr}>{_num(q)}</td></tr>")
            text.append(f"  {item} [{sku}]: {_num(q)}")
        parts.append("</table>")
        text.append("")

    parts.append('<h3 style="margin:28px 0 6px;">Item totals</h3>')
    parts.append(f'<table style="border-collapse:collapse;min-width:420px;"><tr><th {th}>Item</th><th {th}>SKU</th><th {th}>Total</th><th {th}>Techs</th></tr>')
    text.append("ITEM TOTALS")
    for (item, sku), v in sorted(by_item.items()):
        techs = ", ".join(sorted(v["techs"]))
        parts.append(f"<tr><td {td}>{esc(item)}</td><td {td}>{esc(sku)}</td><td {tdr}>{_num(v['qty'])}</td><td {td}>{esc(techs)}</td></tr>")
        text.append(f"  {item} [{sku}]: {_num(v['qty'])} ({techs})")
    parts.append("</table></div>")

    return "".join(parts), "\n".join(text), len(by_tech), len(by_item)


def _send(to, cc, subject, html, text):
    creds = service_account.Credentials.from_service_account_info(
        wmill.get_resource("u/carter/gmail_gcp_service_account"),
        scopes=["https://www.googleapis.com/auth/gmail.send"],
        subject=SENDER,
    )
    creds.refresh(Request())
    msg = MIMEMultipart("alternative")
    msg["To"] = to
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["From"] = f"{FROM_NAME} <{SENDER}>"
    msg["Subject"] = subject
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    resp = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={"Authorization": f"Bearer {creds.token}"},
        json={"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()},
        timeout=30,
    )
    if not resp.ok:
        raise Exception(f"Gmail send failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json().get("id")


def main(
    to: str = "mary@jeffspoolspa.com",
    cc: list = None,
    report_date: str = None,
    dry_run: bool = True,
):
    """report_date: YYYY-MM-DD in Eastern time; defaults to today (ET)."""
    day = date.fromisoformat(report_date) if report_date else datetime.now(ET).date()
    rows = _fetch(day)
    html, text, n_techs, n_items = _build(rows, day)
    subject = f"Daily Sign-Out Report: {day.strftime('%a %b %-d')}"
    result = {"date": day.isoformat(), "rows": len(rows), "techs": n_techs, "items": n_items,
              "to": to, "cc": cc or [], "subject": subject, "dry_run": dry_run}
    if dry_run:
        result["text_preview"] = text
        return result
    result["message_id"] = _send(to, cc or [], subject, html, text)
    return result
