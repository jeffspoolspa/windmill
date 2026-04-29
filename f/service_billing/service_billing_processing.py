# Script: f/service_billing/process_invoice
# Consolidated service billing automation
# Triggered by webhook when SYNCED=Y on a row

import requests
import wmill
import psycopg2
import json
from datetime import datetime, timedelta

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def refresh_qbo_token():
    """Refresh QBO token and return access_token + realm_id"""
    resource_path = "u/carter/quickbooks_api"
    resource = wmill.get_resource(resource_path)
    
    response = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"])
    )
    
    if not response.ok:
        raise Exception(f"Token refresh failed: {response.status_code} - {response.text}")
    
    tokens = response.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(resource_path, resource)
    
    return tokens["access_token"], resource["realm_id"]


def lookup_invoice(invoice_num: str, access_token: str, realm_id: str) -> dict:
    """Find invoice in QBO by DocNumber"""
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    
    query = f"SELECT * FROM Invoice WHERE DocNumber = '{invoice_num}'"
    resp = requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
        headers=headers,
        params={"query": query}
    )
    
    if not resp.ok:
        return {"found": False, "error": f"Query failed: {resp.status_code}"}
    
    invoices = resp.json().get("QueryResponse", {}).get("Invoice", [])
    
    if not invoices:
        return {"found": False, "error": "Invoice not found in QBO"}
    
    inv = invoices[0]
    qbo_total = float(inv.get("TotalAmt", 0))
    qbo_tax = float(inv.get("TxnTaxDetail", {}).get("TotalTax", 0))
    
    return {
        "found": True,
        "invoice_id": inv.get("Id"),
        "sync_token": inv.get("SyncToken"),
        "customer_id": inv.get("CustomerRef", {}).get("value"),
        "customer_name": inv.get("CustomerRef", {}).get("name"),
        "txn_date": inv.get("TxnDate"),
        "total_amt": qbo_total,
        "tax_amt": qbo_tax,
        "subtotal": round(qbo_total - qbo_tax, 2),
        "balance": float(inv.get("Balance", 0)),
        "email_status": inv.get("EmailStatus")
    }


def analyze_credits(customer_id: str, invoice_date: str, invoice_total: float, 
                    invoice_subtotal: float, wo_num: str, deposit_amount: float, 
                    access_token: str, realm_id: str) -> dict:
    """
    Find unapplied payments that match criteria.
    
    Matching rules (in priority order):
    1. Payment ref_num matches WO number → always apply (regardless of other criteria)
    2. Must be within 30 days of invoice date
    3. Must NOT contain "maint" (case insensitive) in memo
    4. Must match one of:
       - 100% of invoice total
       - 50% of invoice total or subtotal
       - Deposit amount from sheet
    
    Returns:
        credits_to_apply: list of credits that match criteria (should be 0 or 1)
        credits_to_review: list of credits that exist but don't match
        total_to_apply: sum of credits_to_apply amounts
    """
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    
    inv_date = datetime.strptime(invoice_date, "%Y-%m-%d")
    date_start = (inv_date - timedelta(days=30)).strftime("%Y-%m-%d")
    date_end = (inv_date + timedelta(days=30)).strftime("%Y-%m-%d")
    
    credits_to_apply = []
    credits_to_review = []
    
    # Query all unapplied payments for this customer (wider date range to catch all)
    pmt_query = f"SELECT * FROM Payment WHERE CustomerRef = '{customer_id}' AND TxnDate >= '{date_start}' AND TxnDate <= '{date_end}'"
    pmt_resp = requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
        headers=headers,
        params={"query": pmt_query}
    )
    
    payments = pmt_resp.json().get("QueryResponse", {}).get("Payment", []) if pmt_resp.ok else []
    
    for pmt in payments:
        unapplied = float(pmt.get("UnappliedAmt", 0))
        if unapplied <= 0:
            continue
        
        ref_num = pmt.get("PaymentRefNum", "") or ""
        memo = pmt.get("PrivateNote", "") or ""
        pmt_date_str = pmt.get("TxnDate", "")
        
        credit_info = {
            "type": "Payment",
            "id": pmt.get("Id"),
            "amount": unapplied,
            "date": pmt_date_str,
            "ref_num": ref_num,
            "memo": memo[:50] if memo else None,
            "coverage_pct": round((unapplied / invoice_total) * 100, 1) if invoice_total > 0 else 0
        }
        
        # RULE 1: WO number match - always apply regardless of other criteria
        if wo_num and ref_num.strip() == wo_num.strip():
            credit_info["match_reason"] = f"ref matches WO# {wo_num}"
            credits_to_apply.append(credit_info)
            continue
        
        # RULE 2: Check date is within 30 days
        try:
            pmt_date = datetime.strptime(pmt_date_str, "%Y-%m-%d")
            days_diff = abs((pmt_date - inv_date).days)
            if days_diff > 30:
                credit_info["reject_reason"] = f"outside 30 days ({days_diff} days)"
                credits_to_review.append(credit_info)
                continue
        except:
            credit_info["reject_reason"] = "invalid date"
            credits_to_review.append(credit_info)
            continue
        
        # RULE 3: Exclude if memo contains "maint"
        if "maint" in memo.lower():
            credit_info["reject_reason"] = "memo contains 'maint'"
            credits_to_review.append(credit_info)
            continue
        
        # RULE 4: Check amount matching
        match_reason = None
        
        # 100% of invoice total
        if abs(unapplied - invoice_total) < 0.02:
            match_reason = "matches invoice total 100%"
        # 50% of invoice total (within 1%)
        elif abs(unapplied - (invoice_total * 0.5)) < (invoice_total * 0.01 + 0.02):
            match_reason = "matches 50% of total"
        # 50% of subtotal (within 1%)
        elif abs(unapplied - (invoice_subtotal * 0.5)) < (invoice_subtotal * 0.01 + 0.02):
            match_reason = "matches 50% of subtotal"
        # Matches deposit amount
        elif deposit_amount > 0 and abs(unapplied - deposit_amount) < 0.02:
            match_reason = f"matches deposit ${deposit_amount:.2f}"
        
        if match_reason:
            credit_info["match_reason"] = match_reason
            credits_to_apply.append(credit_info)
        else:
            credit_info["reject_reason"] = "no amount match"
            credits_to_review.append(credit_info)
    
    total_to_apply = sum(c["amount"] for c in credits_to_apply)
    
    return {
        "credits_to_apply": credits_to_apply,
        "credits_to_review": credits_to_review,
        "total_to_apply": round(total_to_apply, 2)
    }


