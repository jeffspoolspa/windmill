#extra_requirements:
#requests

import requests
import wmill


def refresh_qbo_token() -> tuple[str, str]:
    resource_path = "u/carter/quickbooks_api"
    resource = wmill.get_resource(resource_path)

    response = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": resource["refresh_token"],
        },
        auth=(resource["client_id"], resource["client_secret"]),
    )

    if not response.ok:
        raise Exception(f"Token refresh failed: {response.status_code} - {response.text}")

    tokens = response.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(resource_path, resource)
    return tokens["access_token"], resource["realm_id"]


def is_object_not_found(response) -> bool:
    """True only for QBO's Object Not Found fault (code 610): the payment is really gone."""
    try:
        errors = response.json().get("Fault", {}).get("Error", [])
    except ValueError:
        return False
    return any(str(e.get("code")) == "610" for e in errors)


def main(qbo_payment_id: str) -> dict:
    """
    Delete (void) a QBO Payment by ID.

    QBO does not support a direct DELETE on Payment. The correct approach is to
    read the payment first to get its SyncToken, then POST it back with the
    sparse=true?operation=delete query parameter.

    Returns {success: true, payment_id: ...} or raises on failure.
    If the payment is already gone, returns {success: true, already_deleted: true}.
    """
    if not qbo_payment_id:
        raise Exception("qbo_payment_id is required")

    access_token, realm_id = refresh_qbo_token()
    base_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    # 1) Read the payment to get its SyncToken
    read_resp = requests.get(f"{base_url}/payment/{qbo_payment_id}", headers=headers)

    # Already deleted only on QBO's 610 fault. Anything else fails the script: reporting success
    # lets the app drop its rows while the QBO payment still exists, and a resubmit duplicates it.
    if is_object_not_found(read_resp):
        return {"success": True, "already_deleted": True, "payment_id": qbo_payment_id}

    if not read_resp.ok:
        raise Exception(f"QBO read payment failed: {read_resp.status_code} - {read_resp.text}")

    payment = read_resp.json().get("Payment", {})
    if not payment:
        raise Exception(f"QBO returned no Payment for {qbo_payment_id}: {read_resp.text[:500]}")

    sync_token = payment.get("SyncToken")
    if sync_token is None:
        raise Exception("Payment missing SyncToken — cannot delete")

    # 2) Delete via sparse update with operation=delete
    delete_resp = requests.post(
        f"{base_url}/payment?operation=delete",
        headers=headers,
        json={
            "Id": qbo_payment_id,
            "SyncToken": sync_token,
        },
    )

    if not delete_resp.ok:
        raise Exception(f"QBO delete payment failed: {delete_resp.status_code} - {delete_resp.text}")

    return {"success": True, "payment_id": qbo_payment_id}
