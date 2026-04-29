#extra_requirements:
#requests

import requests
import wmill

LATE_FEE_ITEM_ID = "5737"


def refresh_qbo_token() -> tuple[str, str]:
    resource_path = "u/carter/quickbooks_api"
    resource = wmill.get_resource(resource_path)
    response = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"]),
    )
    if not response.ok:
        raise Exception(f"Token refresh failed: {response.status_code} - {response.text}")
    tokens = response.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(resource_path, resource)
    return tokens["access_token"], resource["realm_id"]


def main(invoice_ids: list[str], customer_id: str) -> dict:
    """
    For each invoice, find any late fee lines (Item ID 5737) and remove them by:
      1. Creating a CreditMemo for the late fee amount (item 5737 line).
      2. Applying the CreditMemo to the invoice via a $0 Payment that links both.

    Returns a summary including total late fees removed and per-invoice details.
    If an invoice has no late fee lines it is skipped silently.
    """
    if not invoice_ids:
        raise Exception("invoice_ids is required")
    if not customer_id:
        raise Exception("customer_id is required")

    access_token, realm_id = refresh_qbo_token()
    base_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    results = []
    total_removed = 0.0

    for invoice_id in invoice_ids:
        # 1) Fetch full invoice
        resp = requests.get(f"{base_url}/invoice/{invoice_id}", headers=headers)
        if not resp.ok:
            results.append({"invoice_id": invoice_id, "skipped": True, "reason": f"fetch_failed_{resp.status_code}"})
            continue

        invoice = resp.json().get("Invoice", {})
        if not invoice:
            results.append({"invoice_id": invoice_id, "skipped": True, "reason": "not_found"})
            continue

        sync_token = invoice.get("SyncToken")

        # 2) Find late fee lines
        late_fee_total = 0.0
        for line in invoice.get("Line", []):
            item_ref = (line.get("SalesItemLineDetail") or {}).get("ItemRef", {})
            if str(item_ref.get("value", "")) == LATE_FEE_ITEM_ID:
                late_fee_total += float(line.get("Amount", 0))

        if late_fee_total <= 0:
            results.append({"invoice_id": invoice_id, "skipped": True, "reason": "no_late_fees"})
            continue

        late_fee_total = round(late_fee_total, 2)

        # 3) Create CreditMemo for the late fee amount
        credit_memo_body = {
            "CustomerRef": {"value": customer_id},
            "Line": [
                {
                    "Amount": late_fee_total,
                    "DetailType": "SalesItemLineDetail",
                    "SalesItemLineDetail": {
                        "ItemRef": {"value": LATE_FEE_ITEM_ID},
                        "Qty": 1,
                        "UnitPrice": late_fee_total,
                    },
                }
            ],
            "PrivateNote": f"Late fee credit — applied to invoice {invoice.get('DocNumber', invoice_id)}",
        }
        cm_resp = requests.post(f"{base_url}/creditmemo", headers=headers, json=credit_memo_body)
        if not cm_resp.ok:
            results.append({
                "invoice_id": invoice_id,
                "skipped": True,
                "reason": f"credit_memo_failed_{cm_resp.status_code}",
                "detail": cm_resp.text[:200],
            })
            continue

        credit_memo_id = cm_resp.json().get("CreditMemo", {}).get("Id")
        if not credit_memo_id:
            results.append({"invoice_id": invoice_id, "skipped": True, "reason": "credit_memo_no_id"})
            continue

        # 4) Apply CreditMemo to Invoice via $0 Payment
        # TotalAmt = invoice_line - credit_memo_line = 0
        payment_body = {
            "CustomerRef": {"value": customer_id},
            "TotalAmt": 0,
            "Line": [
                {
                    "Amount": late_fee_total,
                    "LinkedTxn": [{"TxnId": invoice_id, "TxnType": "Invoice"}],
                },
                {
                    "Amount": late_fee_total,
                    "LinkedTxn": [{"TxnId": credit_memo_id, "TxnType": "CreditMemo"}],
                },
            ],
            "PrivateNote": f"Late fee credit application — invoice {invoice.get('DocNumber', invoice_id)}",
        }
        pay_resp = requests.post(f"{base_url}/payment", headers=headers, json=payment_body)
        if not pay_resp.ok:
            results.append({
                "invoice_id": invoice_id,
                "skipped": True,
                "reason": f"apply_payment_failed_{pay_resp.status_code}",
                "detail": pay_resp.text[:200],
                "credit_memo_id": credit_memo_id,
            })
            continue

        application_payment_id = pay_resp.json().get("Payment", {}).get("Id")
        total_removed += late_fee_total

        results.append({
            "invoice_id": invoice_id,
            "invoice_number": invoice.get("DocNumber"),
            "skipped": False,
            "late_fee_removed": late_fee_total,
            "credit_memo_id": credit_memo_id,
            "application_payment_id": application_payment_id,
        })

    return {
        "success": True,
        "total_late_fees_removed": round(total_removed, 2),
        "invoices_processed": len(results),
        "results": results,
    }