def apply_credits(invoice_id: str, invoice_balance: float, credits_to_apply: list,
                  access_token: str, realm_id: str) -> dict:
    """Apply unapplied payments to the invoice"""
    if not credits_to_apply:
        return {"total_applied": 0, "remaining_balance": invoice_balance, "results": []}
    
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    results = []
    total_applied = 0
    remaining_balance = invoice_balance
    
    for credit in credits_to_apply:
        if remaining_balance <= 0:
            break
        
        apply_amount = min(credit["amount"], remaining_balance)
        credit_id = credit["id"]
        
        try:
            # Get current payment
            pmt_resp = requests.get(
                f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment/{credit_id}",
                headers=headers
            )
            
            if pmt_resp.ok:
                payment = pmt_resp.json().get("Payment", {})
                existing_lines = payment.get("Line", [])
                existing_lines.append({
                    "Amount": apply_amount,
                    "LinkedTxn": [{"TxnId": invoice_id, "TxnType": "Invoice"}]
                })
                payment["Line"] = existing_lines
                payment["sparse"] = True
                
                update_resp = requests.post(
                    f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment",
                    headers=headers,
                    json=payment
                )
                
                if update_resp.ok:
                    total_applied += apply_amount
                    remaining_balance -= apply_amount
                    results.append({"credit_id": credit_id, "type": "Payment", "amount": apply_amount, "success": True})
                else:
                    results.append({"credit_id": credit_id, "type": "Payment", "success": False, "error": update_resp.text[:200]})
            else:
                results.append({"credit_id": credit_id, "type": "Payment", "success": False, "error": f"Failed to fetch payment: {pmt_resp.status_code}"})
        
        except Exception as e:
            results.append({"credit_id": credit_id, "type": "Payment", "success": False, "error": str(e)})
    
    return {"total_applied": round(total_applied, 2), "remaining_balance": round(remaining_balance, 2), "results": results}


