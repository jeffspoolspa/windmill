import requests
import wmill
import re
import uuid
import time


def main(billing_month: str = "2026-02", check_charges: bool = True):
    """
    Audit autopay records for a given billing month.
    
    Args:
        billing_month: YYYY-MM format
        check_charges: If True, also verify charge status at processor level
    
    Returns:
        Audit report with mismatches
    """

    # =========================================
    # STEP 1: Initialize connections
    # =========================================
    
    # Airtable
    at_resource = wmill.get_resource("u/carter/airtable")
    at_key = at_resource.get("apiKey") if isinstance(at_resource, dict) else at_resource
    if isinstance(at_key, str) and at_key.startswith("$var:"):
        at_key = wmill.get_variable(at_key.replace("$var:", ""))

    base_id = "apppQeFQh1Mi6Mv3p"
    table_id = "tbl5l8R6on9W0uiIN"

    # QBO
    qbo_resource_path = "u/carter/quickbooks_api"
    qbo_resource = wmill.get_resource(qbo_resource_path)

    token_resp = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": qbo_resource["refresh_token"],
        },
        auth=(qbo_resource["client_id"], qbo_resource["client_secret"]),
    )

    if not token_resp.ok:
        raise Exception(f"Token refresh failed: {token_resp.text}")

    tokens = token_resp.json()
    access_token = tokens["access_token"]

    # Save refreshed token
    qbo_resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(qbo_resource_path, qbo_resource)

    realm_id = qbo_resource["realm_id"]
    qbo_headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    # =========================================
    # STEP 2: Fetch ALL completed Airtable records
    # =========================================
    
    completed_records = []
    offset = None

    while True:
        params = {
            "pageSize": 100,
            "filterByFormula": "{Completed}",
        }
        if offset:
            params["offset"] = offset

        resp = requests.get(
            f"https://api.airtable.com/v0/{base_id}/{table_id}",
            headers={"Authorization": f"Bearer {at_key}"},
            params=params,
        )

        if not resp.ok:
            raise Exception(f"Airtable fetch failed: {resp.status_code} - {resp.text}")

        data = resp.json()

        for record in data.get("records", []):
            fields = record.get("fields", {})
            completed_records.append({
                "airtable_id": record["id"],
                "name": fields.get("Name", "Unknown"),
                "qbo_id": str(int(fields.get("QBO ID", 0))) if fields.get("QBO ID") else None,
                "notes": fields.get("Notes", ""),
                "amount": fields.get("Amount"),
                "invoices": fields.get("Invoice(s)", ""),
                "emailed": fields.get("Emailed", False),
                "last_run": fields.get("Last Run", ""),
            })

        offset = data.get("offset")
        if not offset:
            break

    print(f"Found {len(completed_records)} completed Airtable records")

    # =========================================
    # STEP 3: Parse Payment IDs and Charge IDs from Notes
    # =========================================
    
    # Pattern: "Payment record created: #57152"
    payment_id_pattern = re.compile(r"Payment record created: #(\d+)")
    # Pattern: "Charge ID: MQ0295472955"
    charge_id_pattern = re.compile(r"Charge ID:\s*([A-Za-z0-9]+)")
    # Pattern: "Transaction ID: ..." (ACH)
    txn_id_pattern = re.compile(r"Transaction ID:\s*([A-Za-z0-9]+)")

    for record in completed_records:
        notes = record["notes"]
        
        pm_match = payment_id_pattern.search(notes)
        record["payment_id"] = pm_match.group(1) if pm_match else None
        
        ch_match = charge_id_pattern.search(notes)
        txn_match = txn_id_pattern.search(notes)
        record["charge_id"] = (
            ch_match.group(1) if ch_match 
            else txn_match.group(1) if txn_match 
            else None
        )
        
        # Detect payment method from notes
        if "ACH" in notes or "Transaction ID" in notes:
            record["method"] = "ach"
        else:
            record["method"] = "card"

    records_with_payments = [r for r in completed_records if r["payment_id"]]
    records_without_payments = [r for r in completed_records if not r["payment_id"]]

    print(f"  {len(records_with_payments)} have payment IDs in notes")
    print(f"  {len(records_without_payments)} are missing payment IDs (investigate)")

    # =========================================
    # STEP 4: Verify each Payment ID exists in QBO
    # =========================================
    
    missing_payments = []
    deleted_payments = []
    valid_payments = []
    payment_check_errors = []

    for i, record in enumerate(records_with_payments):
        pid = record["payment_id"]

        try:
            resp = requests.get(
                f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment/{pid}",
                headers=qbo_headers,
            )

            if resp.ok:
                payment_data = resp.json().get("Payment", {})
                unapplied = float(payment_data.get("UnappliedAmt", 0))
                
                record["qbo_status"] = "exists"
                record["qbo_unapplied"] = unapplied
                record["qbo_total"] = float(payment_data.get("TotalAmt", 0))
                
                # Check if it has proper CreditCardPayment metadata
                ccp = payment_data.get("CreditCardPayment", {})
                has_metadata = bool(
                    ccp.get("CreditChargeInfo", {}).get("ProcessPayment")
                    and ccp.get("CreditChargeResponse", {}).get("CCTransId")
                )
                record["has_metadata"] = has_metadata
                
                # Check if linked to invoices
                lines = payment_data.get("Line", [])
                linked_invoices = []
                for line in lines:
                    for txn in line.get("LinkedTxn", []):
                        if txn.get("TxnType") == "Invoice":
                            linked_invoices.append(txn.get("TxnId"))
                record["linked_invoices"] = linked_invoices
                
                if unapplied > 0:
                    record["issue"] = f"Payment exists but ${unapplied:.2f} is unapplied"
                    missing_payments.append(record)
                else:
                    valid_payments.append(record)

            elif resp.status_code == 400:
                record["qbo_status"] = "deleted"
                record["issue"] = "Payment was DELETED from QBO"
                deleted_payments.append(record)

            else:
                record["qbo_status"] = "not_found"
                record["issue"] = f"Payment not found (HTTP {resp.status_code})"
                missing_payments.append(record)

        except Exception as e:
            record["qbo_status"] = "error"
            record["issue"] = f"Check failed: {str(e)}"
            payment_check_errors.append(record)

        # Rate limiting
        if (i + 1) % 10 == 0:
            time.sleep(1.5)
            print(f"  Checked {i + 1}/{len(records_with_payments)} payments...")

    # =========================================
    # STEP 5: Verify charges at processor level (if enabled)
    # =========================================
    
    if check_charges:
        records_to_check_charges = deleted_payments + missing_payments
        
        for record in records_to_check_charges:
            cid = record.get("charge_id")
            if not cid:
                record["charge_status"] = "no_charge_id"
                record["customer_was_charged"] = None
                continue

            try:
                if record["method"] == "card":
                    resp = requests.get(
                        f"https://api.intuit.com/quickbooks/v4/payments/charges/{cid}",
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Accept": "application/json",
                            "Request-Id": str(uuid.uuid4()),
                        },
                    )
                else:
                    resp = requests.get(
                        f"https://api.intuit.com/quickbooks/v4/payments/echecks/{cid}",
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Accept": "application/json",
                            "Request-Id": str(uuid.uuid4()),
                        },
                    )

                if resp.ok:
                    charge_data = resp.json()
                    charge_status = charge_data.get("status", "UNKNOWN").upper()
                    record["charge_status"] = charge_status
                    record["charge_amount"] = charge_data.get("amount")

                    if charge_status in ["CAPTURED", "SETTLED", "PENDING", "SUCCEEDED"]:
                        record["customer_was_charged"] = True
                    else:
                        record["customer_was_charged"] = False
                else:
                    record["charge_status"] = f"lookup_failed_{resp.status_code}"
                    record["customer_was_charged"] = None

            except Exception as e:
                record["charge_status"] = f"error: {str(e)}"
                record["customer_was_charged"] = None

            time.sleep(0.5)

    # =========================================
    # STEP 6: Build audit report
    # =========================================
    
    critical_issues = [
        r for r in (deleted_payments + missing_payments)
        if r.get("customer_was_charged") is True
    ]

    warnings = [
        r for r in (deleted_payments + missing_payments)
        if r.get("customer_was_charged") is None
    ]

    info_only = [
        r for r in (deleted_payments + missing_payments)
        if r.get("customer_was_charged") is False
    ]

    metadata_issues = [
        r for r in valid_payments
        if not r.get("has_metadata", True)
    ]

    def summarize_record(r):
        return {
            "name": r["name"],
            "qbo_customer_id": r["qbo_id"],
            "payment_id": r["payment_id"],
            "charge_id": r.get("charge_id"),
            "amount": r.get("amount"),
            "qbo_status": r.get("qbo_status"),
            "charge_status": r.get("charge_status"),
            "customer_was_charged": r.get("customer_was_charged"),
            "issue": r.get("issue"),
            "airtable_id": r["airtable_id"],
        }

    return {
        "billing_month": billing_month,
        "audit_summary": {
            "total_completed_records": len(completed_records),
            "records_with_payment_ids": len(records_with_payments),
            "records_missing_payment_ids": len(records_without_payments),
            "valid_payments_in_qbo": len(valid_payments),
            "deleted_from_qbo": len(deleted_payments),
            "missing_or_unapplied": len(missing_payments),
            "check_errors": len(payment_check_errors),
            "metadata_issues": len(metadata_issues),
        },
        "critical": {
            "description": "CUSTOMER CHARGED but QBO payment record DELETED/MISSING - needs immediate fix",
            "count": len(critical_issues),
            "records": [summarize_record(r) for r in critical_issues],
        },
        "warnings": {
            "description": "QBO payment deleted/missing, charge status UNKNOWN - needs manual verification",
            "count": len(warnings),
            "records": [summarize_record(r) for r in warnings],
        },
        "info": {
            "description": "QBO payment deleted but customer was NOT charged - Airtable is inaccurate but no money impact",
            "count": len(info_only),
            "records": [summarize_record(r) for r in info_only],
        },
        "metadata_issues": {
            "description": "Payment exists but missing CreditCardPayment metadata - won't match bank feed",
            "count": len(metadata_issues),
            "records": [summarize_record(r) for r in metadata_issues],
        },
        "no_payment_id_in_notes": {
            "description": "Airtable marked Completed but no payment ID found in Notes field",
            "count": len(records_without_payments),
            "records": [
                {
                    "name": r["name"],
                    "qbo_customer_id": r["qbo_id"],
                    "notes_preview": r["notes"][:200],
                    "airtable_id": r["airtable_id"],
                }
                for r in records_without_payments
            ],
        },
    }
