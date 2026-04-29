#extra_requirements:
#google-auth==2.38.0
#pyasn1==0.6.1
#pyasn1-modules==0.4.1
#cachetools==5.5.2
#rsa==4.9
#requests==2.32.5
#charset-normalizer==3.4.4

import wmill
import base64
import time
import urllib.parse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleRequest
import httpx
import psycopg2
import psycopg2.extras

SEND_AS = "jpsbilling@jeffspoolspa.com"
LANDING_BASE = "https://switch-to-weekly.vercel.app"
DRY_RUN = False

OFFICE_CONFIG = {
    "Jeff's Pool & Spa": {
        "company_name": "Jeff's Pool &amp; Spa Service",
        "company_phone": "(912) 554-0636",
        "company_email": "jpsbilling@jeffspoolspa.com",
        "service_area": "Serving GA, FL, and SC Coast",
        "from_name": "Jeff's Pool & Spa Service",
        "cc": None, "reply_to": None,
    },
    "Perfect Pools": {
        "company_name": "Perfect Pools",
        "company_phone": "(912) 459-2486",
        "company_email": "info@perfectpoolscleaning.com",
        "service_area": "Serving the Richmond Hill &amp; Hinesville Area",
        "from_name": "Perfect Pools",
        "cc": "info@perfectpoolscleaning.com",
        "reply_to": "info@perfectpoolscleaning.com",
    },
}

TEMPLATE = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin: 0; padding: 0; font-family: Arial, sans-serif; background-color: #ffffff;">
<div style="background-color: rgb(15, 23, 42); padding: 32px 24px; text-align: center;">
  <h1 style="margin: 0; font-size: 24px; font-weight: 700; color: rgb(255, 255, 255); letter-spacing: -0.5px;">{{COMPANY_NAME}}</h1>
  <p style="margin: 8px 0 0; font-size: 14px; color: rgb(148, 163, 184);">Important Update About Your Pool Service</p>