def get_customer_payment_method(customer_id: str, access_token: str) -> dict:
    """Get payment method on file: checks cards first, then bank accounts (ACH).
    Returns a unified dict with payment_type='card' or 'ach'."""
    import uuid

    # Check for cards first
    cards_resp = requests.get(
        f"https://api.intuit.com/quickbooks/v4/customers/{customer_id}/cards",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Request-Id": str(uuid.uuid4())
        }
    )

    if cards_resp.ok:
        cards = cards_resp.json() if isinstance(cards_resp.json(), list) else []
        active_cards = sorted(
            [c for c in cards if c.get("status") == "ACTIVE"],
            key=lambda c: c.get("created", ""), reverse=True
        )
        if active_cards:
            card = active_cards[0]
            return {
                "has_method": True,
                "payment_type": "card",
                "method_id": card.get("id"),
                "last4": card.get("number", "")[-4:],
                "card_type": card.get("cardType"),
                "exp_month": card.get("expMonth"),
                "exp_year": card.get("expYear"),
                "total_active": len(active_cards),
                # Legacy compat for log_to_supabase (reads has_card / card_id)
                "has_card": True,
                "card_id": card.get("id")
            }

    # No active card — check for bank accounts (ACH)
    banks_resp = requests.get(
        f"https://api.intuit.com/quickbooks/v4/customers/{customer_id}/bank-accounts",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Request-Id": str(uuid.uuid4())
        }
    )

    if banks_resp.ok:
        banks = banks_resp.json() if isinstance(banks_resp.json(), list) else []
        active_banks = [b for b in banks if b.get("verificationStatus") in ["VERIFIED", "NOT_VERIFIED"]]
        if active_banks:
            bank = next((b for b in active_banks if b.get("default")), active_banks[0])
            return {
                "has_method": True,
                "payment_type": "ach",
                "method_id": bank.get("id"),
                "last4": bank.get("accountNumber", "")[-4:],
                "bank_name": bank.get("bankName", "Bank"),
                "total_active": len(active_banks),
                "has_card": True,
                "card_id": bank.get("id")
            }

    # Nothing found
    return {
        "has_method": False,
        "has_card": False,
        "error": "No active card or bank account on file",
        "total_cards": len(cards_resp.json()) if cards_resp.ok and isinstance(cards_resp.json(), list) else 0,
        "total_banks": len(banks_resp.json()) if banks_resp.ok and isinstance(banks_resp.json(), list) else 0
    }


def charge_card(card_id: str, amount: float, invoice_num: str, customer_name: str, access_token: str) -> dict:
    """Charge a stored card via QBO Payments API"""
    import uuid
    request_id = str(uuid.uuid4())
    
    charge_payload = {
        "amount": f"{amount:.2f}",
        "currency": "USD",
        "capture": True,
        "cardOnFile": card_id,
        "context": {"mobile": False, "isEcommerce": True},
        "description": f"Invoice {invoice_num} - {customer_name}"
    }
    
    charge_resp = requests.post(
        "https://api.intuit.com/quickbooks/v4/payments/charges",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Request-Id": request_id
        },
        json=charge_payload
    )
    
    # Capture full details for logging
    response_data = {
        "request_id": request_id,
        "status_code": charge_resp.status_code,
        "card_id": card_id,
        "amount_requested": amount
    }
    
    if not charge_resp.ok:
        error_detail = charge_resp.text or f"HTTP {charge_resp.status_code}"
        try:
            error_json = charge_resp.json()
            response_data["error_response"] = error_json
            if "errors" in error_json:
                error_detail = error_json["errors"][0].get("message", error_detail)
        except:
            response_data["error_raw"] = charge_resp.text
        
        return {
            "success": False, 
            "error": error_detail,
            "details": response_data,
            "payment_type": "card"
        }
    
    result = charge_resp.json()
    charge_status = result.get("status", "").upper()
    if charge_status != "CAPTURED":
        return {
            "success": False,
            "error": f"Card {charge_status}: {result.get('status', 'unknown')}",
            "details": {"request_id": request_id, "status_code": charge_resp.status_code, "charge_status": charge_status, "response": result},
            "payment_type": "card"
        }
        
    return {
        "success": True,
        "charge_id": result.get("id"),
        "amount": float(result.get("amount", 0)),
        "auth_code": result.get("authCode"),
        "status": result.get("status"),
        "card_last4": result.get("card", {}).get("number", "")[-4:],
        "card_type": result.get("card", {}).get("cardType"),
        "created": result.get("created"),
        "request_id": request_id,
        "payment_type": "card"
    }


def charge_bank_account(bank_id: str, amount: float, invoice_num: str, customer_name: str, access_token: str) -> dict:
    """Charge a stored bank account via QBO Payments eCheck API"""
    import uuid
    request_id = str(uuid.uuid4())

    charge_payload = {
        "amount": f"{amount:.2f}",
        "bankAccountOnFile": bank_id,
        "description": f"Invoice {invoice_num} - {customer_name}",
        "paymentMode": "WEB",
        "context": {
            "deviceInfo": {
                "macAddress": "",
                "ipAddress": "",
                "longitude": "",
                "latitude": "",
                "phoneNumber": ""
            }
        }
    }

    charge_resp = requests.post(
        "https://api.intuit.com/quickbooks/v4/payments/echecks",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Request-Id": request_id
        },
        json=charge_payload
    )

    if not charge_resp.ok:
        error_detail = charge_resp.text or f"HTTP {charge_resp.status_code}"
        try:
            error_json = charge_resp.json()
            if "errors" in error_json:
                error_detail = error_json["errors"][0].get("message", error_detail)
        except:
            pass
        return {
            "success": False,
            "error": error_detail,
            "details": {"request_id": request_id, "status_code": charge_resp.status_code},
            "payment_type": "ach"
        }

    result = charge_resp.json()
    charge_status = result.get("status", "").upper()

    # ACH success = PENDING or SUCCEEDED (not CAPTURED like cards)
    if charge_status not in ["PENDING", "SUCCEEDED"]:
        return {
            "success": False,
            "error": f"ACH {charge_status}",
            "details": {"request_id": request_id, "charge_status": charge_status},
            "payment_type": "ach"
        }

    return {
        "success": True,
        "charge_id": result.get("id"),
        "amount": float(result.get("amount", 0)),
        "auth_code": result.get("authCode", ""),
        "status": result.get("status"),
        "card_last4": result.get("bankAccount", {}).get("accountNumber", "")[-4:],
        "card_type": "ACH",
        "created": result.get("created"),
        "request_id": request_id,
        "payment_type": "ach"
    }


