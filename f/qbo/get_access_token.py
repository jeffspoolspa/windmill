import requests
import wmill
from datetime import datetime, timezone, timedelta

RESOURCE_PATH = "u/carter/quickbooks_api"


def main():
    """Return a currently-valid QBO access token + realm_id for the card vault.

    Caches the access token in this script's state and only performs an OAuth
    refresh (which rotates the shared refresh token) when the cached token is
    within 5 minutes of expiry. This keeps refresh-token rotations rare.
    """
    resource = wmill.get_resource(RESOURCE_PATH)
    realm_id = resource["realm_id"]

    # 1. Serve the cached access token if it is still comfortably valid.
    state = wmill.get_state() or {}
    now = datetime.now(timezone.utc)
    cached = state.get("access_token")
    expires_at = state.get("expires_at")
    if cached and expires_at:
        if datetime.fromisoformat(expires_at) - now > timedelta(minutes=5):
            return {"access_token": cached, "realm_id": realm_id}

    # 2. Refresh. QBO rotates the refresh token — the new one MUST be saved.
    #    (Block replicated from the proven f/qbo/sync_customer_to_qbo.)
    response = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"]),
    )
    if not response.ok:
        raise Exception(f"Token refresh failed: {response.status_code} - {response.text}")

    tokens = response.json()
    access_token = tokens["access_token"]
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(RESOURCE_PATH, resource)

    # 3. Cache the new access token until just before its expiry.
    expires_in = int(tokens.get("expires_in", 3600))
    wmill.set_state({
        "access_token": access_token,
        "expires_at": (now + timedelta(seconds=expires_in)).isoformat(),
    })

    return {"access_token": access_token, "realm_id": realm_id}