</div>
<div style="padding: 32px 24px;">
  <p style="margin: 0 0 24px; font-size: 16px; line-height: 1.6; color: rgb(55, 65, 81);">Hi {{FIRST_NAME}},</p>
  <p style="margin: 0 0 24px; font-size: 16px; line-height: 1.6; color: rgb(55, 65, 81);">As temperatures rise and spring pollen picks up, we're reaching out to all bi-weekly maintenance customers with an important update about your service heading into the warmer months.</p>
  <div style="background-color: rgb(239, 246, 255); border: 2px solid rgb(59, 130, 246); border-radius: 8px; padding: 20px; margin: 24px 0;">
    <p style="margin: 0 0 12px; font-size: 18px; font-weight: 600; color: rgb(30, 64, 175);">Our Recommendation: Switch to Weekly Service Now</p>
    <p style="margin: 0; font-size: 15px; line-height: 1.6; color: rgb(30, 64, 175);">Spring is actually the most critical time of year for pools — even more than summer. Pollen loads are heavy, systems need to come out of winter settings, and this is when we see the most pools start to turn green.</p>
  </div>
  <div style="background-color: rgb(254, 243, 199); border: 2px solid rgb(245, 158, 11); border-radius: 8px; padding: 20px; margin: 24px 0;">
    <p style="margin: 0 0 12px; font-size: 17px; font-weight: 600; color: rgb(146, 64, 14);">If You Stay on Bi-Weekly: Important Information</p>
    <ul style="margin: 0 0 12px; padding-left: 20px; font-size: 14px; line-height: 1.6; color: rgb(146, 64, 14);">
      <li style="margin-bottom: 8px;">You're responsible for maintaining your pool between visits (testing water, brushing, skimming).</li>
      <li style="margin-bottom: 8px;">If we arrive and your pool has turned green, normal service will be paused and the pool will need to undergo our green pool recovery process — which involves a formal estimate and money down to begin.</li>
      <li style="margin-bottom: 0;">Green pool recovery can typically cost <strong>$300+</strong> depending on severity, and the pool will be required to get on a weekly service schedule once recovered.</li>
    </ul>
    <p style="margin: 0; font-size: 14px; line-height: 1.6; color: rgb(146, 64, 14);">We want to be upfront so every customer has clear expectations about what bi-weekly service can cover during peak season.</p>
  </div>
  <h2 style="margin: 32px 0 16px; font-size: 20px; font-weight: 600; color: rgb(15, 23, 42);">What Happens in Two Weeks</h2>
  <p style="margin: 0 0 16px; font-size: 16px; line-height: 1.6; color: rgb(55, 65, 81);">Here's what's working against your pool between bi-weekly visits:</p>
  <ul style="margin: 0 0 24px; padding-left: 24px; font-size: 15px; line-height: 1.7; color: rgb(75, 85, 99);">
    <li style="margin-bottom: 12px;"><strong style="color: rgb(31, 41, 55);">Debris accumulation</strong> — Skimmer baskets become packed, which can lead to poor circulation and even pump failure.</li>
    <li style="margin-bottom: 12px;"><strong style="color: rgb(31, 41, 55);">Phosphate buildup</strong> — Debris, pollen, and organic matter that sit in the pool quickly consume your chlorine as it works to break it down, leaving phosphates behind that provide food for algae blooms.</li>
    <li style="margin-bottom: 12px;"><strong style="color: rgb(31, 41, 55);">Chlorine bottoms out</strong> — In warm weather, chlorine degrades to zero in 10–14 days. Your pool sits with no sanitizer.</li>
    <li style="margin-bottom: 0;"><strong style="color: rgb(31, 41, 55);">Equipment goes unmonitored</strong> — Pump failures or filter issues can run 12+ days undetected, turning simple fixes into expensive repairs.</li>
  </ul>
  {{PRICING_SECTION}}
  <div style="background-color: rgb(240, 253, 244); border: 2px solid rgb(34, 197, 94); border-radius: 8px; padding: 20px; margin: 24px 0;">
    <p style="margin: 0 0 8px; font-size: 17px; font-weight: 600; color: rgb(22, 101, 52);">Weekly Customers Get Our Green-Free Guarantee</p>
    <p style="margin: 0; font-size: 15px; line-height: 1.6; color: rgb(22, 101, 52);">If your pool turns green while on weekly service, we waive the labor fee for any additional visits needed to recover it. This guarantee is exclusive to weekly customers.</p>
  </div>
  <div style="text-align: center; margin: 32px 0;">
    <h3 style="margin: 0 0 16px; font-size: 18px; font-weight: 600; color: rgb(15, 23, 42);">Ready to Switch? It's Easy.</h3>
    <p style="margin: 0 0 20px; font-size: 15px; line-height: 1.6; color: rgb(75, 85, 99);">Click below or call our office. We'll get you on the next available weekly route — no contracts, no hassle.</p>
    <a href="{{SWITCH_URL}}" style="display: inline-block; background-color: rgb(59, 130, 246); color: rgb(255, 255, 255); padding: 14px 32px; text-decoration: none; border-radius: 8px; font-size: 16px; font-weight: 600;">Switch to Weekly Service</a>
  </div>
  <div style="border-top: 1px solid rgb(229, 231, 235); padding-top: 24px; margin-top: 32px;">
    <p style="margin: 0 0 16px; font-size: 15px; line-height: 1.6; color: rgb(55, 65, 81);">Thanks for being a {{COMPANY_NAME}} customer. We want to make sure your pool gets the care it deserves this season.</p>
    <p style="margin: 0; font-size: 15px; line-height: 1.6; color: rgb(55, 65, 81);">Best regards,<br>
    <strong>The {{COMPANY_NAME}} Team</strong><br>
    {{COMPANY_PHONE}}<br>
    <a href="mailto:{{COMPANY_EMAIL}}" style="color: rgb(59, 130, 246); text-decoration: none;">{{COMPANY_EMAIL}}</a></p>
  </div>
</div>
<div style="background-color: rgb(249, 250, 251); padding: 24px; text-align: center; border-top: 1px solid rgb(229, 231, 235);">
  <p style="margin: 0; font-size: 13px; color: rgb(107, 114, 128); line-height: 1.5;">{{COMPANY_NAME}}<br>
  {{SERVICE_AREA}}</p>
