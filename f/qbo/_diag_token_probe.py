import requests
import wmill

# Read-only diagnostic: compare the token a SUB-JOB call yields against one taken
# straight from the door, using the exact same request the failing script makes.
# Never returns the tokens themselves — only shape and outcome.
def probe(tok, label):
    at = tok.get("access_token") or ""
    r = requests.get(
        "https://api.intuit.com/quickbooks/v4/customers/10084/cards",
        headers={"Authorization": f"Bearer {at}", "Accept": "application/json"},
        timeout=30,
    )
    return {
        "source": label,
        "type": type(tok).__name__,
        "keys": sorted(tok.keys()) if isinstance(tok, dict) else None,
        "token_len": len(at),
        "has_whitespace": at != at.strip(),
        "realm": tok.get("realm_id"),
        "payments_v4_status": r.status_code,
    }

def main():
    via_wrapper = wmill.run_script_by_path("f/qbo/get_access_token", args={})
    via_door = wmill.run_script_by_path("f/qbo/api/get_access_token", args={})
    same = (via_wrapper.get("access_token") == via_door.get("access_token"))
    return {
        "wrapper": probe(via_wrapper, "f/qbo/get_access_token"),
        "door": probe(via_door, "f/qbo/api/get_access_token"),
        "same_token": same,
    }
