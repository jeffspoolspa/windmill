import wmill
from datetime import datetime, timezone, timedelta

# Token provider: a SHORT-LIVED cache in front of the ADR 012 one door.
#
# Why a cache at all: QBO's refresh token ROTATES, and ~43 scripts each used to
# refresh independently — 115 pairs of refreshes began within one second of each
# other over three days. Simultaneous refreshes race for the rotating token.
# f/qbo/api/get_access_token carries concurrent_limit=1, so delegating to it
# serializes every rotation; this cache then keeps a burst from queuing behind
# that limit.
#
# Why the cache is SMALL: it previously served any token with 5 minutes of
# nominal life left, i.e. up to ~55 minutes old. On 2026-08-05 that handed a
# 49-minute-old token to pull_customer_payment_methods and QBO answered
# 401 AuthenticationFailed — the nominal 3600s lifetime is not what QBO actually
# honours in practice. A token's PAPER expiry is not evidence it still works.
#
# So freshness is judged on AGE, not on the expiry QBO quotes. Ten minutes is
# long enough to collapse a bulk-capture burst into one rotation and short
# enough that a served token has never been observed to fail.
MAX_AGE = timedelta(minutes=10)
ONE_DOOR = "f/qbo/api/get_access_token"


def main():
    state = wmill.get_state() or {}
    now = datetime.now(timezone.utc)

    cached = state.get("access_token")
    realm_id = state.get("realm_id")
    issued_at = state.get("issued_at")
    if cached and realm_id and issued_at:
        if now - datetime.fromisoformat(issued_at) < MAX_AGE:
            return {"access_token": cached, "realm_id": realm_id}

    # Miss. The one door refreshes AND saves the rotated refresh token.
    result = wmill.run_script_by_path(ONE_DOOR, args={})
    access_token = result["access_token"]
    realm_id = result["realm_id"]

    wmill.set_state({
        "access_token": access_token,
        "realm_id": realm_id,
        "issued_at": now.isoformat(),
    })
    return {"access_token": access_token, "realm_id": realm_id}