def record_payment(customer_id: str, invoice_id: str, amount: float, charge_result: dict,
                   wo_num: str, invoice_num: str, access_token: str, realm_id: str) -> dict:
    """Record a payment in QBO linked to an invoice with full details"""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    charge_id = charge_result.get("charge_id", "")
    auth_code = charge_result.get("auth_code", "")
    card_type = charge_result.get("card_type", "")
    card_last4 = charge_result.get("card_last4", "")
    
    # Build detailed private note (single line)
    private_note = f"Auto-charge | WO# {wo_num} | Inv# {invoice_num} | Charge ID: {charge_id} | Auth: {auth_code} | {card_type} x{card_last4} | {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    
    payment_data = {
        "CustomerRef": {"value": customer_id},
        "TotalAmt": amount,
        "PaymentMethodRef": {"value": "20" if charge_result.get("payment_type") == "ach" else "21"},  # 20=ACH, 21=CC
        "PaymentRefNum": wo_num,  # Use WO number as reference for easy lookup
        "TxnDate": datetime.now().strftime("%Y-%m-%d"),
        "Line": [{
            "Amount": amount,
            "LinkedTxn": [{"TxnId": invoice_id, "TxnType": "Invoice"}]
        }],
        "PrivateNote": private_note,
        "CreditCardPayment": {
            "CreditChargeInfo": {"ProcessPayment": True, "Amount": amount},
            "CreditChargeResponse": {"Status": "Completed", "CCTransId": charge_id}
        },
        "TxnSource": "IntuitPayment"
    }
    
    create_resp = requests.post(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment",
        headers=headers,
        json=payment_data
    )
    
    if not create_resp.ok:
        return {
            "success": False, 
            "error": create_resp.text[:300],
            "status_code": create_resp.status_code
        }
    
    payment = create_resp.json().get("Payment", {})
    return {
        "success": True, 
        "payment_id": payment.get("Id"),
        "payment_ref": payment.get("PaymentRefNum"),
        "total_amt": payment.get("TotalAmt")
    }


