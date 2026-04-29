import requests
import wmill
from datetime import date
from supabase import create_client

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


def get_supabase_client():
    url = wmill.get_variable("f/SUPABASE/URL")
    key = wmill.get_variable("f/SUPABASE/ANON_KEY")
    return create_client(url, key)


def main(adjustments: list, zoho_location_id: str, event_id: int):
    ORGANIZATION_ID = "870657839"
    ACCESS_TOKEN = get_access_token()
    
    # Look up parent location
    supabase = get_supabase_client()
    location_result = supabase.table('locations').select('parent_location_id').eq('zoho_location_id', zoho_location_id).single().execute()
    
    parent_location_id = location_result.data.get('parent_location_id') if location_result.data else None
    header_location_id = parent_location_id or zoho_location_id
    line_item_location_id = zoho_location_id
    
    print(f"📍 Header location: {header_location_id}")
    print(f"📍 Line item location: {line_item_location_id}")
    
    url = "https://www.zohoapis.com/inventory/v1/inventoryadjustments"
    params = {'organization_id': ORGANIZATION_ID}
    headers = {
        'Authorization': f'Zoho-oauthtoken {ACCESS_TOKEN}',
        'Content-Type': 'application/json'
    }
    
    # Build line items
    line_items = []
    item_lookup = {}
    
    for row in adjustments:
        adj_qty = row.get('adjustment_qty', 0)
        item_id = str(row['zoho_item_id'])
        line_items.append({
            "item_id": int(item_id),
            "quantity_adjusted": float(adj_qty),
            "location_id": line_item_location_id
        })
        item_lookup[item_id] = {
            "sku": row.get('sku'),
            "item_name": row.get('item_name'),
            "adjustment_qty": float(adj_qty)
        }
    
    if not line_items:
        return {"success": True, "message": "No adjustments needed", "count": 0}
    
    payload = {
        "date": date.today().isoformat(),
        "reason": "Stocktaking results",
        "description": f"Inventory count event {event_id}" if event_id else "Inventory count adjustment",
        "reference_number": f"COUNT-{event_id}" if event_id else None,
        "adjustment_type": "quantity",
        "location_id": header_location_id,
        "line_items": line_items
    }
    
    response = requests.post(url, headers=headers, params=params, json=payload)
    data = response.json()

    if data.get('code') == 0:
        adj = data.get('inventory_adjustment', {})
        adjustment_id = str(adj.get('inventory_adjustment_id'))
        created_time = adj.get('created_time')  # Zoho's timestamp
        
        # Update event with adjustment ID and Zoho's created time
        supabase.table('inventory_count_events').update({
            'zoho_adjustment_id': adjustment_id,
            'status': 'adjusted',
            'adjusted_at': created_time  # Use Zoho's timestamp, not today's date
        }).eq('id', event_id).execute()
        
        print(f"✅ Created adjustment: {adjustment_id}")
        print(f"📊 Items adjusted: {len(line_items)}")
        print(f"🕐 Adjusted at: {created_time}")
        
        return {
            "success": True,
            "adjustment_id": adjustment_id,
            "items_adjusted": len(line_items),
            "adjusted_at": created_time
        }
    else:
        error_msg = data.get('message', 'Unknown error')
        raise Exception(f"Zoho adjustment failed: {error_msg}")
        
    