</div>
</body>
</html>"""

PRICING_BLOCK = """
  <h2 style="margin: 32px 0 16px; font-size: 20px; font-weight: 600; color: rgb(15, 23, 42);">Your Pricing</h2>
  <div style="background-color: rgb(249, 250, 251); border: 1px solid rgb(229, 231, 235); border-radius: 8px; padding: 24px; margin: 0 0 24px; max-width: 480px;">
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-bottom: 1px solid rgb(229, 231, 235); margin-bottom: 16px; padding-bottom: 16px;">
      <tr>
        <td style="padding-bottom: 16px;">
          <div style="font-size: 13px; color: rgb(107, 114, 128); margin-bottom: 4px;">Bi-Weekly Labor Rate</div>
          <table cellpadding="0" cellspacing="0" border="0"><tr>
            <td style="vertical-align: middle;"><div style="font-size: 20px; font-weight: 700; color: rgb(107, 114, 128); text-decoration: line-through;">{{BIWEEKLY_PRICE}}</div></td>
            <td style="vertical-align: middle; padding-left: 10px;"><div style="font-size: 13px; font-weight: 700; color: rgb(107, 114, 128); line-height: 1.2;"><span style="display: block;">Plus</span><span style="display: block;">Chemicals</span></div></td>
          </tr></table>
        </td>
        <td style="padding-bottom: 16px; text-align: right;">
          <div style="font-size: 13px; color: rgb(107, 114, 128); margin-bottom: 4px;">Monthly Labor Est.</div>
          <table cellpadding="0" cellspacing="0" border="0" style="float: right;"><tr>
            <td style="vertical-align: middle;"><div style="font-size: 20px; font-weight: 700; color: rgb(107, 114, 128);">~{{BIWEEKLY_MONTHLY}}</div></td>
            <td style="vertical-align: middle; padding-left: 10px;"><div style="font-size: 13px; font-weight: 700; color: rgb(107, 114, 128); line-height: 1.2;"><span style="display: block;">Plus</span><span style="display: block;">Chemicals</span></div></td>
          </tr></table>
        </td>
      </tr>
    </table>
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom: 16px;">
      <tr>
        <td>
          <div style="font-size: 13px; color: rgb(30, 64, 175); margin-bottom: 4px; font-weight: 600;">Weekly Labor Rate</div>
          <table cellpadding="0" cellspacing="0" border="0"><tr>
            <td style="vertical-align: middle;"><div style="font-size: 24px; font-weight: 700; color: rgb(30, 64, 175);">{{WEEKLY_PRICE}}</div></td>
            <td style="vertical-align: middle; padding-left: 10px;"><div style="font-size: 14px; font-weight: 700; color: rgb(30, 64, 175); line-height: 1.2;"><span style="display: block;">Plus</span><span style="display: block;">Chemicals</span></div></td>
          </tr></table>
        </td>
        <td style="text-align: right;">
          <div style="font-size: 13px; color: rgb(30, 64, 175); margin-bottom: 4px; font-weight: 600;">Monthly Labor Est.</div>
          <table cellpadding="0" cellspacing="0" border="0" style="float: right;"><tr>
            <td style="vertical-align: middle;"><div style="font-size: 24px; font-weight: 700; color: rgb(30, 64, 175);">~{{WEEKLY_MONTHLY}}</div></td>
            <td style="vertical-align: middle; padding-left: 10px;"><div style="font-size: 14px; font-weight: 700; color: rgb(30, 64, 175); line-height: 1.2;"><span style="display: block;">Plus</span><span style="display: block;">Chemicals</span></div></td>
          </tr></table>
        </td>
      </tr>
    </table>
    <div style="background-color: rgb(220, 252, 231); border-radius: 6px; padding: 12px; text-align: center;">
      <div style="font-size: 15px; font-weight: 600; color: rgb(21, 128, 61);">$25 less per visit + potentially reduced chemical costs</div>
    </div>
    <p style="margin: 16px 0 0; font-size: 13px; color: rgb(107, 114, 128); line-height: 1.5;">Labor rates shown do not include chemicals, which are billed separately. Monthly estimates are based on 4 visits per month, though some months may have 5. With weekly service, we maintain consistent chemistry rather than playing catch-up, which usually leads to less chemical usage per visit.</p>
  </div>"""


def main():
    sa = wmill.get_resource("u/carter/gmail_gcp_service_account")
    creds = service_account.Credentials.from_service_account_info(
        sa, scopes=["https://www.googleapis.com/auth/gmail.send"], subject=SEND_AS,
    )
    creds.refresh(GoogleRequest())

    db = wmill.get_resource("u/carter/supabase")
    conn = psycopg2.connect(
        host=db["host"], port=db.get("port", 5432), dbname=db["dbname"],
        user=db["user"], password=db["password"], sslmode=db.get("sslmode", "require"),
    )
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM switch_to_weekly_campaign WHERE status = 'pending' ORDER BY id")
    customers = cur.fetchall()
    print(f"Loaded {len(customers)} pending customers to send")

    results = {"sent": [], "failed": []}

    for i, c in enumerate(customers):
        cust_name = c["customer"]
        email_raw = c["email"]
        first_name = c["name"]
        office = c["office"]
        rate = c["rate"]
        oldrate = c["oldrate"]
        phone = c.get("phone") or ""
        price_change = c.get("price_change", True)
        to_addr = email_raw.split(",")[0].strip()
        oc = OFFICE_CONFIG.get(office, OFFICE_CONFIG["Jeff's Pool & Spa"])
        is_pp = office == "Perfect Pools"

        params = urllib.parse.urlencode({
            "name": first_name, "last": c["last"], "customer": cust_name,
            "email": to_addr, "office": office, "rate": int(rate),
            "oldrate": int(oldrate), "phone": phone,
        })
        switch_url = f"{LANDING_BASE}?{params}"

        if price_change:
            pricing = (PRICING_BLOCK
                .replace("{{BIWEEKLY_PRICE}}", f"${int(oldrate)}")
                .replace("{{WEEKLY_PRICE}}", f"${int(rate)}")
                .replace("{{BIWEEKLY_MONTHLY}}", f"${int(oldrate * 2)}")
                .replace("{{WEEKLY_MONTHLY}}", f"${int(rate * 4)}"))
        else:
            pricing = ""

        html = (TEMPLATE
            .replace("{{COMPANY_NAME}}", oc["company_name"])
            .replace("{{FIRST_NAME}}", first_name)
            .replace("{{SWITCH_URL}}", switch_url)
            .replace("{{PRICING_SECTION}}", pricing)
            .replace("{{COMPANY_PHONE}}", oc["company_phone"])
            .replace("{{COMPANY_EMAIL}}", oc["company_email"])
            .replace("{{SERVICE_AREA}}", oc["service_area"]))

        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Important Update: Your Pool Service This Spring"
        msg["To"] = to_addr
        msg["From"] = f"{oc['from_name']} <{SEND_AS}>"
        if is_pp:
            msg["Cc"] = oc["cc"]
            msg["Reply-To"] = oc["reply_to"]
        msg.attach(MIMEText(html, "html"))

        try:
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
            send_resp = httpx.post(
                f"https://gmail.googleapis.com/gmail/v1/users/{SEND_AS}/messages/send",
                headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"},
                json={"raw": raw}, timeout=30,
            )
            if send_resp.status_code == 200:
                mid = send_resp.json().get("id", "?")
                print(f"[SENT] {i+1}/{len(customers)} — {cust_name} → {to_addr} (msg:{mid})")
                cur.execute("UPDATE switch_to_weekly_campaign SET status = 'sent', sent_at = now() WHERE id = %s", (c["id"],))
                results["sent"].append(cust_name)
            else:
                print(f"[FAIL] {i+1}/{len(customers)} — {cust_name} → {to_addr} — {send_resp.status_code}: {send_resp.text[:200]}")
                results["failed"].append({"name": cust_name, "error": send_resp.text[:200]})
        except Exception as e:
            print(f"[ERROR] {i+1}/{len(customers)} — {cust_name} → {to_addr} — {str(e)}")
            results["failed"].append({"name": cust_name, "error": str(e)})

        time.sleep(1.5)

    print("\n" + "=" * 60)
    print(f"CAMPAIGN SUMMARY")
    print(f"  Total queued:  {len(customers)}")
    print(f"  Sent:          {len(results['sent'])}")
    print(f"  Failed:        {len(results['failed'])}")
    print("=" * 60)
    if results["failed"]:
        print("\nFailed sends:")
        for f in results["failed"]:
            print(f"  - {f['name']}: {f['error']}")
    cur.close()
    conn.close()
    return results
