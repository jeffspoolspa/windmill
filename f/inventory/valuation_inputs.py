# extra_requirements:
# wmill
"""Read-only Zoho Inventory inputs for the quarterly valuation. The one copy of this logic: Windmill runs it as
f/inventory/valuation_inputs (deployed from this file); any other host only needs its own main() for credentials.

  location_stock(h, names)            -> {location: [[item_id, sku, stock_on_hand], ...]}   (non-zero only)
  bill_lines(h, start, end)           -> [[date, item_id, sku, qty, rate, location], ...]
  movements(h, location, since)       -> [[date, kind, ref, item_id, sku, qty], ...] stock changes at a location
                                         after `since` (adjustments, transfers in/out, bills, invoices); qty signed
Writes nothing anywhere.
"""
import json
import time
import requests
try:
    import wmill                  # present on Windmill (imported at top level so Windmill installs it); absent elsewhere
except ImportError:
    wmill = None

ORG = "870657839"
BASE = "https://www.zohoapis.com/inventory/v1"


def token(client_id, client_secret, refresh_token):
    r = requests.post("https://accounts.zoho.com/oauth/v2/token", data={
        "refresh_token": refresh_token, "client_id": client_id, "client_secret": client_secret,
        "grant_type": "refresh_token"})
    r.raise_for_status()
    return {"Authorization": f"Zoho-oauthtoken {r.json()['access_token']}"}


def get(h, path, **params):
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


def pages(h, path, key, **params):
    page = 1
    while True:
        body = get(h, path, page=page, per_page=200, **params)
        yield from body.get(key, [])
        if not body.get("page_context", {}).get("has_more_page"):
            return
        page += 1
        time.sleep(0.6)


def locations(h):
    return {l["location_name"]: l["location_id"] for l in get(h, "locations")["locations"]}


def location_stock(h, names):
    ids = locations(h)
    return {n: [[i["item_id"], i.get("sku"), i.get("stock_on_hand")]
                for i in pages(h, "items", "items", location_id=ids[n]) if i.get("stock_on_hand")]
            for n in names}


def bill_lines(h, start, end):
    out = []
    for b in pages(h, "bills", "bills", date_start=start, date_end=end):
        d = get(h, f"bills/{b['bill_id']}")["bill"]
        out += [[d.get("date"), l.get("item_id"), l.get("sku"), l.get("quantity"), l.get("rate"), l.get("location_name")]
                for l in d.get("line_items", [])]
        time.sleep(0.6)
    return out


def movements(h, location, since):
    """Every recorded stock change at `location` dated after `since` (YYYY-MM-DD): what turns today's stock back
    into the stock as of `since`."""
    end, out = time.strftime("%Y-%m-%d"), []
    for a in pages(h, "inventoryadjustments", "inventory_adjustments", date_start=since, date_end=end):
        if a["date"] <= since:
            continue
        d = get(h, f"inventoryadjustments/{a['inventory_adjustment_id']}")["inventory_adjustment"]
        out += [[d["date"], "adjustment", d.get("reference_number") or d.get("reason"), l["item_id"], l.get("sku"),
                 l.get("quantity_adjusted")] for l in d.get("line_items", []) if l.get("location_name") == location]
        time.sleep(0.6)
    for t in pages(h, "transferorders", "transfer_orders", date_start=since, date_end=end):
        if t["date"] <= since:
            continue
        d = get(h, f"transferorders/{t['transfer_order_id']}")["transfer_order"]
        sign = (1 if d.get("to_location_name") == location else 0) - (1 if d.get("from_location_name") == location else 0)
        if sign:
            out += [[d["date"], "transfer", d.get("transfer_order_number"), l["item_id"], l.get("sku"),
                     sign * (l.get("quantity_transfer") or l.get("quantity") or 0)] for l in d.get("line_items", [])]
        time.sleep(0.6)
    for date, item_id, sku, qty, _rate, loc in bill_lines(h, since, end):
        if date > since and loc == location:
            out.append([date, "bill", "", item_id, sku, qty])
    for inv in pages(h, "invoices", "invoices", date_start=since, date_end=end):
        if inv["date"] <= since or inv.get("status") in ("draft", "void"):
            continue
        d = get(h, f"invoices/{inv['invoice_id']}")["invoice"]
        out += [[d["date"], "sale", d.get("invoice_number"), l["item_id"], l.get("sku"), -(l.get("quantity") or 0)]
                for l in d.get("line_items", []) if l.get("item_id") and l.get("location_name") == location]
        time.sleep(0.6)
    return out


def main(location_names: list = [], bills_from: str = "2026-01-01", bills_to: str = "2026-09-30",
         movements_location: str = "", movements_since: str = ""):
    """Windmill entry: credentials from Windmill variables. Returns JSON text (`wmill script run -s` prints a string
    verbatim but abbreviates nested objects; Windmill caps result size, so pull long bill ranges in parts)."""
    h = token(wmill.get_variable("u/ZOHO/CLIENT_ID"), wmill.get_variable("u/ZOHO/CLIENT_SECRET"),
              wmill.get_variable("u/ZOHO/REFRESH_TOKEN"))
    out = {"stock": location_stock(h, location_names)}
    if bills_from:
        out["bill_lines"] = bill_lines(h, bills_from, bills_to)
    if movements_location:
        out["movements"] = movements(h, movements_location, movements_since)
    return json.dumps(out)
