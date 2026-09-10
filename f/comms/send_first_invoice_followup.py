# requirements:
# wmill
# google-auth
# requests
# psycopg2-binary

import base64, uuid, html as H, time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests, wmill, psycopg2, psycopg2.extras
from google.oauth2 import service_account
from google.auth.transport.requests import Request

AUTH_SENDER = "jpsbilling@jeffspoolspa.com"
FROM_NAME = "Perfect Pools"
CC = ["chris@jeffspoolspa.com"]
OFFICE = "richmond_hill"
TEMPLATE_KEY = "first_invoice_chemical_discount_aug2026"
SUBJECT = "Important Notice about your August Invoice"
PHONE = "(912) 303-7372"

BLOCKS = [
    ("p", "Since your August invoice is your first one from us, we wanted to take the opportunity to explain your bill and any potential discounts that you may have seen."),
    ("h", "What we did on your August invoice"),
    ("p", "Every invoice you get from us itemizes the chemicals your pool used that month, so you can always see what went in and what it cost. For August we went one step further and discounted nearly all routine chemicals in full as an introductory credit while you get accustomed to your pool's chemical demand."),
    ("p", "As part of this, we included a custom memo on each invoice breaking down the bill and any discounts for your pool in August, including whether an issue was already identified:"),
    ("memo", None),
    ("p", "Our goal with these discounts is to provide transparency on what your pool requires, and evaluate accounts with higher bills for potential equipment issues or other contributing factors that may require our joint attention. We will continue to evaluate bills month to month, and higher chemical usage caused by unaddressed issues may be billed at full price on future invoices."),
    ("h", "Why the chemical lines and service logs matter"),
    ("p", "Chemical demand is a property of the pool itself: its size, the sun it gets, how much it is used, how much debris gets in the pool, and above all how well the equipment is doing its job. When a pool needs a lot of product to hold a chlorine level, that is almost always reactive treatment. With the usual causes being attributable to a piece of equipment that has quietly stopped doing its part."),
    ("p", "So if a large amount of chemicals are showing up on your bill or service logs, treat it as information: your pool might be asking for more than it should, and there is probably a fix that costs less over a season than the chemicals do."),
    ("p", "Our office has already reached out to a number of customers about problems we found in August, and were able to successfully identify and repair or replace equipment that was at the end of its life on several pools."),
    ("h", "How we would like to work with you"),
    ("p", "We think of every customer as a long-term partner, and align our interests around keeping chemical costs stable and avoiding surprises both with your bill and your pool. We do our best internally to monitor bills and chemical usage throughout the month and proactively identify equipment issues while on site so that we can inform you when a piece of equipment is not doing its job, and make recommendations on how to improve efficiency."),
    ("p", "If an issue is identified at your pool, our service and repair department knows these systems well, will give you straight advice, and is a great resource for our customers to quickly get issues diagnosed and get pools running efficiently."),
    ("p", f"Please look over your invoice and call us at {PHONE} or reply to this email with any questions."),
]

LIST_SQL = """
with cb as (select i.account_id, i.ref::int qbo_id from accounts.identities i
            where i.kind='qbo_customer' and i.ref ~ '^[0-9]+$' and i.ref::int between 10000 and 10090)
select a.id account_id, a.display_name, inv.id invoice_id, inv.doc_number, inv.memo, qc.email,
       exists (select 1 from operations.agreements oa where oa.account_id=a.id and oa.closed_on is not null) closed,
       exists (select 1 from comms.messages m join comms.message_accounts ma on ma.message_id=m.id
               where ma.account_id=a.id and m.template_key=%s and m.status='sent') already_sent
from cb join accounts.accounts a on a.id=cb.account_id
join revenue.invoices inv on inv.account_id=cb.account_id and inv.billing_month='2026-08'
     and inv.doc_number like '8047%%' and inv.status='sent'
left join qbo.customers qc on qc.id=cb.qbo_id::text
where inv.memo <> 'August Pool Maintenance' and inv.memo not ilike 'Final invoice%%'
  and a.display_name <> 'YAKIMA, CHARLOTTE'
order by a.display_name
"""


def first_name(display):
    last, _, rest = display.partition(", ")
    return (rest.split() or [last])[0].title()


