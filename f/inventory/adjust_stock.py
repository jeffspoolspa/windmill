# extra_requirements:
# wmill
"""Write: post a quantity inventory adjustment in Zoho Inventory at one location. The one copy of this logic;
Windmill runs it as f/inventory/adjust_stock (deployed from this file).

  post_adjustment(h, location_id, date, lines, reference, description)
      lines = [[item_id, quantity_adjusted], ...] (signed change, not the new quantity); zero lines are dropped;
      items Zoho can't adjust (deleted, inactive, not stock-tracked) are skipped and reported; the rest post in
      batches of BATCH lines, one adjustment each, sharing the reference. Returns (adjustment ids, skipped).

main() defaults to a dry run: it returns what it would post and posts nothing until dry_run is false.
"""
import json
import requests
try:
    import wmill                                          # on Windmill only
    from f.inventory.valuation_inputs import ORG, BASE, token, locations, get
except ImportError:
    wmill = None
    from zoho_inputs import ORG, BASE, token, locations, get   # the same code, from the task folder
from concurrent.futures import ThreadPoolExecutor

BATCH = 100


def adjustable(h, item_ids):
    """Zoho refuses a whole adjustment if one item is deleted, inactive or not stock-tracked: find those first.
    Returns {item_id: reason} for the ones that can't be adjusted."""
    def check(i):
        try:
            it = get(h, f"items/{i}")["item"]
        except Exception as e:
            return i, f"not in Zoho ({str(e)[-40:]})"
        if it.get("status") != "active":
            return i, f"inactive: {it.get('name')}"
        if it.get("item_type") != "inventory" or not it.get("track_inventory", True):
            return i, f"not stock-tracked: {it.get('name')}"
        return i, None
    with ThreadPoolExecutor(4) as ex:
        return {i: why for i, why in ex.map(check, item_ids) if why}


def post_adjustment(h, location_id, date, lines, reference, description):
    """Returns (adjustment ids, {item_id: reason skipped})."""
    lines = [[str(i), float(q)] for i, q in lines if float(q)]
    skipped = adjustable(h, [i for i, _ in lines])
    lines = [l for l in lines if l[0] not in skipped]
    ids = []
    for n in range(0, len(lines), BATCH):
        payload = {"date": date, "reason": "Stocktaking results", "adjustment_type": "quantity",
                   "reference_number": reference, "description": description, "location_id": location_id,
                   "line_items": [{"item_id": i, "quantity_adjusted": q, "location_id": location_id}
                                  for i, q in lines[n:n + BATCH]]}
        r = requests.post(f"{BASE}/inventoryadjustments", headers={**h, "Content-Type": "application/json"},
                          params={"organization_id": ORG}, json=payload)
        body = r.json()
        if body.get("code") != 0:
            raise Exception(f"Zoho adjustment failed after {len(ids)} posted ({ids}): {body.get('code')} "
                            f"{body.get('message')}")
        ids.append(body["inventory_adjustment"]["inventory_adjustment_id"])
    return ids, skipped


def main(location_name: str, date: str, reference: str, description: str, lines: list, dry_run: bool = True):
    h = token(wmill.get_variable("u/ZOHO/CLIENT_ID"), wmill.get_variable("u/ZOHO/CLIENT_SECRET"),
              wmill.get_variable("u/ZOHO/REFRESH_TOKEN"))
    location_id = locations(h)[location_name]
    nonzero = [l for l in lines if float(l[1])]
    summary = {"location": location_name, "location_id": location_id, "date": date, "reference": reference,
               "lines": len(nonzero), "units_up": sum(float(q) for _, q in nonzero if float(q) > 0),
               "units_down": sum(float(q) for _, q in nonzero if float(q) < 0), "dry_run": dry_run}
    if dry_run:
        summary["would_skip"] = adjustable(h, [str(i) for i, _ in nonzero])
    else:
        summary["adjustment_ids"], summary["skipped"] = post_adjustment(h, location_id, date, nonzero, reference,
                                                                         description)
    return json.dumps(summary)
