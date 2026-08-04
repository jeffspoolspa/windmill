import wmill
from datetime import datetime, timezone, timedelta

# Token provider for the card vault's `capture` edge function.
#
# This script used to perform the OAuth refresh ITSELF, against the shared
# u/carter/quickbooks_api resource. That made it a SECOND refresher of a token
# that ROTATES: QBO issues a new refresh token on every exchange and invalidates
# the old one, so two independent refreshers race and one of them ends up
# holding a dead token — taking down every QBO integration, not just the vault.
# ADR 012 says there is exactly one door. It is f/qbo/api/get_access_token, and
# that script carries concurrent_limit=1 so rotations are serialized.
#
# So this is now a CACHE in front of that one door, not a second door:
#   - serve the cached access token while it is comfortably valid, and
#   - on a miss, delegate the refresh (and the rotated-token save) downstream.
#
# The cache is what keeps the burst of captures from a bulk card-collection send
# from triggering a rotation per capture; the ADR 012 door is what guarantees
# that when a rotation does happen, only one script performs it.

ONE_DOOR = "f/qbo/api/get_access_token"
SKEW = timedelta(minutes=5)


def main():
    state = wmill.get_state() or {}
    now = datetime.now(timezone.utc)

    cached = state.get("access_token")
    realm_id = state.get("realm_id")
    expires_at = state.get("expires_at")
    if cached and realm_id and expires_at:
        if datetime.fromisoformat(expires_at) - now > SKEW:
            return {"access_token": cached, "realm_id": realm_id}

    # Miss. The one door refreshes AND saves the rotated refresh token.
    result = wmill.run_script_sync(ONE_DOOR, args={})
    access_token = result["access_token"]
    realm_id = result["realm_id"]
    expires_in = int(result.get("expires_in") or 3600)

    wmill.set_state({
        "access_token": access_token,
        "realm_id": realm_id,
        "expires_at": (now + timedelta(seconds=expires_in)).isoformat(),
    })

    return {"access_token": access_token, "realm_id": realm_id}