def render(first, memo):
    hp, tp = [], []
    hp.append(f"<p>Hi {H.escape(first)},</p>"); tp.append(f"Hi {first},\n")
    for kind, txt in BLOCKS:
        if kind == "h":
            hp.append(f"<p style='margin:18px 0 6px'><b>{H.escape(txt)}</b></p>"); tp.append(f"\n{txt}\n")
        elif kind == "memo":
            hp.append("<blockquote style='margin:10px 0 14px;padding:10px 14px;background:#f1f5f9;"
                      f"border-left:4px solid #94a3b8;color:#1f2937'>{H.escape(memo)}</blockquote>")
            tp.append(f"\n    {memo}\n")
        else:
            hp.append(f"<p>{H.escape(txt)}</p>"); tp.append(txt + "\n")
    hp.append("<p>Thank you for trusting us with your pool,<br>Perfect Pools</p>")
    tp.append("\nThank you for trusting us with your pool,\nPerfect Pools\n")
    html = ("<div style=\"font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;"
            "color:#111;max-width:640px\">" + "\n".join(hp) + "</div>")
    return html, "\n".join(tp)


def gmail_token():
    sa = wmill.get_resource("u/carter/gmail_gcp_service_account")
    creds = service_account.Credentials.from_service_account_info(
        sa, scopes=["https://www.googleapis.com/auth/gmail.send"], subject=AUTH_SENDER)
    creds.refresh(Request())
    return creds.token


def send(token, to, html, text, cc):
    msg = MIMEMultipart("alternative")
    msg["To"] = to
    msg["From"] = f"{FROM_NAME} <{AUTH_SENDER}>"
    msg["Reply-To"] = AUTH_SENDER
    msg["Subject"] = SUBJECT
    if cc: msg["Cc"] = ", ".join(cc)
    msg.attach(MIMEText(text, "plain")); msg.attach(MIMEText(html, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    r = requests.post("https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      json={"raw": raw}, timeout=30)
    if not r.ok:
        raise Exception(f"Gmail {r.status_code}: {r.text[:300]}")
    return r.json().get("id")


def log(cur, row, to, cc, html, text, status, provider_id, err):
    mid = str(uuid.uuid4())
    cur.execute("""insert into comms.messages (id, channel, direction, office, reason, from_address, to_address, status,
                   provider, provider_message_id, error, requested_at, sent_at, actor, template_key, template_version)
                   values (%s,'email','outbound',%s,'first_invoice_followup',%s,%s,%s,'gmail',%s,%s,now(),
                           case when %s='sent' then now() end,'windmill:send_first_invoice_followup',%s,1)""",
                (mid, OFFICE, f"{FROM_NAME} <{AUTH_SENDER}>", to, status, provider_id, err, status, TEMPLATE_KEY))
    cur.execute("insert into comms.email_parts (message_id, subject, body_html, body_text, cc, bcc) values (%s,%s,%s,%s,%s,%s)",
                (mid, SUBJECT, html, text, cc, []))
    cur.execute("insert into comms.message_accounts values (%s,%s)", (mid, row["account_id"]))
    cur.execute("insert into comms.message_invoices values (%s,%s)", (mid, row["invoice_id"]))
    return mid


def main(dry_run: bool = True, test_to: str = None, only: list = None, skip: list = None, limit: int = 0):
    pg = wmill.get_resource("u/carter/supabase")
    conn = psycopg2.connect(host=pg["host"], port=pg["port"], dbname=pg["dbname"],
                            user=pg["user"], password=pg["password"], sslmode="require")
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(LIST_SQL, (TEMPLATE_KEY,))
    rows = cur.fetchall()

    plan, held = [], []
    for r in rows:
        why = None
        if r["closed"]: why = "agreement closed"
        elif not r["email"]: why = "no email"
        elif r["already_sent"]: why = "already sent"
        elif only and r["display_name"] not in only: why = "not in only[]"
        elif skip and r["display_name"] in skip: why = "in skip[]"
        (held if why else plan).append({**r, "why": why})
    if limit: plan = plan[:limit]

    summary = {"eligible": len(plan), "held": [(h["display_name"], h["why"]) for h in held],
               "dry_run": dry_run, "test_to": test_to, "sent": [], "failed": []}
    if dry_run:
        summary["would_send"] = [(p["display_name"], p["email"], p["doc_number"]) for p in plan]
        return summary

    token = gmail_token()
    if test_to:
        p = plan[0]
        html, text = render(first_name(p["display_name"]), p["memo"])
        mid = send(token, test_to, html, text, [])
        return {**summary, "test_sample": p["display_name"], "gmail_id": mid}

    for p in plan:
        html, text = render(first_name(p["display_name"]), p["memo"])
        try:
            gid = send(token, p["email"], html, text, CC)
            log(cur, p, p["email"], CC, html, text, "sent", gid, None)
            summary["sent"].append((p["display_name"], p["email"]))
        except Exception as e:
            log(cur, p, p["email"], CC, html, text, "failed", None, str(e)[:500])
            summary["failed"].append((p["display_name"], str(e)[:200]))
        time.sleep(0.5)
    cur.close(); conn.close()
    summary["sent_count"] = len(summary["sent"])
    return summary
