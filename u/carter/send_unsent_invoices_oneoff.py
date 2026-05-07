# One-shot: send the 37 paid-but-never-emailed April invoices.
#
# Context: process_invoice's charge path was sending only the payment receipt,
# never the invoice itself. Office staff manually backfilled the Apr 22-23
# cohort via the QBO UI, but the May 4 / May 7 cohorts didn't get the manual
# treatment. 38 invoices charged, customers paid, no invoice email delivered.
# 1 of those (RHODES, GREG) has no email on file and is excluded — needs a
# phone call instead. This script handles the remaining 37.
#
# Idempotent: each invoice is checked for EmailStatus=EmailSent first; if
# someone already sent it (e.g. office staff backfill), skip. Real send only
# happens for invoices QBO confirms have not been delivered.

import time
from typing import Any

import requests
import wmill

QBO_RESOURCE = "u/carter/quickbooks_api"


def refresh_qbo_token() -> tuple[str, str]:
    resource = wmill.get_resource(QBO_RESOURCE)
    resp = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token",
              "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"]),
        timeout=30,
    )
    resp.raise_for_status()
    tokens = resp.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(QBO_RESOURCE, resource)
    return tokens["access_token"], resource["realm_id"]


def fetch_invoice_status(invoice_id: str, token: str, realm: str) -> dict[str, Any]:
    r = requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm}/invoice/{invoice_id}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=30,
    )
    if not r.ok:
        return {"error": f"GET {r.status_code}: {r.text[:200]}"}
    inv = (r.json() or {}).get("Invoice") or {}
    return {
        "EmailStatus": inv.get("EmailStatus"),
        "DocNumber":   inv.get("DocNumber"),
        "CustomerRef": inv.get("CustomerRef") or {},
        "BillEmail":   (inv.get("BillEmail") or {}).get("Address"),
    }


def fetch_customer_email(customer_id: str, token: str, realm: str):
    if not customer_id:
        return None
    r = requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm}/customer/{customer_id}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=30,
    )
    if not r.ok:
        return None
    cust = (r.json() or {}).get("Customer") or {}
    return (cust.get("PrimaryEmailAddr") or {}).get("Address")


def send_invoice(invoice_id: str, email, token: str, realm: str) -> dict[str, Any]:
    url = f"https://quickbooks.api.intuit.com/v3/company/{realm}/invoice/{invoice_id}/send"
    if email:
        url += f"?sendTo={email}"
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "Content-Type": "application/octet-stream"},
        timeout=30,
    )
    if not r.ok:
        return {"sent": False, "status": r.status_code, "error": r.text[:200], "to": email}
    return {"sent": True, "status": r.status_code, "to": email}


def main(qbo_invoice_ids: list):
    """Args: qbo_invoice_ids (list of QBO Invoice Ids to send)."""
    token, realm = refresh_qbo_token()

    sent, skipped, failed = [], [], []

    for inv_id in qbo_invoice_ids:
        info = fetch_invoice_status(inv_id, token, realm)
        if "error" in info:
            failed.append({"qbo_invoice_id": inv_id, "stage": "fetch_invoice", **info})
            continue

        if info.get("EmailStatus") == "EmailSent":
            skipped.append({
                "qbo_invoice_id": inv_id,
                "doc_number":     info.get("DocNumber"),
                "reason":         "already EmailSent",
            })
            continue

        cust_id = (info.get("CustomerRef") or {}).get("value")
        email = info.get("BillEmail") or fetch_customer_email(cust_id, token, realm)

        if not email:
            failed.append({
                "qbo_invoice_id": inv_id,
                "doc_number":     info.get("DocNumber"),
                "stage":          "lookup_email",
                "error":          "no customer email on file",
            })
            continue

        result = send_invoice(inv_id, email, token, realm)
        record = {"qbo_invoice_id": inv_id, "doc_number": info.get("DocNumber"), **result}
        if result["sent"]:
            sent.append(record)
        else:
            failed.append({"stage": "send", **record})

        time.sleep(0.2)  # ~5 req/s, well under QBO's 500/min ceiling

    return {
        "total_input":    len(qbo_invoice_ids),
        "sent_count":     len(sent),
        "skipped_count":  len(skipped),
        "failed_count":   len(failed),
        "sent":           sent,
        "skipped":        skipped,
        "failed":         failed,
    }
