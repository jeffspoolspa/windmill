import requests
import psycopg2
import wmill
import uuid
import calendar
import json
import time
from datetime import datetime

def main(customer: dict, billing_month: str, access_token: str, realm_id: str, dry_run: bool = True):
    qbo_id = customer["qbo_customer_id"]
    txn_id = customer.get("transaction_id")
    db = wmill.get_resource("u/carter/supabase")
    conn = psycopg2.connect(host=db["host"], port=db["port"], dbname=db["dbname"], user=db["user"], password=db["password"])

    result = {
        "customer_name": customer["name"],
        "qbo_customer_id": qbo_id,
        "transaction_id": txn_id,
        "payment_status": customer.get("payment_status", "good"),
        "consecutive_declines": customer.get("consecutive_declines", 0),
        "status": None,
        "amount_charged": None,
        "invoices_paid": [],
        "has_outstanding": False,
        "notes": [],
        "errors": [],
        "dry_run": dry_run
    }

    headers_qbo = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    year, month = map(int, billing_month.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    target_date = f"{year}-{month:02d}-{last_day:02d}"
    month_name = datetime.strptime(billing_month, "%Y-%m").strftime("%B %Y")
    month_word = month_name.split()[0]

    # Build set of maintenance invoice IDs passed from module c (all months)
    maint_invoice_ids = set()
    for inv in customer.get("maint_invoices", []):
        if inv.get("qbo_invoice_id"):
            maint_invoice_ids.add(str(inv["qbo_invoice_id"]))

    def log_event(event_type, status_before, status_after, details=None):
        if not txn_id:
            return
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO billing.autopay_events (transaction_id, event_type, status_before, status_after, details) VALUES (%s::uuid, %s, %s, %s, %s::jsonb)",
                (txn_id, event_type, status_before, status_after, json.dumps(details or {}))
            )
            conn.commit()
        except:
            try:
                conn.rollback()
            except:
                pass

    def update_txn(new_status, **kwargs):
        if not txn_id:
            return
        try:
            cur = conn.cursor()
            sets = ["status = %s", "updated_at = now()"]
            vals = [new_status]
            now_fields = kwargs.pop("_now_fields", [])
            for k, v in kwargs.items():
                sets.append(f"{k} = %s")
                vals.append(v)
            for nf in now_fields:
                sets.append(f"{nf} = now()")
            vals.append(txn_id)
            cur.execute(f"UPDATE billing.autopay_transactions SET {', '.join(sets)} WHERE id = %s::uuid", vals)
            conn.commit()
        except:
            try:
                conn.rollback()
            except:
                pass

    def update_autopay_customer_status(new_payment_status, increment_declines=False, reset_declines=False):
        try:
            cur = conn.cursor()
            if reset_declines:
                cur.execute("UPDATE billing.autopay_customers SET payment_status = %s, consecutive_declines = 0, updated_at = now() WHERE qbo_customer_id = %s", (new_payment_status, qbo_id))
            elif increment_declines:
                cur.execute("UPDATE billing.autopay_customers SET payment_status = %s, consecutive_declines = consecutive_declines + 1, updated_at = now() WHERE qbo_customer_id = %s", (new_payment_status, qbo_id))
            else:
                cur.execute("UPDATE billing.autopay_customers SET payment_status = %s, updated_at = now() WHERE qbo_customer_id = %s", (new_payment_status, qbo_id))
            conn.commit()
        except:
            try:
                conn.rollback()
            except:
                pass

    def attempt_charge(method, method_info, amount, description):
        request_id = str(uuid.uuid4())
        if method == "card":
            payload = {"amount": f"{amount:.2f}", "currency": "USD", "capture": True, "cardOnFile": method_info["id"], "context": {"mobile": False, "isEcommerce": True}, "description": description}
            url = "https://api.intuit.com/quickbooks/v4/payments/charges"
        else:
            payload = {"amount": f"{amount:.2f}", "bankAccountOnFile": method_info["id"], "description": description, "paymentMode": "WEB", "context": {"deviceInfo": {"macAddress": "", "ipAddress": "", "longitude": "", "latitude": "", "phoneNumber": ""}}}
            url = "https://api.intuit.com/quickbooks/v4/payments/echecks"
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                resp = requests.post(url, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json", "Content-Type": "application/json", "Request-Id": request_id}, json=payload, timeout=30)
            except requests.exceptions.RequestException as e:
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                    continue
                return False, True, f"Network error: {str(e)[:200]}"
            if resp.status_code == 429:
                if attempt < max_retries:
                    time.sleep(2 ** (attempt + 1))
                    continue
                return False, True, "Rate limited (429) after retries"
            if not resp.ok:
                try:
                    err_body = resp.json()
                    err_msg = err_body.get("errors", [{}])[0].get("message", f"HTTP {resp.status_code}")
                except:
                    err_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
                return False, True, err_msg
            charge_data = resp.json()
            status = charge_data.get("status", "").upper()
            if method == "card":
                if status == "CAPTURED":
                    return True, False, charge_data
                else:
                    return False, False, f"Card charge status: {status} (expected CAPTURED)"
            else:
                if status in ("PENDING", "SUCCEEDED"):
                    return True, False, charge_data
                else:
                    return False, False, f"ACH charge status: {status} (expected PENDING or SUCCEEDED)"
        return False, True, "Max retries exceeded"

    try:
        cust_resp = requests.get(f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/customer/{qbo_id}", headers=headers_qbo, timeout=15)
        if cust_resp.ok:
            result["customer_email"] = cust_resp.json().get("Customer", {}).get("PrimaryEmailAddr", {}).get("Address")
    except:
        pass

    log_event("invoice_lookup", "pending", "pending")
    try:
        query = f"SELECT * FROM Invoice WHERE CustomerRef = '{qbo_id}' AND Balance > '0'"
        inv_resp = requests.get(f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query", headers=headers_qbo, params={"query": query}, timeout=15)
        if not inv_resp.ok:
            result["status"] = "error"
            result["notes"].append("Failed to fetch invoices")
            result["errors"].append(f"QBO invoice query failed: HTTP {inv_resp.status_code}")
            update_txn("error", error_step="invoice_lookup", error_message="QBO query failed")
            conn.close()
            return result
        all_invoices = inv_resp.json().get("QueryResponse", {}).get("Invoice", [])
    except Exception as e:
        result["status"] = "error"
        result["notes"].append(f"Invoice error: {str(e)}")
        result["errors"].append(f"Invoice lookup exception: {str(e)[:300]}")
        update_txn("error", error_step="invoice_lookup", error_message=str(e)[:500])
        conn.close()
        return result

    maint_invoices = []
    other_invoices = []
    for inv in all_invoices:
        inv_info = {"id": inv.get("Id"), "doc_number": inv.get("DocNumber"), "date": inv.get("TxnDate"), "balance": float(inv.get("Balance", 0)), "sync_token": inv.get("SyncToken")}
        if str(inv.get("Id")) in maint_invoice_ids:
            maint_invoices.append(inv_info)
        else:
            other_invoices.append(inv_info)

    if not maint_invoices:
        result["status"] = "no_invoice"
        result["notes"].append(f"No open maintenance invoices found in QBO matching billing_audit records (may have been paid since audit)")
        if other_invoices:
            result["notes"].append(f"Has {len(other_invoices)} other open invoice(s) not on autopay")
        update_txn("no_invoice")
        conn.close()
        return result

    total_charge_amount = sum(inv["balance"] for inv in maint_invoices)
    current_month_invoices = [inv for inv in maint_invoices if inv["date"] == target_date]
    outstanding_invoices = [inv for inv in maint_invoices if inv["date"] != target_date]
    maint_total = sum(inv["balance"] for inv in current_month_invoices)
    outstanding_total = sum(inv["balance"] for inv in outstanding_invoices)
    has_outstanding = len(outstanding_invoices) > 0
    result["has_outstanding"] = has_outstanding
    charge_invoice_ids = [inv["id"] for inv in maint_invoices]
    charge_invoice_numbers = [inv["doc_number"] for inv in maint_invoices]

    if has_outstanding:
        outstanding_details = ', '.join(f"{i['doc_number']} (${i['balance']:.2f})" for i in outstanding_invoices)
        result["notes"].append(f"Including {len(outstanding_invoices)} outstanding maint invoice(s) (${outstanding_total:.2f}): {outstanding_details}")

    if len(maint_invoices) > 1:
        result["notes"].append(f"Charging total ${total_charge_amount:.2f} across {len(maint_invoices)} maint invoice(s)")

    update_txn("pending", charge_amount=total_charge_amount, qbo_invoice_ids=charge_invoice_ids, qbo_invoice_numbers=charge_invoice_numbers, has_outstanding=has_outstanding, maint_amount=maint_total, outstanding_amount=outstanding_total, outstanding_invoice_count=len(outstanding_invoices))

    log_event("payment_method_lookup", "pending", "pending")
    active_card = None
    active_bank = None
    try:
        cards_resp = requests.get(f"https://api.intuit.com/quickbooks/v4/customers/{qbo_id}/cards", headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json", "Request-Id": str(uuid.uuid4())}, timeout=15)
        if cards_resp.ok:
            cards = cards_resp.json()
            if isinstance(cards, list):
                for card in cards:
                    if card.get("status") == "ACTIVE":
                        if card.get("default") or active_card is None:
                            active_card = card
        banks_resp = requests.get(f"https://api.intuit.com/quickbooks/v4/customers/{qbo_id}/bank-accounts", headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json", "Request-Id": str(uuid.uuid4())}, timeout=15)
        if banks_resp.ok:
            banks = banks_resp.json()
            if isinstance(banks, list):
                for bank in banks:
                    if bank.get("verificationStatus") in ("VERIFIED", "NOT_VERIFIED"):
                        if bank.get("default") or active_bank is None:
                            active_bank = bank
        if not active_card and not active_bank:
            result["status"] = "no_payment_method"
            result["notes"].append("No active card or bank account on file")
            update_txn("no_payment_method")
            update_autopay_customer_status("payment_issue", increment_declines=True)
            conn.close()
            return result
    except Exception as e:
        result["status"] = "error"
        result["notes"].append("Payment method check error")
        result["errors"].append(f"Payment method lookup exception: {str(e)[:300]}")
        update_txn("error", error_step="payment_method_lookup", error_message=str(e)[:500])
        conn.close()
        return result

    card_info = None
    bank_info = None
    if active_card:
        card_info = {"id": active_card.get("id"), "type": active_card.get("cardType"), "last4": active_card.get("number", "")[-4:], "exp": f"{active_card.get('expMonth')}/{active_card.get('expYear')}"}
    if active_bank:
        bank_info = {"id": active_bank.get("id"), "type": "ACH", "last4": active_bank.get("accountNumber", "")[-4:], "bank_name": active_bank.get("bankName", "Bank")}

    if has_outstanding:
        charge_description = f"Pool Maintenance - {month_name} + Outstanding Balance"
    else:
        charge_description = f"Monthly Pool Maintenance - {month_name}"

    log_event("charge_attempted", "pending", "charge_attempted", {"has_card": card_info is not None, "has_ach": bank_info is not None, "amount": total_charge_amount, "invoice_count": len(maint_invoices), "has_outstanding": has_outstanding})

    if dry_run:
        # Prefer ACH over card (lower processing fees)
        preferred = bank_info or card_info
        preferred_method = "ach" if bank_info else "card"
        result["payment_method"] = preferred_method
        result["payment_info"] = preferred
        result["notes"].append(f"DRY RUN: Would charge ${total_charge_amount:.2f} to {preferred['type']} ending {preferred['last4']} for {len(maint_invoices)} invoice(s)")
        if has_outstanding:
            result["notes"].append(f"DRY RUN: Includes ${outstanding_total:.2f} outstanding from prior months")
    else:
        charge_success = False
        charge_data = None
        used_method = None
        used_info = None
        primary_error = None
        primary_was_api_failure = False
        # ACH-first priority: try ACH before card (lower processing fees)
        if bank_info:
            update_txn("charge_attempted", payment_method="ach", card_type="ACH", last_four=bank_info["last4"])
            success, is_api_failure, data = attempt_charge("ach", bank_info, total_charge_amount, charge_description)
            if success:
                charge_success = True
                charge_data = data
                used_method = "ach"
                used_info = bank_info
            else:
                primary_error = data
                primary_was_api_failure = is_api_failure
                if is_api_failure:
                    result["notes"].append(f"ACH API failure (NOT a decline): {data}")
                    log_event("charge_api_failure", "charge_attempted", "charge_attempted", {"error": data, "method": "ach", "is_api_failure": True})
                else:
                    result["notes"].append(f"ACH declined: {data}")
                    log_event("charge_ach_declined", "charge_attempted", "charge_attempted", {"error": data, "has_card_fallback": card_info is not None})
                if card_info:
                    result["notes"].append("Attempting card fallback...")
                    success, is_api_failure_card, data = attempt_charge("card", card_info, total_charge_amount, charge_description)
                    if success:
                        charge_success = True
                        charge_data = data
                        used_method = "card"
                        used_info = card_info
                        result["notes"].append("Card fallback succeeded")
                    else:
                        if is_api_failure_card:
                            result["notes"].append(f"Card API failure: {data}")
                        else:
                            result["notes"].append(f"Card fallback also declined: {data}")
        elif card_info:
            update_txn("charge_attempted", payment_method="card", card_type=card_info["type"], last_four=card_info["last4"])
            success, is_api_failure, data = attempt_charge("card", card_info, total_charge_amount, charge_description)
            if success:
                charge_success = True
                charge_data = data
                used_method = "card"
                used_info = card_info
            else:
                primary_error = data
                primary_was_api_failure = is_api_failure
                if is_api_failure:
                    result["notes"].append(f"Card API failure: {data}")
                else:
                    result["notes"].append(f"Card declined: {data}")

        if charge_success:
            update_autopay_customer_status("good", reset_declines=True)

        if not charge_success:
            err_msg = primary_error or "Payment failed"
            if primary_was_api_failure:
                is_expired = "exp" in str(err_msg).lower() or "invalid" in str(err_msg).lower()
                new_status = "expired_card" if is_expired else "payment_issue"
                update_autopay_customer_status(new_status, increment_declines=True)
                result["status"] = "charge_api_failure"
                result["payment_method"] = "ach" if bank_info else "card"
                result["payment_info"] = bank_info or card_info
                result["charge_amount"] = total_charge_amount
                update_txn("error", error_step="charge_api_failure", error_message=err_msg, charge_amount=total_charge_amount)
                log_event("charge_api_failure", "charge_attempted", "error", {"error": err_msg, "needs_manual_review": True})
            else:
                update_autopay_customer_status("payment_issue", increment_declines=True)
                result["status"] = "charge_declined"
                result["payment_method"] = "ach" if bank_info else "card"
                result["payment_info"] = bank_info or card_info
                result["charge_amount"] = total_charge_amount
                update_txn("charge_declined", charge_error=err_msg, charge_amount=total_charge_amount)

            try:
                cur = conn.cursor()
                cur.execute("SELECT consecutive_declines FROM billing.autopay_customers WHERE qbo_customer_id = %s", (qbo_id,))
                row = cur.fetchone()
                if row:
                    result["consecutive_declines"] = row[0]
            except:
                pass

            conn.close()
            return result
        result["payment_method"] = used_method
        result["payment_info"] = used_info
        charge_id = charge_data.get("id")
        charge_status = charge_data.get("status", "").upper()
        result["charge_id"] = charge_id
        result["notes"].append(f"Charged ${total_charge_amount:.2f} via {used_method} - ID: {charge_id} ({len(maint_invoices)} invoice(s))")
        update_txn("charge_success", payment_method=used_method, card_type=used_info.get("type"), last_four=used_info.get("last4"), charge_id=charge_id, charge_status=charge_status, charge_amount=total_charge_amount, _now_fields=["charged_at"])

    if dry_run:
        result["notes"].append(f"DRY RUN: Would create payment for {len(maint_invoices)} maint invoice(s) totaling ${total_charge_amount:.2f}")
    else:
        try:
            lines = [{"Amount": inv["balance"], "LinkedTxn": [{"TxnId": inv["id"], "TxnType": "Invoice"}]} for inv in maint_invoices]
            pm_id = "20" if used_method == "ach" else "21"
            if has_outstanding:
                memo = f"{month_word} Pool Maintenance + Outstanding - Autopay - {used_info['type']} ending {used_info['last4']} - ID: {charge_id}"
            else:
                memo = f"{month_word} Pool Maintenance - Autopay - {used_info['type']} ending {used_info['last4']} - ID: {charge_id}"
            payment_payload = {"CustomerRef": {"value": qbo_id}, "TotalAmt": total_charge_amount, "Line": lines, "PrivateNote": memo, "PaymentMethodRef": {"value": pm_id}, "CreditCardPayment": {"CreditChargeInfo": {"ProcessPayment": True, "Amount": total_charge_amount}, "CreditChargeResponse": {"Status": "Completed", "CCTransId": charge_id}}, "TxnSource": "IntuitPayment"}
            payment_resp = requests.post(f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/payment", headers={**headers_qbo, "Content-Type": "application/json"}, json=payment_payload, timeout=15)
            if not payment_resp.ok:
                result["status"] = "payment_failed"
                result["notes"].append("CRITICAL: Charged but payment record failed")
                result["errors"].append(f"QBO payment create failed: {payment_resp.text[:300]}")
                update_txn("payment_failed", error_step="qbo_payment", error_message=payment_resp.text[:500])
                conn.close()
                return result
            qbo_payment = payment_resp.json().get("Payment", {})
            qbo_payment_id = qbo_payment.get("Id")
            result["payment_id"] = qbo_payment_id
            result["notes"].append(f"Payment #{qbo_payment_id} created for {len(maint_invoices)} invoice(s)")
            update_txn("payment_created", qbo_payment_id=qbo_payment_id, _now_fields=["payment_created_at"])
        except Exception as e:
            result["status"] = "payment_failed"
            result["notes"].append("CRITICAL: May have been charged but payment record failed")
            result["errors"].append(f"QBO payment exception: {str(e)[:300]}")
            update_txn("payment_failed", error_step="qbo_payment", error_message=str(e)[:500])
            conn.close()
            return result

    result["status"] = "awaiting_verification" if not dry_run else "dry_run_success"
    result["amount_charged"] = total_charge_amount
    result["invoices_paid"] = charge_invoice_numbers
    result["maint_invoices"] = [i["doc_number"] for i in current_month_invoices]
    result["outstanding_invoices_charged"] = [{"doc_number": i["doc_number"], "balance": i["balance"], "date": i["date"]} for i in outstanding_invoices]
    result["maint_total"] = maint_total
    result["outstanding_total"] = outstanding_total if has_outstanding else 0
    conn.close()
    return result
