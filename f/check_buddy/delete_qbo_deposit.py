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


def main(qbo_deposit_id: str) -> dict:
    """
    Delete a QBO Deposit by ID (read for SyncToken, then POST ?operation=delete).
    Returns {success: true, deposit_id} or {success: true, already_deleted: true}.
    Raises on QBO refusal (e.g. deposit is matched in the bank feed — unmatch it in QBO first).
    """
    if not qbo_deposit_id:
        raise Exception("qbo_deposit_id is required")

    access_token, realm_id = refresh_qbo_token()
    base_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    read_resp = requests.get(f"{base_url}/deposit/{qbo_deposit_id}", headers=headers)
    if read_resp.status_code in (400, 404):
        return {"success": True, "already_deleted": True, "deposit_id": qbo_deposit_id}
    if not read_resp.ok:
        raise Exception(f"QBO read deposit failed: {read_resp.status_code} - {read_resp.text}")

    deposit = read_resp.json().get("Deposit", {})
    if not deposit:
        return {"success": True, "already_deleted": True, "deposit_id": qbo_deposit_id}

    sync_token = deposit.get("SyncToken")
    if sync_token is None:
        raise Exception("Deposit missing SyncToken — cannot delete")

    delete_resp = requests.post(
        f"{base_url}/deposit?operation=delete",
        headers=headers,
        json={"Id": qbo_deposit_id, "SyncToken": sync_token},
    )
    if not delete_resp.ok:
        raise Exception(f"QBO delete deposit failed: {delete_resp.status_code} - {delete_resp.text}")

    return {"success": True, "deposit_id": qbo_deposit_id, "total_amount": float(deposit.get("TotalAmt", 0))}
