import requests
import wmill
from supabase import create_client
import time
from datetime import datetime

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

def get_zoho_headers(access_token):
    return {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Content-Type': 'application/json'
    }

def fetch_all_transfer_orders(organization_id, access_token):
    """Fetch all transfer orders, filtering out drafts"""
    transfer_orders = []
    page = 1
    
    while True:
        url = "https://www.zohoapis.com/inventory/v1/transferorders"
        params = {
            'organization_id': organization_id,
            'page': page,
            'per_page': 200
        }
        
        response = requests.get(url, headers=get_zoho_headers(access_token), params=params)
        data = response.json()
        
        if data['code'] != 0:
            break
            
        batch = data.get('transfer_orders', [])
        if not batch:
            break
        
        # Filter out drafts
        published = [to for to in batch if to.get('status') == 'transferred']
        transfer_orders.extend(published)
        print(f"📦 Page {page}: {len(transfer_orders)} transfer orders (excluding drafts)")
        time.sleep(0.6)
        page += 1
    
    return transfer_orders

def fetch_all_local_transfers(supabase):
    """Fetch all local transfer records grouped by to_number"""
    transfers = []
    start = 0
    
    while True:
        result = (
            supabase.table('transfers')
            .select('to_number, line_item_id')
            .eq('source', 'zoho')
            .range(start, start + 999)
            .execute()
        )
        
        if not result.data:
            break
        transfers.extend(result.data)
        start += 1000
    
    return transfers

def get_transfer_order_details(transfer_order_id, organization_id, access_token):
    url = f"https://www.zohoapis.com/inventory/v1/transferorders/{transfer_order_id}"
    params = {'organization_id': organization_id}
    
    response = requests.get(url, headers=get_zoho_headers(access_token), params=params)
    data = response.json()
    
    if data['code'] != 0:
        return None
    return data['transfer_order']

def build_item_mapping(supabase):
    all_items = []
    start = 0
    
    while True:
        result = supabase.table('items').select('zoho_item_id, sku, lou_sku').range(start, start + 999).execute()
        if not result.data:
            break
        all_items.extend(result.data)
        start += 1000
    
    return {str(item['zoho_item_id']): item for item in all_items if item.get('zoho_item_id')}

def build_location_mapping(supabase):
    result = supabase.table('locations').select('id, zoho_location_id, lou_stock_site').execute()
    return {str(loc['zoho_location_id']): loc for loc in result.data if loc.get('zoho_location_id')}

def transfer_to_records(to_details, zoho_item_map, zoho_location_map):
    records = []
    
    to_date = None
    if to_details.get('date'):
        try:
            to_date = datetime.strptime(to_details['date'], '%Y-%m-%d').date().isoformat()
        except:
            pass
    
    zoho_from_location_id = str(to_details.get('from_location_id', ''))
    zoho_to_location_id = str(to_details.get('to_location_id', ''))
    
    from_location = zoho_location_map.get(zoho_from_location_id)
    to_location = zoho_location_map.get(zoho_to_location_id)
    
    from_location_id = from_location['id'] if from_location else None
    to_location_id = to_location['id'] if to_location else None
    
    for line_item in to_details.get('line_items', []):
        zoho_item_id = str(line_item.get('item_id', ''))
        local_item = zoho_item_map.get(zoho_item_id)
        
        if not local_item:
            continue
        
        records.append({
            'to_number': to_details.get('transfer_order_number'),
            'date': to_date,
            'from_location': from_location_id,
            'to_location': to_location_id,
            'item_id': local_item['zoho_item_id'],
            'line_item_id': line_item.get('line_item_id'),
            'sku': local_item.get('sku') or local_item.get('lou_sku'),
            'quantity': float(line_item.get('quantity_transfer', 0)),
            'source': 'zoho',
            'created_time': to_details.get('created_time')
        })
    
    return records

def main():
    print("🚀 Starting transfer order reconciliation...")
    
    supabase = create_client(
        wmill.get_variable("f/SUPABASE/URL"),
        wmill.get_variable("f/SUPABASE/ANON_KEY")
    )
    
    ORGANIZATION_ID = "870657839"
    ACCESS_TOKEN = get_access_token()
    
    # Build mappings
    zoho_item_map = build_item_mapping(supabase)
    zoho_location_map = build_location_mapping(supabase)
    
    # Fetch all transfer orders from Zoho (excluding drafts)
    transfer_orders = fetch_all_transfer_orders(ORGANIZATION_ID, ACCESS_TOKEN)
    zoho_to_numbers = {to['transfer_order_number'] for to in transfer_orders}
    zoho_to_by_number = {to['transfer_order_number']: to for to in transfer_orders}
    print(f"📊 Zoho transfer orders: {len(zoho_to_numbers)}")
    
    # Fetch all local transfers and group by to_number
    local_transfers = fetch_all_local_transfers(supabase)
    local_to_numbers = {t['to_number'] for t in local_transfers if t.get('to_number')}
    print(f"📊 Local transfer orders: {len(local_to_numbers)}")
    
    # Find differences
    missing_locally = zoho_to_numbers - local_to_numbers
    extra_locally = local_to_numbers - zoho_to_numbers
    
    print(f"\n🆕 Missing locally: {len(missing_locally)}")
    print(f"🗑️  Extra locally (to delete): {len(extra_locally)}")
    
    # Delete extra transfers (drafts or deleted in Zoho)
    deleted = 0
    for to_number in extra_locally:
        supabase.table('transfers').delete().eq('to_number', to_number).eq('source', 'zoho').execute()
        deleted += 1
        print(f"🗑️  Deleted: {to_number}")
    print(f"🗑️  Total deleted: {deleted}")
    
    # Insert missing transfers
    inserted = 0
    for i, to_number in enumerate(missing_locally):
        print(f"⚙️  Processing {i+1}/{len(missing_locally)}: {to_number}")
        
        to_data = zoho_to_by_number.get(to_number)
        if not to_data:
            continue
            
        details = get_transfer_order_details(to_data['transfer_order_id'], ORGANIZATION_ID, ACCESS_TOKEN)
        if details:
            records = transfer_to_records(details, zoho_item_map, zoho_location_map)
            if records:
                supabase.table('transfers').upsert(records, on_conflict='line_item_id').execute()
                inserted += len(records)
        
        time.sleep(0.6)
    
    print(f"\n🎉 Done!")
    print(f"🗑️  Deleted: {deleted} transfer orders")
    print(f"💾 Inserted: {inserted} line items")
    
    return {
        "zoho_transfer_orders": len(zoho_to_numbers),
        "local_transfer_orders": len(local_to_numbers),
        "missing": len(missing_locally),
        "extra": len(extra_locally),
        "deleted": deleted,
        "inserted": inserted
    }