import requests
import wmill
import calendar
import uuid
from datetime import datetime

def main(
    customer: dict,
    billing_month: str = "2026-01",
    dry_run: bool = True
):
    """
    Process a single autopay customer: find maint invoices, charge card, 
    create payment, send receipt, update Airtable.
    """
    
    result = {
        "customer_name": customer["name"],
        "qbo_customer_id": customer["qbo_customer_id"],
        "airtable_record_id": customer["airtable_record_id"],
        "status": None,
        "amount_charged": None,
        "invoices_paid": [],
        "other_open_invoices": [],
        "notes": [],
        "dry_run": dry_run
    }
    
    # Parse billing month
    year, month = map(int, billing_month.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    target_date = f"{year}-{month:02d}-{last_day:02d}"
    month_name = datetime.strptime(billing_month, "%Y-%m").strftime("%B %Y")
    result["target_invoice_date"] = target_date
    
    # =========================================
    # STEP 1: Initialize QBO connection
    # =========================================
    try:
        resource_path = "u/carter/quickbooks_api"
        resource = wmill.get_resource(resource_path)
        
        response = requests.post(
            "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
            auth=(resource["client_id"], resource["client_secret"])
        )
        
        if not response.ok:
            result["status"] = "error"
            result["notes"].append(f"QBO token refresh failed: {response.text}")
            return result
        
        tokens = response.json()
        access_token = tokens["access_token"]
        
        resource["refresh_token"] = tokens["refresh_token"]
        wmill.set_resource(resource_path, resource)
        
        realm_id = resource["realm_id"]
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        
    except Exception as e:
        result["status"] = "error"
        result["notes"].append(f"QBO connection error: {str(e)}")
        return result
    
    qbo_id = customer["qbo_customer_id"]
    
    # =========================================
    # STEP 2: Get customer details & email
    # =========================================
    try:
        cust_resp = requests.get(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/customer/{qbo_id}",
            headers=headers
        )
        
        if cust_resp.ok:
            cust_data = cust_resp.json().get("Customer", {})
            result["customer_email"] = cust_data.get("PrimaryEmailAddr", {}).get("Address")
        else:
            result["notes"].append("Could not fetch customer email")
            
    except Exception as e:
        result["notes"].append(f"Customer fetch warning: {str(e)}")
    
    # =========================================
    # STEP 3: Get customer's open invoices
    # =========================================
    try:
        query = f"SELECT * FROM Invoice WHERE CustomerRef = '{qbo_id}' AND Balance > '0'"
        
        inv_resp = requests.get(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
            headers=headers,
            params={"query": query}
        )
        
        if not inv_resp.ok:
            result["status"] = "error"
            result["notes"].append("Failed to fetch invoices")
            return result
        
        all_invoices = inv_resp.json().get("QueryResponse", {}).get("Invoice", [])
        
    except Exception as e:
        result["status"] = "error"
        result["notes"].append(f"Invoice fetch error: {str(e)}")
        return result
    
    # =========================================
    # STEP 4: Identify maintenance invoices
    # =========================================
    maint_invoices = []
    other_invoices = []
    
    for inv in all_invoices:
        inv_info = {
            "id": inv.get("Id"),
            "doc_number": inv.get("DocNumber"),
            "date": inv.get("TxnDate"),
            "balance": float(inv.get("Balance", 0)),
            "sync_token": inv.get("SyncToken")
        }
        
        if inv.get("TxnDate") == target_date:
            maint_invoices.append(inv_info)
        else:
            other_invoices.append(inv_info)
    
    result["other_open_invoices"] = other_invoices
    
    if other_invoices:
        other_inv_nums = [inv["doc_number"] for inv in other_invoices]
        result["notes"].append(f"Has {len(other_invoices)} other open invoice(s): {', '.join(other_inv_nums)}")
    
    # No maintenance invoices = mark as no_invoice
    if not maint_invoices:
        result["status"] = "no_invoice"
        result["notes"].append(f"No maintenance invoice found for {month_name} (expected date: {target_date})")
        
        if not dry_run:
            _update_airtable(customer["airtable_record_id"], result)
        
        return result
    
    total_maint_balance = sum(inv["balance"] for inv in maint_invoices)
    result["amount_to_charge"] = total_maint_balance
    result["maint_invoices"] = maint_invoices
    
    # =========================================
    # STEP 5: Get payment method on file (card or bank account)
    # =========================================
    try:
        # Check for cards first
        cards_resp = requests.get(
            f"https://api.intuit.com/quickbooks/v4/customers/{qbo_id}/cards",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Request-Id": str(uuid.uuid4())
            }
        )
        
        cards = cards_resp.json() if cards_resp.ok else []
        active_card = None
        for card in cards:
            if card.get("status") == "ACTIVE":
                if card.get("default") or active_card is None:
                    active_card = card
        
        # Check for bank accounts (ACH)
        banks_resp = requests.get(
            f"https://api.intuit.com/quickbooks/v4/customers/{qbo_id}/bank-accounts",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Request-Id": str(uuid.uuid4())
            }
        )
        
        banks = banks_resp.json() if banks_resp.ok else []
        active_bank = None
        for bank in banks:
            # Bank accounts use verificationStatus instead of status
            if bank.get("verificationStatus") in ["VERIFIED", "NOT_VERIFIED"]:
                if bank.get("default") or active_bank is None:
                    active_bank = bank
        
        # Determine which payment method to use (prefer card over ACH)
        if active_card:
            result["payment_method"] = "card"
            result["payment_info"] = {
                "id": active_card.get("id"),
                "type": active_card.get("cardType"),
                "last4": active_card.get("number", "")[-4:],
                "exp": f"{active_card.get('expMonth')}/{active_card.get('expYear')}"
            }
        elif active_bank:
            result["payment_method"] = "ach"
            result["payment_info"] = {
                "id": active_bank.get("id"),
                "type": "ACH",
                "last4": active_bank.get("accountNumber", "")[-4:],
                "bank_name": active_bank.get("bankName", "Bank")
            }
        else:
            result["status"] = "payment_issue"
            result["notes"].append("No active card or bank account on file")
            if not dry_run:
                _update_airtable(customer["airtable_record_id"], result)
            return result
        
    except Exception as e:
        result["status"] = "payment_issue"
        result["notes"].append(f"Payment method check error")
        if not dry_run:
            _update_airtable(customer["airtable_record_id"], result)
        return result
    
    # =========================================
    # STEP 6: Charge card or bank account
    # =========================================
    payment_method = result.get("payment_method")
    payment_info = result.get("payment_info", {})
    
    if dry_run:
        if payment_method == "card":
            result["notes"].append(f"DRY RUN: Would charge ${total_maint_balance:.2f} to {payment_info['type']} ending {payment_info['last4']}")
        else:
            result["notes"].append(f"DRY RUN: Would charge ${total_maint_balance:.2f} via ACH ending {payment_info['last4']}")
    else:
        try:
            if payment_method == "card":
                # Charge credit/debit card
                charge_payload = {
                    "amount": f"{total_maint_balance:.2f}",
                    "currency": "USD",
                    "capture": True,
                    "cardOnFile": payment_info.get("id"),
                    "context": {
                        "mobile": False,
                        "isEcommerce": True
                    },
                    "description": f"Monthly Pool Maintenance - {month_name}"
                }
                
                charge_resp = requests.post(
                    "https://api.intuit.com/quickbooks/v4/payments/charges",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "Request-Id": str(uuid.uuid4())
                    },
                    json=charge_payload
                )
            else:
                # Charge via ACH/eCheck
                charge_payload = {
                    "amount": f"{total_maint_balance:.2f}",
                    "bankAccountOnFile": payment_info.get("id"),
                    "description": f"Monthly Pool Maintenance - {month_name}",
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
                        "Request-Id": str(uuid.uuid4())
                    },
                    json=charge_payload
                )
            
            # First check HTTP status for network/auth errors
            if not charge_resp.ok:
                result["status"] = "payment_issue"
                try:
                    err = charge_resp.json()
                    err_msg = err.get("errors", [{}])[0].get("message", "Payment failed")
                except:
                    err_msg = f"HTTP {charge_resp.status_code}"
                result["notes"].append(f"Charge failed: {err_msg}")
                _update_airtable(customer["airtable_record_id"], result)
                return result
            
            charge_data = charge_resp.json()
            
            # CRITICAL: Check the status field in response body
            # QBO returns HTTP 201 even for declined cards!
            # Valid success statuses: CAPTURED (cards), PENDING (ACH)
            charge_status = charge_data.get("status", "").upper()
            
            if payment_method == "card" and charge_status != "CAPTURED":
                result["status"] = "payment_issue"
                result["notes"].append(f"Card {charge_status}: {payment_info['type']} ending {payment_info['last4']}")
                _update_airtable(customer["airtable_record_id"], result)
                return result
            elif payment_method == "ach" and charge_status not in ["PENDING", "SUCCEEDED"]:
                result["status"] = "payment_issue"
                result["notes"].append(f"ACH {charge_status}: account ending {payment_info['last4']}")
                _update_airtable(customer["airtable_record_id"], result)
                return result
            
            result["charge_id"] = charge_data.get("id")
            
            if payment_method == "card":
                result["notes"].append(f"Charged ${total_maint_balance:.2f} to {payment_info['type']} ending {payment_info['last4']} - Charge ID: {charge_data.get('id')}")
            else:
                result["notes"].append(f"ACH initiated ${total_maint_balance:.2f} from account ending {payment_info['last4']} - Transaction ID: {charge_data.get('id')}")
            
        except Exception as e:
            result["status"] = "payment_issue"
            result["notes"].append(f"Charge error")
            _update_airtable(customer["airtable_record_id"], result)
            return result
    
    # =========================================
    # STEP 7: Create payment record in QBO
    # =========================================
    if dry_run:
        result["notes"].append(f"DRY RUN: Would create payment for invoice(s): {', '.join([inv['doc_number'] for inv in maint_invoices])}")
    else:
        try:
            # Build payment with line items linking to each invoice
            lines = []
            for inv in maint_invoices:
                lines.append({
                    "Amount": inv["balance"],
                    "LinkedTxn": [{
                        "TxnId": inv["id"],
                        "TxnType": "Invoice"
                    }]
                })
        
            pm_id = "20" if payment_method == "ach" else "21"
            payment_payload = {
                "CustomerRef": {"value": qbo_id},
                "TotalAmt": total_maint_balance,
                "Line": lines,
                "PrivateNote": f"{month_name.split()[0]} Pool Maintenance - Autopay - {payment_info['type']} ending {payment_info['last4']} - ID: {result.get('charge_id', 'N/A')}",
                "PaymentMethodRef": {"value": pm_id},
                "CreditCardPayment": {
                    "CreditChargeInfo": {"ProcessPayment": True, "Amount": total_maint_balance},
                    "CreditChargeResponse": {"Status": "Completed", "CCTransId": result.get("charge_id", "")}
                },
                "TxnSource": "IntuitPayment"
            }
            
            payment_resp = requests.post(
                f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment",
                headers={**headers, "Content-Type": "application/json"},
                json=payment_payload
            )
            
            if not payment_resp.ok:
                result["status"] = "error"
                result["notes"].append("CRITICAL: Card charged but payment record failed - needs manual review")
                _update_airtable(customer["airtable_record_id"], result)
                return result
            
            payment_data = payment_resp.json().get("Payment", {})
            result["payment_id"] = payment_data.get("Id")
            result["notes"].append(f"Payment record created: #{payment_data.get('Id')}")
            
        except Exception as e:
            result["status"] = "error"
            result["notes"].append(f"Payment record error: {str(e)}")
            result["notes"].append("CRITICAL: Card may have been charged but payment record failed")
            _update_airtable(customer["airtable_record_id"], result)
            return result
    
    # =========================================
    # STEP 8: Send email receipt & invoices
    # =========================================
    result["emailed"] = False
    
    if dry_run:
        result["notes"].append(f"DRY RUN: Would email payment receipt and invoice(s) to {result.get('customer_email', 'N/A')}")
    else:
        customer_email = result.get("customer_email")
        
        if not customer_email:
            result["notes"].append("Email skipped - no email on file")
        else:
            try:
                payment_email_ok = False
                invoice_email_ok = False
                
                # Send payment receipt
                if result.get("payment_id"):
                    send_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment/{result['payment_id']}/send?sendTo={customer_email}"
                    email_resp = requests.post(
                        send_url,
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Accept": "application/json",
                            "Content-Type": "application/octet-stream"
                        }
                    )
                    payment_email_ok = email_resp.ok
                
                # Send each invoice
                invoices_sent = 0
                for inv in maint_invoices:
                    send_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/invoice/{inv['id']}/send?sendTo={customer_email}"
                    inv_resp = requests.post(
                        send_url,
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Accept": "application/json",
                            "Content-Type": "application/octet-stream"
                        }
                    )
                    if inv_resp.ok:
                        invoices_sent += 1
                
                invoice_email_ok = invoices_sent == len(maint_invoices)
                
                if payment_email_ok and invoice_email_ok:
                    result["emailed"] = True
                    result["notes"].append(f"Emailed to {customer_email}")
                elif payment_email_ok or invoice_email_ok:
                    result["notes"].append("Some emails sent")
                else:
                    result["notes"].append("Email failed")
                    
            except Exception as e:
                result["notes"].append("Email error")
    
    # =========================================
    # STEP 9: Mark as completed
    # =========================================
    result["status"] = "completed" if not dry_run else "dry_run_success"
    result["amount_charged"] = total_maint_balance
    result["invoices_paid"] = [inv["doc_number"] for inv in maint_invoices]
    
    if not dry_run:
        _update_airtable(customer["airtable_record_id"], result)
    else:
        result["notes"].append("DRY RUN: Would update Airtable record as Completed")
    
    return result


