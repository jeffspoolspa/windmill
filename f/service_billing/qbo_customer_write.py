
import requests
import wmill

# Pattern D QBO Customer write (create / sparse update). Pure QBO — the caller
# (lib/qbo/write.ts createInQbo / writeToQbo) owns the Supabase cache state machine
# and the webhook_expectations write-ahead row; this script only talks to QBO.
#
# Contract (per lib/qbo/write.ts):
#   args:    { operation: 'create'|'update', body?, changes?, entity_id?, idempotency_key? }
#   returns: { success: true, qbo_id, entity } | { success: false, error, status_code? }
#
# Rotating refresh token: we MUST persist the new refresh_token after every refresh
# (see the quickbooks-windmill skill) or the integration burns.


def _refresh(resource_path: str):
    resource = wmill.get_resource(resource_path)
    r = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"]),
    )
    if not r.ok:
        raise Exception(f"Token refresh failed: {r.status_code} - {r.text}")
    tokens = r.json()
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(resource_path, resource)  # CRITICAL: persist rotated token
    return tokens["access_token"], resource


def main(
    operation: str = "create",
    body: dict = None,
    changes: dict = None,
    entity_id: str = "",
    idempotency_key: str = "",
):
    resource_path = "u/carter/quickbooks_api"
    access_token, resource = _refresh(resource_path)
    realm_id = resource["realm_id"]
    base = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/customer?minorversion=73"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    if operation == "create":
        payload = dict(body or {})
        resp = requests.post(base, headers=headers, json=payload)

        # QBO rejects duplicate DisplayName — retry once with the street appended.
        if resp.status_code == 400 and "already being used" in resp.text.lower():
            street = ((payload.get("BillAddr") or {}).get("Line1") or "New").strip()
            payload["DisplayName"] = f"{payload.get('DisplayName', '').strip()} ({street})".strip()
            resp = requests.post(base, headers=headers, json=payload)

        if not resp.ok:
            return {"success": False, "error": resp.text[:500], "status_code": resp.status_code}
        cust = resp.json()["Customer"]
        return {"success": True, "qbo_id": cust["Id"], "entity": cust}

    if operation == "update":
        if not entity_id:
            return {"success": False, "error": "entity_id required for update"}
        # Sparse update needs the current SyncToken.
        q = requests.get(
            f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/customer/{entity_id}?minorversion=73",
            headers=headers,
        )
        if not q.ok:
            return {"success": False, "error": f"fetch for SyncToken failed: {q.text[:300]}", "status_code": q.status_code}
        existing = q.json()["Customer"]
        update_body = dict(changes or {})
        update_body.update({"Id": entity_id, "SyncToken": existing["SyncToken"], "sparse": True})
        resp = requests.post(base, headers=headers, json=update_body)
        if not resp.ok:
            return {"success": False, "error": resp.text[:500], "status_code": resp.status_code}
        cust = resp.json()["Customer"]
        return {"success": True, "qbo_id": cust["Id"], "entity": cust}

    return {"success": False, "error": f"unknown operation: {operation}"}
