import requests
import wmill
from supabase import create_client
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

def get_zoho_headers(access_token):
    return {
        'Authorization': f'Zoho-oauthtoken {access_token}',
        'Content-Type': 'application/json'
    }

def fetch_all_vendor_credits(organization_id, access_token):
    credits = []
    page = 1
    
    while True:
        url = "https://www.zohoapis.com/inventory/v1/vendorcredits"
        params = {'organization_id': organization_id, 'page': page, 'per_page': 200}
        
        response = requests.get(url, headers=get_zoho_headers(access_token), params=params)
        data = response.json()
        
        if data['code'] != 0:
            print(f"❌ API error: {data.get('message')}")
            break
            
        batch = data.get('vendor_credits', [])
        if not batch:
            break
        
        # Filter out drafts and void
        published = [vc for vc in batch if vc.get('status') not in ('draft', 'void')]
        credits.extend(published)
        print(f"📦 Page {page}: {len(credits)} vendor credits total")
        time.sleep(0.6)
        page += 1

    if credits:
        print(f"Sample keys: {credits[0].keys()}")
    return credits

def fetch_all_local_vendor_credits(supabase):
    records = []
    start = 0
    
    while True:
        result = (
            supabase.table('vendor_credits')
            .select('vendor_credit_id, quantity, rate, line_item_id')
            .eq('source', 'zoho')
            .range(start, start + 999)
            .execute()
        )
        
        if not result.data:
            break
        records.extend(result.data)
        start += 1000
    
    return records

def get_vendor_credit_details(vc_id, organization_id, access_token):
    url = f"https://www.zohoapis.com/inventory/v1/vendorcredits/{vc_id}"
    params = {'organization_id': organization_id}
    
    response = requests.get(url, headers=get_zoho_headers(access_token), params=params)
    data = response.json()
    
    if data['code'] != 0:
        return None
    return data.get('vendor_credit')

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
    result = supabase.table('locations').select('id, zoho_location_id').execute()
    return {str(loc['zoho_location_id']): loc for loc in result.data if loc.get('zoho_location_id')}

def vendor_credit_to_records(vc_details, zoho_item_map, zoho_location_map):
    records = []
    
    for line in vc_details.get('line_items', []):
        zoho_item_id = str(line.get('item_id', ''))
        local_item = zoho_item_map.get(zoho_item_id)
        
        if not local_item:
            continue
        
        local_location = zoho_location_map.get(str(line.get('location_id', '')))
        
        records.append({
            'vendor_credit_id': str(vc_details.get('vendor_credit_id')),
            'vendor_credit_number': vc_details.get('vendor_credit_number'),
            'item_id': local_item['zoho_item_id'],
            'location_id': local_location['id'] if local_location else None,
            'quantity': float(line.get('quantity', 0)),
            'rate': float(line.get('rate', 0)),
            'credit_date': vc_details.get('date'),
            'created_time': vc_details.get('created_time'),
            'source': 'zoho',
            'status': vc_details.get('status'),
            'line_item_id': str(line.get('line_item_id'))
        })
    
    return records

def main():
    print("🚀 Starting vendor credits reconciliation...")
    
    supabase = create_client(
        wmill.get_variable("f/SUPABASE/URL"),
        wmill.get_variable("f/SUPABASE/ANON_KEY")
    )
    
    ORGANIZATION_ID = "870657839"
    ACCESS_TOKEN = get_access_token()
    
    # Build mappings
    zoho_item_map = build_item_mapping(supabase)
    zoho_location_map = build_location_mapping(supabase)
    
    # Fetch all vendor credits from Zoho
    vendor_credits = fetch_all_vendor_credits(ORGANIZATION_ID, ACCESS_TOKEN)
    zoho_vc_ids = {str(vc['vendor_credit_id']): float(vc.get('total', 0)) for vc in vendor_credits}
    print(f"📊 Zoho vendor credits: {len(zoho_vc_ids)}")
    
    # Fetch all local vendor credits and group by vendor_credit_id
    local_records = fetch_all_local_vendor_credits(supabase)
    local_by_vc = {}
    for r in local_records:
        vc_id = str(r.get('vendor_credit_id', ''))
        if vc_id:
            if vc_id not in local_by_vc:
                local_by_vc[vc_id] = []
            local_by_vc[vc_id].append(r)
    print(f"📊 Local vendor credits: {len(local_by_vc)}")
    
    # Calculate local subtotals
    local_subtotals = {
        vc_id: sum(float(r['quantity'] or 0) * float(r['rate'] or 0) for r in items)
        for vc_id, items in local_by_vc.items()
    }
    
    # Find differences
    zoho_ids = set(zoho_vc_ids.keys())
    local_ids = set(local_by_vc.keys())
    
    missing_locally = zoho_ids - local_ids
    extra_locally = local_ids - zoho_ids
    common = zoho_ids & local_ids
    
    mismatched = [vc_id for vc_id in common if abs(zoho_vc_ids[vc_id] - local_subtotals[vc_id]) > 0.01]
    
    print(f"\n🆕 Missing locally: {len(missing_locally)}")
    print(f"🗑️  Extra locally (to delete): {len(extra_locally)}")
    print(f"⚠️  Mismatched subtotals: {len(mismatched)}")
    
    # Delete extra vendor credits
    deleted = 0
    for vc_id in extra_locally:
        supabase.table('vendor_credits').delete().eq('vendor_credit_id', vc_id).eq('source', 'zoho').execute()
        deleted += 1
    print(f"🗑️  Deleted {deleted} vendor credits")
    
    # Show top differences
    diffs = [(vc_id, abs(zoho_vc_ids[vc_id] - local_subtotals[vc_id])) for vc_id in common]
    diffs.sort(key=lambda x: x[1], reverse=True)
    
    print("\nTop 10 differences:")
    for vc_id, diff in diffs[:10]:
        print(f"  {vc_id}: ${diff:.2f} (Zoho: ${zoho_vc_ids[vc_id]:.2f}, Local: ${local_subtotals[vc_id]:.2f})")
    
    # Process missing and mismatched vendor credits
    to_process = list(missing_locally) + mismatched
    inserted = 0
    
    for i, vc_id in enumerate(to_process):
        print(f"⚙️  Processing {i+1}/{len(to_process)}: {vc_id}")
        
        # Delete existing if mismatched
        if vc_id in mismatched:
            supabase.table('vendor_credits').delete().eq('vendor_credit_id', vc_id).eq('source', 'zoho').execute()
        
        # Fetch and insert
        details = get_vendor_credit_details(vc_id, ORGANIZATION_ID, ACCESS_TOKEN)
        if details:
            records = vendor_credit_to_records(details, zoho_item_map, zoho_location_map)
            if records:
                supabase.table('vendor_credits').upsert(records, on_conflict='line_item_id').execute()
                inserted += len(records)
        
        time.sleep(0.6)
    
    print(f"\n🎉 Done! Inserted {inserted} records")
    
    return {
        "missing_added": len(missing_locally),
        "extra_deleted": len(extra_locally),
        "mismatched_fixed": len(mismatched),
        "records_inserted": inserted
    }