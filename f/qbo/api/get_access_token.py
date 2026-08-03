# The ONE QBO auth door (ADR 012 pattern, mirroring f/ION/api/get_session).
#
# QBO refresh tokens ROTATE: every refresh returns a new one and invalidates
# the old. Two independent refreshers = a burned token and a dead integration,
# so the app NEVER refreshes — it calls this script, which refreshes and SAVES
# the rotated token in the same breath (quickbooks-windmill skill pattern).
# Serialized by concurrency key so two callers cannot race the rotation.
#
# Returns short-lived keys only: {access_token, realm_id, expires_in}.

import requests
import wmill


def main():
    resource_path = "u/carter/quickbooks_api"
    resource = wmill.get_resource(resource_path)

    response = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"]),
    )
    if not response.ok:
        raise Exception(f"Token refresh failed: {response.status_code} - {response.text[:300]}")

    tokens = response.json()

    # CRITICAL: save the rotated refresh token before anything else can fail.
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(resource_path, resource)

    return {
        "access_token": tokens["access_token"],
        "realm_id": resource["realm_id"],
        "expires_in": tokens.get("expires_in"),
    }
