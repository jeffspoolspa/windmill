# requirements:
# requests

# One-off QBO customer cleanup: align ShipAddr (service address) to ION's service
# address for customers whose recurring task mis-linked due to a wrong/typo QBO addr.
# QBO ShipAddr quirk: Line1=name, Line2=street, Line3=city/state/zip (see
# f/qbo/qbo_customer_sync.extract_street). We rewrite the STREET line only.
# Token-safe per quickbooks-windmill skill: refresh + SAVE new refresh_token.
# dry_run=True (default) -> fetch + show before/after, write nothing.

import json
import requests
import wmill

QBO_RES = "u/carter/quickbooks_api"

# qbo_customer_id -> change. 'street' rewrites the street line to ION's service addr.
# 'typo' replaces a substring anywhere in the address (PARRISH 'CICLE'->'CIRCLE').
UPDATES = {
    "4072": {"street": "116 LINWOOD COURT"},   # HEATON: ION services 116 Linwood (QBO had 116 CARGO LANE)
    "20":   {"street": "69 THORNHILL DR"},      # DEZEREAUX: ION says 69 THORNHILL DR (QBO had 69 THORNHILL ROAD)
    "9731": {"typo": ["CICLE", "CIRCLE"]},      # PARRISH: fix street typo
}


def _refresh(res_path):
    r = wmill.get_resource(res_path)
    resp = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": r["refresh_token"]},
        auth=(r["client_id"], r["client_secret"]),
    )
    if not resp.ok:
        raise Exception(f"token refresh failed: {resp.status_code} {resp.text}")
    tok = resp.json()
    r["refresh_token"] = tok["refresh_token"]
    wmill.set_resource(res_path, r)        # CRITICAL: save rotated token
    return tok["access_token"], r


def main(dry_run: bool = True):
    at, res = _refresh(QBO_RES)
    realm = res["realm_id"]
    base = f"https://quickbooks.api.intuit.com/v3/company/{realm}"
    H = {"Authorization": f"Bearer {at}", "Accept": "application/json", "Content-Type": "application/json"}
    out = []
    for cid, change in UPDATES.items():
        g = requests.get(f"{base}/customer/{cid}", headers=H, params={"minorversion": "65"})
        if not g.ok:
            out.append({"qbo_customer_id": cid, "error": f"GET {g.status_code} {g.text[:200]}"})
            continue
        cust = g.json()["Customer"]
        ship = dict(cust.get("ShipAddr") or {})
        before = dict(ship)
        if "street" in change:
            if (ship.get("Line2") or "").strip():     # Line2 is street when Line1 is a name
                ship["Line2"] = change["street"]
            else:
                ship["Line1"] = change["street"]
        if "typo" in change:
            old, new = change["typo"]
            for k in ("Line1", "Line2", "Line3", "Line4", "Line5"):
                if ship.get(k) and old in ship[k]:
                    ship[k] = ship[k].replace(old, new)
        rec = {"qbo_customer_id": cid, "name": cust.get("DisplayName"),
               "before_ship": before, "after_ship": ship}
        if not dry_run:
            payload = {"Id": cust["Id"], "SyncToken": cust["SyncToken"], "sparse": True, "ShipAddr": ship}
            p = requests.post(f"{base}/customer", headers=H, params={"minorversion": "65"}, data=json.dumps(payload))
            rec["write_status"] = p.status_code
            if not p.ok:
                rec["write_error"] = p.text[:300]
        out.append(rec)
    return {"dry_run": dry_run, "updates": out}
