# check_invoice_status
#
# Query QBO for invoice status and update Google Sheet SYNCED/SENT/PAID columns.
# Uses IN clause + MAXRESULTS for efficiency: ~7 QBO API calls instead of ~2700.
#
# Path: u/carter/check_invoice_status
# Language: python3

import requests
import wmill
from datetime import datetime

SPREADSHEET_ID = "1uI54DP-Wj0p06G2rwNHfob6LIu150YsMokEZeT_D5tE"
SHEET_NAME = "All WOs"
SHEET_BATCH_SIZE = 50
QBO_IN_BATCH_SIZE = 400  # Keep under URI length limit


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
        raise Exception(f"Token refresh failed: {response.status_code}")

    tokens = response.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(resource_path, resource)

    return tokens["access_token"], resource["realm_id"]


def batch_query_invoices(doc_numbers: list, access_token: str, realm_id: str) -> dict:
    """
    Query invoices using IN clause with MAXRESULTS.
    Returns {doc_number: {email_status, balance}}
    """
    if not doc_numbers:
        return {}

    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    result = {}

    for i in range(0, len(doc_numbers), QBO_IN_BATCH_SIZE):
        batch = doc_numbers[i:i + QBO_IN_BATCH_SIZE]
        in_values = ", ".join([f"'{d}'" for d in batch])
        query = f"SELECT DocNumber, EmailStatus, Balance FROM Invoice WHERE DocNumber IN ({in_values}) MAXRESULTS 1000"

        resp = requests.get(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
            headers=headers,
            params={"query": query}
        )

        if resp.ok:
            batch_found = 0
            for inv in resp.json().get("QueryResponse", {}).get("Invoice", []):
                doc_num = inv.get("DocNumber")
                if doc_num:
                    result[str(doc_num)] = {
                        "email_status": inv.get("EmailStatus"),
                        "balance": float(inv.get("Balance", 0))
                    }
                    batch_found += 1
            print(f"  QBO batch {i//QBO_IN_BATCH_SIZE + 1}: queried {len(batch)}, found {batch_found}")
        else:
            print(f"  QBO batch {i//QBO_IN_BATCH_SIZE + 1} failed: {resp.status_code}")

    return result


def get_sheets_token():
    """Get Google Sheets OAuth token"""
    return wmill.get_resource("u/carter/gsheets").get("token")


def read_sheet_data(token: str) -> list:
    """Read relevant columns from the sheet"""
    range_name = f"'{SHEET_NAME}'!D:R"

    resp = requests.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{range_name}",
        headers={"Authorization": f"Bearer {token}"}
    )

    if not resp.ok:
        raise Exception(f"Failed to read sheet: {resp.status_code}")

    values = resp.json().get("values", [])

    data = []
    for i, row in enumerate(values):
        if i == 0:  # Skip header
            continue

        def get_val(idx):
            return str(row[idx]).strip() if len(row) > idx and row[idx] else ""

        data.append({
            "actual_row": i + 1,
            "invoice": get_val(1),   # E - Invoice #
            "synced": get_val(12),   # P - SYNCED
            "sent": get_val(13),     # Q - SENT
            "paid": get_val(14)      # R - PAID
        })

    return data


def batch_update_sheet(token: str, updates: list):
    """Batch update status columns in the sheet"""
    if not updates:
        return

    data = []
    for u in updates:
        row = u["actual_row"]  # +1 for header
        data.append({
            "range": f"'{SHEET_NAME}'!P{row}:R{row}",
            "values": [[u["synced"], u["sent"], u["paid"]]]
        })

    for i in range(0, len(data), SHEET_BATCH_SIZE):
        batch = data[i:i + SHEET_BATCH_SIZE]

        resp = requests.post(
            f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values:batchUpdate",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            json={"valueInputOption": "USER_ENTERED", "data": batch}
        )

        if not resp.ok:
            print(f"  Sheet batch update failed: {resp.status_code}")


def main(dry_run: bool = False) -> dict:
    """
    Check invoice status for all rows with invoice numbers.

    Uses optimized IN clause queries to minimize API calls:
    - ~7 QBO API calls (400 invoices per batch)
    - ~55 Sheet API calls (50 rows per batch)

    Args:
        dry_run: If True, don't write updates to sheet

    Returns:
        Summary dict with stats
    """
    result = {"started": datetime.now().isoformat()}

    # Authenticate
    print("Authenticating...")
    access_token, realm_id = refresh_qbo_token()
    sheets_token = get_sheets_token()

    # Read sheet data
    print("Reading sheet...")
    sheet_data = read_sheet_data(sheets_token)

    # Get rows needing update (have invoice # but status not all TRUE)
    rows_to_check = []
    for r in sheet_data:
        if not r["invoice"]:
            continue
        # Skip if all status columns are already TRUE
        if (r["synced"].upper() == "TRUE" and
            r["sent"].upper() == "TRUE" and
            r["paid"].upper() == "TRUE"):
            continue
        rows_to_check.append(r)

    # Get unique invoice numbers
    invoice_numbers = list(set(r["invoice"] for r in rows_to_check))

    result["rows_to_check"] = len(rows_to_check)
    result["unique_invoices"] = len(invoice_numbers)
    result["qbo_api_calls"] = (len(invoice_numbers) + QBO_IN_BATCH_SIZE - 1) // QBO_IN_BATCH_SIZE

    print(f"Found {len(rows_to_check)} rows to check, {len(invoice_numbers)} unique invoices")
    print(f"Will use {result['qbo_api_calls']} QBO API calls")

    # Batch query QBO using IN clause
    print("Querying QBO...")
    qbo_data = batch_query_invoices(invoice_numbers, access_token, realm_id)
    result["qbo_found"] = len(qbo_data)

    # Build updates
    updates = []
    stats = {"synced": 0, "sent": 0, "paid": 0, "not_found": 0}

    for row in rows_to_check:
        inv_num = row["invoice"]

        if inv_num in qbo_data:
            inv = qbo_data[inv_num]
            synced = "TRUE"
            sent = "TRUE" if inv["email_status"] == "EmailSent" else "FALSE"
            paid = "TRUE" if inv["balance"] == 0 else "FALSE"

            stats["synced"] += 1
            if sent == "TRUE":
                stats["sent"] += 1
            if paid == "TRUE":
                stats["paid"] += 1
        else:
            synced = "FALSE"
            sent = ""
            paid = ""
            stats["not_found"] += 1

        updates.append({
            "actual_row": row["actual_row"],
            "synced": synced,
            "sent": sent,
            "paid": paid
        })

    result.update(stats)
    result["rows_updated"] = len(updates)

    # Write updates to sheet
    if not dry_run and updates:
        print(f"Writing {len(updates)} updates to sheet...")
        batch_update_sheet(sheets_token, updates)
    elif dry_run:
        print(f"DRY RUN: Would update {len(updates)} rows")

    result["completed"] = datetime.now().isoformat()
    result["dry_run"] = dry_run

    return result