def update_invoice(invoice_id: str, memo: str, access_token: str, realm_id: str) -> dict:
    """Update invoice with due date = today and memos. Fetches fresh sync_token to avoid stale object errors."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    # Fetch current sync_token to avoid stale object error
    inv_resp = requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/invoice/{invoice_id}",
        headers=headers
    )
    
    if not inv_resp.ok:
        return {"success": False, "error": f"Failed to fetch invoice: {inv_resp.status_code}"}
    
    current_invoice = inv_resp.json().get("Invoice", {})
    sync_token = current_invoice.get("SyncToken")
    
    update_data = {
        "Id": invoice_id,
        "SyncToken": sync_token,
        "sparse": True,
        "DueDate": datetime.now().strftime("%Y-%m-%d")
    }
    
    if memo:
        update_data["PrivateNote"] = memo  # Internal memo
        update_data["CustomerMemo"] = {"value": memo}  # Customer-facing memo on invoice
    
    update_resp = requests.post(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/invoice",
        headers=headers,
        json=update_data
    )
    
    if not update_resp.ok:
        return {"success": False, "error": update_resp.text[:300]}
    
    return {"success": True}


def send_invoice_email(invoice_id: str, customer_id: str, access_token: str, realm_id: str) -> dict:
    """Send invoice via QBO email if not already sent"""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }
    
    # Check if invoice already sent
    inv_resp = requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/invoice/{invoice_id}",
        headers=headers
    )
    
    if inv_resp.ok:
        invoice = inv_resp.json().get("Invoice", {})
        if invoice.get("EmailStatus") == "EmailSent":
            return {"success": True, "skipped": True, "reason": "Already sent"}
    
    # Get customer email
    customer_resp = requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/customer/{customer_id}",
        headers=headers
    )
    
    email_address = None
    if customer_resp.ok:
        customer = customer_resp.json().get("Customer", {})
        email_address = customer.get("PrimaryEmailAddr", {}).get("Address")
    
    # Build send URL with sendTo if we have email
    send_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/invoice/{invoice_id}/send"
    if email_address:
        send_url += f"?sendTo={email_address}"
    
    send_resp = requests.post(
        send_url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/octet-stream"
        }
    )
    
    if not send_resp.ok:
        return {"success": False, "error": send_resp.text[:300], "email_attempted": email_address}
    
    return {"success": True, "sent_to": email_address}


def send_payment_receipt(payment_id: str, customer_id: str, access_token: str, realm_id: str) -> dict:
    """Send payment receipt via QBO email"""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }
    
    # Get customer email
    customer_resp = requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/customer/{customer_id}",
        headers=headers
    )
    
    email_address = None
    if customer_resp.ok:
        customer = customer_resp.json().get("Customer", {})
        email_address = customer.get("PrimaryEmailAddr", {}).get("Address")
    
    if not email_address:
        return {"success": False, "error": "No customer email found"}
    
    # Send payment receipt
    send_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment/{payment_id}/send?sendTo={email_address}"
    
    send_resp = requests.post(
        send_url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/octet-stream"
        }
    )
    
    if not send_resp.ok:
        return {"success": False, "error": send_resp.text[:300], "email_attempted": email_address}
    
    return {"success": True, "sent_to": email_address}


def get_col_letter(headers: list, header_name: str) -> str | None:
    """Find column letter by header name"""
    for i, h in enumerate(headers):
        if str(h).upper().strip() == header_name.upper():
            col = i + 1
            letter = ""
            while col > 0:
                col, rem = divmod(col - 1, 26)
                letter = chr(65 + rem) + letter
            return letter
    return None


def update_sheet(row_number: int, synced: str | None = None, sent: str | None = None, 
                 paid: str | None = None, status: str | None = None, process: str | None = None):
    """Update Google Sheet row using dynamic column lookup"""
    gsheets = wmill.get_resource("u/carter/gsheets")
    token = gsheets.get("token")
    
    spreadsheet_id = "1uI54DP-Wj0p06G2rwNHfob6LIu150YsMokEZeT_D5tE"
    sheet_name = "All WOs"
    
    # Get headers
    resp = requests.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/'{sheet_name}'!1:1",
        headers={"Authorization": f"Bearer {token}"}
    )
    headers = resp.json().get("values", [[]])[0] if resp.ok else []
    
    updates = []
    
    if synced is not None:
        col = get_col_letter(headers, "SYNCED")
        if col:
            updates.append({"range": f"'{sheet_name}'!{col}{row_number}", "values": [[synced]]})
    
    if sent is not None:
        col = get_col_letter(headers, "SENT")
        if col:
            updates.append({"range": f"'{sheet_name}'!{col}{row_number}", "values": [[sent]]})
    
    if paid is not None:
        col = get_col_letter(headers, "PAID")
        if col:
            updates.append({"range": f"'{sheet_name}'!{col}{row_number}", "values": [[paid]]})
    
    if status is not None:
        col = get_col_letter(headers, "STATUS")
        if col:
            updates.append({"range": f"'{sheet_name}'!{col}{row_number}", "values": [[status]]})
    
    if process is not None:
        col = get_col_letter(headers, "PROCESS")
        if col:
            updates.append({"range": f"'{sheet_name}'!{col}{row_number}", "values": [[process]]})
    
    if updates:
        requests.post(
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values:batchUpdate",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"valueInputOption": "USER_ENTERED", "data": updates}
        )


def log_to_supabase(data: dict):
    """Log processing attempt to Supabase"""
    supabase = wmill.get_resource("u/carter/supabase")
    
    conn = psycopg2.connect(
        host=supabase["host"],
        port=supabase["port"],
        dbname=supabase["dbname"],
        user=supabase["user"],
        password=supabase["password"]
    )
    
    # Map our data to actual table columns
    credits_applied = data.get("credits_applied")
    charge_result = data.get("card_charge_result") or {}
    
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO invoice_processing_log 
            (invoice_number, row_number, customer_name, payment_method,
             qbo_invoice_id, qbo_customer_id, sheet_subtotal, qbo_subtotal,
             subtotal_match, credits_found, credit_applied_id, credit_applied_amount, credit_decision,
             card_on_file, payment_attempted, payment_successful, payment_amount, payment_error,
             invoice_email_sent, status, error_message, processed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """, (
            data.get("invoice_number"),
            data.get("row_number"),
            data.get("customer_name"),
            data.get("payment_method"),
            data.get("qbo_invoice_id"),
            data.get("qbo_customer_id"),
            data.get("sheet_subtotal"),
            data.get("qbo_subtotal"),
            # subtotal_match
            data.get("qbo_subtotal") is not None and data.get("sheet_subtotal") is not None and abs((data.get("qbo_subtotal") or 0) - (data.get("sheet_subtotal") or 0)) < 0.02,
            # credits_found (jsonb)
            json.dumps(data.get("credits_analyzed")) if data.get("credits_analyzed") else None,
            # credit_applied_id
            credits_applied[0]["credit_id"] if credits_applied else None,
            # credit_applied_amount
            credits_applied[0]["amount"] if credits_applied else None,
            # credit_decision
            "applied" if credits_applied else "none",
            # card_on_file
            data.get("card_info", {}).get("has_card", False) if data.get("card_info") else None,
            # payment_attempted
            data.get("card_charge_attempted", False),
            # payment_successful
            charge_result.get("success", False),
            # payment_amount
            data.get("card_charge_amount"),
            # payment_error
            charge_result.get("error") if not charge_result.get("success") else None,
            # invoice_email_sent
            data.get("email_sent", False),
            # status
            data.get("status"),
            # error_message
            data.get("error_message")
        ))
        conn.commit()
    conn.close()


