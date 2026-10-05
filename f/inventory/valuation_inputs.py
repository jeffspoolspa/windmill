"""Read-only: the Zoho Inventory inputs a quarterly inventory valuation needs.

Returns stock on hand per item at each requested location, and every bill line dated in [bills_from, bills_to]
as compact rows [date, item_id, sku, qty, rate, location_name] (Windmill caps result size; pull long ranges in parts).
Writes nothing anywhere.
"""
import time
import requests
import wmill

ORG = "870657839"
BASE = "https://www.zohoapis.com/inventory/v1"


def _token():
    r = requests.post("https://accounts.zoho.com/oauth/v2/token", data={
        "refresh_token": wmill.get_variable("u/ZOHO/REFRESH_TOKEN"),
        "client_id": wmill.get_variable("u/ZOHO/CLIENT_ID"),
        "client_secret": wmill.get_variable("u/ZOHO/CLIENT_SECRET"),
        "grant_type": "refresh_token"})
    r.raise_for_status()
    return r.json()["access_token"]


def _get(h, path, **params):
    for attempt in range(5):
        r = requests.get(f"{BASE}/{path}", headers=h, params={"organization_id": ORG, **params})
        if r.status_code == 429:
            time.sleep(10 * (attempt + 1))
            continue
        body = r.json()
        if body.get("code") != 0:
            raise Exception(f"Zoho {path} failed: {body.get('code')} {body.get('message')}")
        return body
    raise Exception(f"Zoho {path}: rate limited")


def _pages(h, path, key, **params):
    page = 1
    while True:
        body = _get(h, path, page=page, per_page=200, **params)
        yield from body.get(key, [])
        if not body.get("page_context", {}).get("has_more_page"):
            return
        page += 1
        time.sleep(0.6)


def main(location_names: list = [], bills_from: str = "2026-01-01", bills_to: str = "2026-09-30"):
    h = {"Authorization": f"Zoho-oauthtoken {_token()}"}
    locations = {l["location_name"]: l["location_id"] for l in _get(h, "locations")["locations"]}

    stock = {}
    for name in location_names:
        stock[name] = [[i["item_id"], i.get("sku"), i.get("stock_on_hand")]
                       for i in _pages(h, "items", "items", location_id=locations[name])
                       if i.get("stock_on_hand")]

    bills = list(_pages(h, "bills", "bills", date_start=bills_from, date_end=bills_to))
    lines = []
    for b in bills:
        detail = _get(h, f"bills/{b['bill_id']}")["bill"]
        for l in detail.get("line_items", []):
            lines.append([detail.get("date"), l.get("item_id"), l.get("sku"), l.get("quantity"), l.get("rate"),
                          l.get("location_name")])
        time.sleep(0.6)

    return {"stock": stock, "bills": len(bills), "bill_lines": lines}
