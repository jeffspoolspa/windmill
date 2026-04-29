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

def fetch_all_adjustments(organization_id, access_token):
    adjustments = []
    page = 1
    
    while True:
        url = "https://www.zohoapis.com/inventory/v1/inventoryadjustments"
        params = {'organization_id': organization_id, 'page': page, 'per_page': 200}
        
        response = requests.get(url, headers=get_zoho_headers(access_token), params=params)
        data = response.json()
        
        if data['code'] != 0:
            break
            
        batch = data.get('inventory_adjustments', [])
        if not batch:
            break
        
        published = [adj for adj in batch if adj.get('status') != 'draft']
        adjustments.extend(published)
        print(f"📦 Page {page}: {len(adjustments)} adjustments")
        time.sleep(0.6)
        page += 1
    
    return adjustments

def fetch_all_local_adjustments(supabase):
    adjustments = []
    start = 0
    
    while True:
        result = (
            supabase.table('adjustments')
            .select('adjustment_id, qty_adjusted, cost, line_item_id')
            .eq('source', 'zoho')
            .range(start, start + 999)
            .execute()
        )
        
        if not result.data:
            break
        adjustments.extend(result.data)
        start += 1000
    
    return adjustments

def get_adjustment_details(adjustment_id, organization_id, access_token):
    url = f"https://www.zohoapis.com/inventory/v1/inventoryadjustments/{adjustment_id}"
    params = {'organization_id': organization_id}
    
    response = requests.get(url, headers=get_zoho_headers(access_token), params=params)
    data = response.json()
    
    if data['code'] != 0:
        return None
    return data['inventory_adjustment']

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

def adjustment_to_records(adj_details, zoho_item_map, zoho_location_map):
    records = []
    adj_date = adj_details.get('date')
    
    for line in adj_details.get('line_items', []):
        zoho_item_id = str(line.get('item_id', ''))
        local_item = zoho_item_map.get(zoho_item_id)
        
        if not local_item:
            continue
        
        local_location = zoho_location_map.get(str(line.get('location_id', '')))
        
        quantity_adjusted = float(line.get('quantity_adjusted', 0))
        item_total = float(line.get('item_total', 0))
        unit_cost = item_total / quantity_adjusted if quantity_adjusted != 0 else 0
        
        records.append({
            'date': adj_date,
            'sku': local_item.get('sku') or local_item.get('lou_sku'),
            'item_id': local_item['zoho_item_id'],
            'qty_adjusted': quantity_adjusted,
            'unit_cost': unit_cost,
            'cost': item_total,
            'lou_stock_site': local_location.get('lou_stock_site') if local_location else None,
            'location_id': local_location['id'] if local_location else None,
            'adjustment_id': str(adj_details.get('inventory_adjustment_id')),
            'reason': adj_details.get('reason'),
            'reason_id': adj_details.get('reason_id'),
            'reference_number': adj_details.get('reference_number'),
            'comment': adj_details.get('description'),
            'line_item_id': line.get('line_item_id'),
            'source': 'zoho',
            'created_time': adj_details.get('created_time')
        })
    
    return records

def main():
    print("🚀 Starting adjustment reconciliation...")
    
    supabase = create_client(
        wmill.get_variable("f/SUPABASE/URL"),
        wmill.get_variable("f/SUPABASE/ANON_KEY")
    )
    
    ORGANIZATION_ID = "870657839"
    ACCESS_TOKEN = get_access_token()
    
    # Build mappings
    zoho_item_map = build_item_mapping(supabase)
    zoho_location_map = build_location_mapping(supabase)
    
    # Fetch all adjustments from Zoho
    adjustments = fetch_all_adjustments(ORGANIZATION_ID, ACCESS_TOKEN)
    
    # Check available fields
    print(adjustments[0].keys())
    
    # Use total or appropriate field
    zoho_adjustments = {str(adj['inventory_adjustment_id']): float(adj.get('total', 0)) for adj in adjustments}
    print(f"📊 Zoho adjustments: {len(zoho_adjustments)}")
    
    # Fetch all local adjustments and group by adjustment_id
    local_adj = fetch_all_local_adjustments(supabase)
    local_by_adj = {}
    for a in local_adj:
        aid = str(a.get('adjustment_id', ''))
        if aid:
            if aid not in local_by_adj:
                local_by_adj[aid] = []
            local_by_adj[aid].append(a)
    print(f"📊 Local adjustments: {len(local_by_adj)}")
    
    # Calculate local totals (qty_adjusted * unit_cost)
    local_totals = {
        aid: sum(float(a['cost'] or 0) for a in items)
        for aid, items in local_by_adj.items()
    }
    
    # Find differences
    zoho_ids = set(zoho_adjustments.keys())
    local_ids = set(local_by_adj.keys())
    
    missing_locally = zoho_ids - local_ids
    extra_locally = local_ids - zoho_ids
    common = zoho_ids & local_ids
    
    mismatched = [aid for aid in common if abs(zoho_adjustments[aid] - local_totals[aid]) > 0.01]
    
    print(f"\n🆕 Missing locally: {len(missing_locally)}")
    print(f"🗑️  Extra locally (to delete): {len(extra_locally)}")
    print(f"⚠️  Mismatched totals: {len(mismatched)}")
    
    # Show top differences
    diffs = [(aid, abs(zoho_adjustments[aid] - local_totals[aid])) for aid in common]
    diffs.sort(key=lambda x: x[1], reverse=True)
    
    print("Top 10 differences:")
    for aid, diff in diffs[:10]:
        print(f"  {aid}: ${diff:.2f} (Zoho: ${zoho_adjustments[aid]:.2f}, Local: ${local_totals[aid]:.2f})")
    
    print(f"\nDifferences > $1: {len([d for d in diffs if d[1] > 1])}")
    print(f"Differences > $0.10: {len([d for d in diffs if d[1] > 0.10])}")
    print(f"Differences > $0.01: {len([d for d in diffs if d[1] > 0.01])}")
    
    # Uncomment below to fix discrepancies:
    # Delete extra adjustments
    deleted = 0
    for aid in extra_locally:
        supabase.table('adjustments').delete().eq('adjustment_id', aid).eq('source', 'zoho').execute()
        deleted += 1
    print(f"🗑️  Deleted {deleted} adjustments")
    
    # Process missing and mismatched
    to_process = list(missing_locally) + mismatched
    inserted = 0
    
    for i, aid in enumerate(to_process):
        print(f"⚙️  Processing {i+1}/{len(to_process)}: {aid}")
        
        if aid in mismatched:
            supabase.table('adjustments').delete().eq('adjustment_id', aid).eq('source', 'zoho').execute()
        
        details = get_adjustment_details(aid, ORGANIZATION_ID, ACCESS_TOKEN)
        if details:
            records = adjustment_to_records(details, zoho_item_map, zoho_location_map)
            if records:
                supabase.table('adjustments').upsert(records, on_conflict='line_item_id').execute()
                inserted += len(records)
        
        time.sleep(0.6)
    
    print(f"\n🎉 Done! Inserted {inserted} records")
    
    return {
        "zoho_adjustments": len(zoho_adjustments),
        "local_adjustments": len(local_by_adj),
        "missing": len(missing_locally),
        "extra": len(extra_locally),
        "mismatched": len(mismatched)
    }