# =============================================================================
# MAIN FUNCTION
# =============================================================================

def main(
    row_number: int,
    invoice_num: str,
    wo_num: str,
    customer: str,
    subtotal: float,
    payment_method: str,
    deposit: float,
    description: str,
) -> dict:
    """
    Process a single invoice from webhook trigger.
    """
    # Initialize result with all fields for comprehensive logging
    result = {
        "invoice_number": invoice_num,
        "row_number": row_number,
        "customer_name": customer,
        "payment_method": payment_method,
        "sheet_subtotal": subtotal,
        "status": None,
        "error_message": None,
        "qbo_invoice_id": None,
        "qbo_customer_id": None,
        "qbo_subtotal": None,
        "qbo_balance": None,
        "credits_analyzed": None,
        "credits_applied": None,
        "card_info": None,
        "card_charge_attempted": False,
        "card_charge_amount": None,
        "card_charge_result": None,
        "payment_recorded_id": None,
        "receipt_sent": False,
        "email_sent": False
    }
    
    try:
        # =================================================================
        # STEP 1: QBO Authentication
        # =================================================================
        access_token, realm_id = refresh_qbo_token()
        
        # =================================================================
        # STEP 2: Lookup Invoice
        # =================================================================
        invoice = lookup_invoice(invoice_num, access_token, realm_id)
        
        if not invoice["found"]:
            result["status"] = "Error - Not Found"
            result["error_message"] = invoice.get("error", "Invoice not found")
            update_sheet(row_number, status=result["status"], process="FALSE")
            log_to_supabase(result)
            return result
        
        result["qbo_invoice_id"] = invoice["invoice_id"]
        result["qbo_customer_id"] = invoice["customer_id"]
        result["qbo_subtotal"] = invoice["subtotal"]
        result["qbo_balance"] = invoice["balance"]
        
        # =================================================================
        # STEP 2b: Check if already processed (invoice already sent)
        # =================================================================
        if invoice.get("email_status") == "EmailSent":
            is_paid = invoice["balance"] == 0
            result["status"] = "Sent & Paid" if is_paid else "Sent"
            result["email_sent"] = True
            update_sheet(row_number, synced="TRUE", sent="TRUE", paid="TRUE" if is_paid else "FALSE", 
                        status=result["status"], process="TRUE")
            log_to_supabase(result)
            return result
        
        # =================================================================
        # STEP 3: Validate Subtotal
        # =================================================================
        if abs(invoice["subtotal"] - subtotal) >= 0.02:
            result["status"] = "Error - Subtotal Mismatch"
            result["error_message"] = f"Sheet ${subtotal:.2f} vs QBO ${invoice['subtotal']:.2f}"
            update_sheet(row_number, status=result["status"], process="FALSE")
            log_to_supabase(result)
            return result
        
        # =================================================================
        # STEP 4: Analyze Credits
        # =================================================================
        credits = analyze_credits(
            customer_id=invoice["customer_id"],
            invoice_date=invoice["txn_date"],
            invoice_total=invoice["total_amt"],
            invoice_subtotal=invoice["subtotal"],
            wo_num=wo_num,
            deposit_amount=deposit,
            access_token=access_token,
            realm_id=realm_id
        )
        
        # Store all credits analyzed for logging
        result["credits_analyzed"] = {
            "to_apply": credits["credits_to_apply"],
            "to_review": credits["credits_to_review"],
            "total_to_apply": credits["total_to_apply"]
        }
        
        # =================================================================
        # STEP 5: Payment Method Specific Logic
        # =================================================================
        remaining_balance = invoice["balance"]
        total_credit = credits["total_to_apply"]
        has_credits_to_apply = len(credits["credits_to_apply"]) > 0
        has_unmatched_credits = len(credits["credits_to_review"]) > 0
        
        # =================================================================
        # STEP 5a: Credit Validation (for all payment methods, if balance > 0)
        # =================================================================
        
        if remaining_balance > 0:
            # CHECK: Multiple matching credits - can't auto-decide
            if len(credits["credits_to_apply"]) > 1:
                result["status"] = "Error - Multiple Credits"
                result["error_message"] = f"Multiple credits match: {[c['id'] for c in credits['credits_to_apply']]}"
                update_sheet(row_number, status=result["status"], process="FALSE")
                log_to_supabase(result)
                return result
            
            # CHECK: Credits exist but none match our criteria - need manual review
            if has_unmatched_credits and not has_credits_to_apply:
                result["status"] = "Error - Credit Review"
                result["error_message"] = f"Credits exist but none match: {[c['id'] for c in credits['credits_to_review']]}"
                update_sheet(row_number, status=result["status"], process="FALSE")
                log_to_supabase(result)
                return result
        
        # =================================================================
        # STEP 5b: INVOICE - Just send (apply credit if exists)
        # =================================================================
        if payment_method == "INVOICE":
            # Apply credit if one exists
            if has_credits_to_apply and remaining_balance > 0:
                apply_result = apply_credits(
                    invoice_id=invoice["invoice_id"],
                    invoice_balance=invoice["balance"],
                    credits_to_apply=credits["credits_to_apply"],
                    access_token=access_token,
                    realm_id=realm_id
                )
                result["credits_applied"] = apply_result["results"]
            # No card charge for INVOICE - just proceed to send
        
        # =================================================================
        # STEP 5c: PREPAID - Must have credit covering full balance
        # =================================================================
        elif payment_method == "PREPAID":
            if remaining_balance > 0:
                # Must have a credit that covers the balance
                if not has_credits_to_apply:
                    result["status"] = "Error - No Prepayment"
                    result["error_message"] = "PREPAID requires a matching prepayment credit"
                    update_sheet(row_number, status=result["status"], process="FALSE")
                    log_to_supabase(result)
                    return result
                
                # Check if credit covers full balance (99-101%)
                coverage = (total_credit / invoice["total_amt"]) * 100 if invoice["total_amt"] > 0 else 0
                if not (99 <= coverage <= 101):
                    result["status"] = "Error - Partial Prepayment"
                    result["error_message"] = f"Credit covers {coverage:.1f}%, need 100%"
                    update_sheet(row_number, status=result["status"], process="FALSE")
                    log_to_supabase(result)
                    return result
                
                # Apply the credit
                apply_result = apply_credits(
                    invoice_id=invoice["invoice_id"],
                    invoice_balance=invoice["balance"],
                    credits_to_apply=credits["credits_to_apply"],
                    access_token=access_token,
                    realm_id=realm_id
                )
                result["credits_applied"] = apply_result["results"]
        
        # =================================================================
        # STEP 5d: RUN UPON COMPLETION - Apply credit if exists, charge remainder
        # =================================================================
        elif payment_method == "RUN UPON COMPLETION":
            charge_amount = remaining_balance - total_credit
            
            if charge_amount > 0:
                # Get payment method on file (card or ACH)
                pm = get_customer_payment_method(invoice["customer_id"], access_token)
                result["card_info"] = pm
                
                if not pm.get("has_method"):
                    result["status"] = "Error - No Payment Method"
                    result["error_message"] = pm.get("error", "No active card or bank account on file")
                    update_sheet(row_number, status=result["status"], process="FALSE")
                    log_to_supabase(result)
                    return result
                
                # Apply credits first (if any)
                if has_credits_to_apply:
                    apply_result = apply_credits(
                        invoice_id=invoice["invoice_id"],
                        invoice_balance=invoice["balance"],
                        credits_to_apply=credits["credits_to_apply"],
                        access_token=access_token,
                        realm_id=realm_id
                    )
                    result["credits_applied"] = apply_result["results"]
                    charge_amount = apply_result["remaining_balance"]
                
                # Charge card or bank account for remaining balance
                result["card_charge_attempted"] = True
                result["card_charge_amount"] = charge_amount
                
                if pm["payment_type"] == "card":
                    charge_result = charge_card(
                        card_id=pm["method_id"],
                        amount=charge_amount,
                        invoice_num=invoice_num,
                        customer_name=customer,
                        access_token=access_token
                    )
                else:
                    charge_result = charge_bank_account(
                        bank_id=pm["method_id"],
                        amount=charge_amount,
                        invoice_num=invoice_num,
                        customer_name=customer,
                        access_token=access_token
                    )
                
                result["card_charge_result"] = charge_result
                
                if not charge_result["success"]:
                    error_label = "Card Declined" if pm["payment_type"] == "card" else "ACH Failed"
                    result["status"] = f"Error - {error_label}"
                    result["error_message"] = charge_result.get("error", "Unknown error")
                    update_sheet(row_number, status=result["status"], process="FALSE")
                    log_to_supabase(result)
                    return result
                
                # SAFETY CHECK: Only record payment if charge was definitively successful
                if charge_result.get("success") != True or not charge_result.get("charge_id"):
                    result["status"] = "Error - Charge Unclear"
                    result["error_message"] = f"Charge response unclear: {charge_result}"
                    update_sheet(row_number, status=result["status"], process="FALSE")
                    log_to_supabase(result)
                    return result
                
                # Record payment in QBO
                payment_result = record_payment(
                    customer_id=invoice["customer_id"],
                    invoice_id=invoice["invoice_id"],
                    amount=charge_amount,
                    charge_result=charge_result,
                    wo_num=wo_num,
                    invoice_num=invoice_num,
                    access_token=access_token,
                    realm_id=realm_id
                )
                
                if payment_result["success"]:
                    result["payment_recorded_id"] = payment_result["payment_id"]
                    result["payment_ref"] = payment_result.get("payment_ref")
                    
                    # Send payment receipt
                    receipt_result = send_payment_receipt(
                        payment_id=payment_result["payment_id"],
                        customer_id=invoice["customer_id"],
                        access_token=access_token,
                        realm_id=realm_id
                    )
                    result["receipt_sent"] = receipt_result["success"]
                    result["receipt_sent_to"] = receipt_result.get("sent_to")
                else:
                    charge_type_label = "CARD CHARGED" if pm["payment_type"] == "card" else "ACH INITIATED"
                    result["status"] = "Error - Payment Not Recorded"
                    result["error_message"] = f"{charge_type_label} ${charge_amount:.2f} but QBO payment failed: {payment_result.get('error', 'Unknown')}"
                    update_sheet(row_number, status=result["status"], process="FALSE")
                    log_to_supabase(result)
                    return result
            
            elif has_credits_to_apply and remaining_balance > 0:
                # Credit covers full balance, just apply it
                apply_result = apply_credits(
                    invoice_id=invoice["invoice_id"],
                    invoice_balance=invoice["balance"],
                    credits_to_apply=credits["credits_to_apply"],
                    access_token=access_token,
                    realm_id=realm_id
                )
                result["credits_applied"] = apply_result["results"]
        
        # =================================================================
        # STEP 6: Update Invoice (due date + memo)
        # =================================================================
        update_result = update_invoice(
            invoice_id=invoice["invoice_id"],
            memo=description,
            access_token=access_token,
            realm_id=realm_id
        )
        
        if not update_result["success"]:
            result["status"] = "Error - Update Failed"
            result["error_message"] = update_result.get("error", "Unknown error")
            update_sheet(row_number, status=result["status"], process="FALSE")
            log_to_supabase(result)
            return result
        
        # =================================================================
        # STEP 8: Send Invoice Email
        # =================================================================
        email_result = send_invoice_email(
            invoice_id=invoice["invoice_id"],
            customer_id=invoice["customer_id"],
            access_token=access_token,
            realm_id=realm_id
        )
        result["email_sent"] = email_result["success"]
        result["email_sent_to"] = email_result.get("sent_to")
        
        if not email_result["success"] and not email_result.get("skipped"):
            # Email failed - don't mark as sent
            result["status"] = "Error - Email Failed"
            result["error_message"] = email_result.get("error", "Unknown")
            update_sheet(row_number, status=result["status"], process="FALSE")
            log_to_supabase(result)
            return result
        
        # =================================================================
        # STEP 9: Success! Re-check invoice to determine final status
        # =================================================================
        # Re-fetch invoice to get current balance (may have changed after credits applied)
        final_invoice = lookup_invoice(invoice_num, access_token, realm_id)
        final_balance = final_invoice.get("balance", remaining_balance) if final_invoice.get("found") else remaining_balance
        
        # Check if invoice is fully paid
        is_paid = final_balance == 0 or result.get("payment_recorded_id")
        
        if is_paid:
            result["status"] = "Sent & Paid"
        else:
            result["status"] = "Sent"
        
        update_sheet(row_number, synced="TRUE", sent="TRUE", paid="TRUE" if is_paid else "FALSE", 
                    status=result["status"], process="TRUE")
        log_to_supabase(result)
        
        return result
    
    except Exception as e:
        result["status"] = "Error - System"
        result["error_message"] = str(e)[:500]
        update_sheet(row_number, status=result["status"], process="FALSE")
        log_to_supabase(result)
        return result