def _update_airtable(record_id: str, result: dict):
    """Update Airtable record with processing results"""
    try:
        airtable_resource = wmill.get_resource("u/carter/airtable")
        api_key = airtable_resource.get("apiKey")
        if isinstance(api_key, str) and api_key.startswith("$var:"):
            api_key = wmill.get_variable(api_key.replace("$var:", ""))
        
        base_id = "apppQeFQh1Mi6Mv3p"
        table_id = "tbl5l8R6on9W0uiIN"
        
        # Build update payload based on status
        fields = {
            "Last Run": datetime.now().strftime("%Y-%m-%d"),
            "Notes": "\n".join(result.get("notes", []))
        }
        
        status = result.get("status")
        
        if status == "completed":
            fields["Completed"] = True
            fields["Amount"] = result.get("amount_charged")
            fields["Invoice(s)"] = ", ".join(result.get("invoices_paid", []))
            fields["Emailed"] = result.get("emailed", False)
        elif status == "no_invoice":
            fields["No Invoice"] = True
        elif status == "payment_issue":
            fields["Payment Issue"] = True
        elif status == "error":
            fields["Payment Issue"] = True
        
        requests.patch(
            f"https://api.airtable.com/v0/{base_id}/{table_id}/{record_id}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={"fields": fields}
        )
        
    except Exception as e:
        result["notes"].append(f"Airtable update error: {str(e)}")
