import requests
import wmill
from supabase import create_client
from concurrent.futures import ThreadPoolExecutor
import time

def get_access_token():
    client_id = wmill.get_variable("u/ZOHO/CLIENT_ID")
    client_secret = wmill.get_variable("u/ZOHO/CLIENT_SECRET")
    refresh_token = wmill.get_variable("u/ZOHO/REFRESH_TOKEN")
    token_url = "https://accounts.zoho.com/oauth/v2/token"
    
    data = {
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token"
    }
    
    token_response = requests.post(token_url, data=data)
    return token_response.json().get("access_token")

def fetch_item_detail(item, org_id, access_token, zoho_location_id):
    try:
        response = requests.get(
            f"https://www.zohoapis.com/inventory/v1/items/{item['item_id']}?organization_id={org_id}",
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"}
        )
        
        # Check for rate limit or errors
        if response.status_code == 429:
            print(f"RATE LIMIT hit on {item['sku']}")
            time.sleep(2)  # Wait 2 seconds
            return fetch_item_detail(item, org_id, access_token, zoho_location_id)  # Retry
        
        if response.status_code != 200:
            print(f"ERROR: API returned {response.status_code} for {item['sku']}: {response.text[:200]}")
            return None
        
        detail = response.json()
        
        if detail.get('code') != 0:
            print(f"ERROR: Zoho error for {item['sku']}: {detail.get('message')}")
            return None
            
        if "item" not in detail:
            print(f"ERROR: No 'item' key for {item['sku']}")
            return None
            
        locations = detail["item"].get("locations", [])
        location_stock = next(
            (w for w in locations if w["location_id"] == zoho_location_id),
            None
        )
        
        if location_stock and location_stock["location_stock_on_hand"] > 0:
            return {
                "item_id": item["item_id"],
                "sku": item["sku"],
                "snapshot_qoh": location_stock["location_stock_on_hand"],
                "available_qoh": location_stock["location_actual_available_stock"]
            }
    except Exception as e:
        print(f"EXCEPTION on {item['sku']}: {e}")
        return None

def create_snapshot(org_id, access_token, zoho_location_id, event_id):
    print(f"Starting snapshot for event {event_id}, location {zoho_location_id}")
    
    # 1. Get all items with pagination
    all_items = []
    page = 1
    per_page = 1000
    
    while True:
        items_response = requests.get(
            f"https://www.zohoapis.com/inventory/v1/items?organization_id={org_id}&page={page}&per_page={per_page}",
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"}
        )
        data = items_response.json()
        items = data.get("items", [])
        
        if not items:
            break
            
        all_items.extend(items)
        print(f"Fetched page {page}: {len(items)} items (total: {len(all_items)})")
        
        if not data.get("page_context", {}).get("has_more_page", False):
            break
            
        page += 1
    
    print(f"Total items retrieved: {len(all_items)}")
    
    # 2. Filter to only items with stock
    items_with_stock = [item for item in all_items if item.get('stock_on_hand', 0) > 0]
    print(f"Items with stock: {len(items_with_stock)} (skipping {len(all_items) - len(items_with_stock)})")

    if items_with_stock:
        first_item = items_with_stock[0]
        test_detail = requests.get(
            f"https://www.zohoapis.com/inventory/v1/items/{first_item['item_id']}?organization_id={org_id}",
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"}
        ).json()
        print(f"DEBUG - First item detail response keys: {test_detail.keys()}")
        if "item" in test_detail:
            print(f"DEBUG - Item has locations: {test_detail['item'].get('locations', [])[:2]}")
        else:
            print(f"DEBUG - Full response: {test_detail}")
    
    # 3. Fetch details only for items with stock
    snapshots = []
    with ThreadPoolExecutor(max_workers=2) as executor:  # Reduced from 10
        futures = [
            executor.submit(fetch_item_detail, item, org_id, access_token, zoho_location_id) 
            for item in items_with_stock
        ]
        
        for i, future in enumerate(futures, 1):
            if i % 100 == 0:
                print(f"Processed {i}/{len(items_with_stock)}, found {len(snapshots)} items")
                time.sleep(10)  # Brief pause every 100 items
            
            result = future.result()
            if result:
                result["event_id"] = event_id
                snapshots.append(result)
    
    print(f"Snapshot complete: {len(snapshots)} items with stock at location")
    return snapshots

def main(event_id: int):
    supabase = create_client(
        wmill.get_variable("f/SUPABASE/URL"),
        wmill.get_variable("f/SUPABASE/ANON_KEY")
    )

    result = (
        supabase.table("inventory_count_events")
        .select("locations(zoho_location_id)")
        .eq("id", event_id)
        .single()
        .execute()
    )
    
    zoho_location_id = result.data["locations"]["zoho_location_id"]
    
    ORGANIZATION_ID = "870657839"
    ACCESS_TOKEN = get_access_token()
    snapshots = create_snapshot(ORGANIZATION_ID, ACCESS_TOKEN, zoho_location_id, event_id)
    
    response = (
        supabase.table("inventory_count_snapshots")
        .insert(snapshots)
        .execute()
    )
    
    return {"success": True, "items_snapshotted": len(snapshots)}
