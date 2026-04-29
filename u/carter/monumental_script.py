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

def fetch_all_bills(organization_id, access_token):
    bills = []
    page = 1
    
    while True:
        url = "https://www.zohoapis.com/inventory/v1/bills"
        params = {'organization_id': organization_id, 'page': page, 'per_page': 200}
        
        response = requests.get(url, headers=get_zoho_headers(access_token), params=params)
        data = response.json()
        
        if data['code'] != 0:
            break
            
        batch = data.get('bills', [])
        if not batch:
            break
            
        bills.extend(batch)
        print(f"📦 Page {page}: {len(bills)} bills")
        time.sleep(0.6)
        page += 1

    print(bills[0].keys())
    return bills

def fetch_all_purchases(supabase):
    purchases = []
    start = 0
    
    while True:
        result = (
            supabase.table('purchases')
            .select('bill_id, quantity, unit_cost, line_item_id')
            .eq('source', 'zoho')
            .range(start, start + 999)
            .execute()
        )
        
        if not result.data:
            break
        purchases.extend(result.data)
        start += 1000
    
    return purchases

def get_bill_details(bill_id, organization_id, access_token):
    url = f"https://www.zohoapis.com/inventory/v1/bills/{bill_id}"
    params = {'organization_id': organization_id}
    
    response = requests.get(url, headers=get_zoho_headers(access_token), params=params)
    data = response.json()
    
    if data['code'] != 0:
        return None
    return data['bill']

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

def bill_to_records(bill_details, zoho_item_map, zoho_location_map):
    records = []
    bill_date = bill_details.get('date')
    
    for line in bill_details.get('line_items', []):
        zoho_item_id = str(line.get('item_id', ''))
        local_item = zoho_item_map.get(zoho_item_id)
        
        if not local_item:
            continue
        
        local_location = zoho_location_map.get(str(line.get('location_id', '')))
        
        records.append({
            'item_id': local_item['zoho_item_id'],
            'sku': local_item.get('sku') or local_item.get('lou_sku'),
            'sku_type': 'Inventory',
            'date': bill_date,
            'quantity': float(line.get('quantity', 0)),
            'unit_cost': float(line.get('rate', 0)),
            'vendor': bill_details.get('vendor_name'),
            'receipt_number': bill_details.get('bill_number'),
            'purchase_order_number': bill_details.get('reference_number'),
            'notes': bill_details.get('notes'),
            'source': 'zoho',
            'status': bill_details.get('status'),
            'location_id': local_location['id'] if local_location else None,
            'bill_id': str(bill_details.get('bill_id')),
            'line_item_id': line.get('line_item_id'),
            'created_time': bill_details.get('created_time')
        })
    
    return records

def main():
    print("🚀 Starting bill reconciliation...")
    
    supabase = create_client(
        wmill.get_variable("f/SUPABASE/URL"),
        wmill.get_variable("f/SUPABASE/ANON_KEY")
    )
    
    ORGANIZATION_ID = "870657839"
    ACCESS_TOKEN = get_access_token()
    
    # Build mappings
    zoho_item_map = build_item_mapping(supabase)
    zoho_location_map = build_location_mapping(supabase)
    
    # Fetch all bills from Zoho
    bills = fetch_all_bills(ORGANIZATION_ID, ACCESS_TOKEN)
    zoho_bills = {str(b['bill_id']): float(b.get('total', 0)) for b in bills}
    print(f"📊 Zoho bills: {len(zoho_bills)}")
    
    # Fetch all purchases and group by bill_id
    purchases = fetch_all_purchases(supabase)
    local_by_bill = {}
    for p in purchases:
        bid = str(p.get('bill_id', ''))
        if bid:
            if bid not in local_by_bill:
                local_by_bill[bid] = []
            local_by_bill[bid].append(p)
    print(f"📊 Local bills: {len(local_by_bill)}")
    
    # Calculate local subtotals
    local_subtotals = {
        bid: sum(float(p['quantity'] or 0) * float(p['unit_cost'] or 0) for p in items)
        for bid, items in local_by_bill.items()
    }
    
    # Find differences
    zoho_ids = set(zoho_bills.keys())
    local_ids = set(local_by_bill.keys())
    
    missing_locally = zoho_ids - local_ids
    extra_locally = local_ids - zoho_ids
    common = zoho_ids & local_ids
    
    mismatched = [bid for bid in common if abs(zoho_bills[bid] - local_subtotals[bid]) > 0.01]
    
    print(f"\n🆕 Missing locally: {len(missing_locally)}")
    print(f"🗑️  Extra locally (to delete): {len(extra_locally)}")
    print(f"⚠️  Mismatched subtotals: {len(mismatched)}")
    
    # Delete extra bills
    deleted = 0
    for bid in extra_locally:
        supabase.table('purchases').delete().eq('bill_id', bid).eq('source', 'zoho').execute()
        deleted += 1
    print(f"🗑️  Deleted {deleted} bills")

        # Add this before processing to see the distribution
    diffs = [(bid, abs(zoho_bills[bid] - local_subtotals[bid])) for bid in common]
    diffs.sort(key=lambda x: x[1], reverse=True)

    print("Top 10 differences:")
    for bid, diff in diffs[:10]:
        print(f"  {bid}: ${diff:.2f} (Zoho: ${zoho_bills[bid]:.2f}, Local: ${local_subtotals[bid]:.2f})")

    print(f"\nDifferences > $1: {len([d for d in diffs if d[1] > 1])}")
    print(f"Differences > $0.10: {len([d for d in diffs if d[1] > 0.10])}")
    print(f"Differences > $0.01: {len([d for d in diffs if d[1] > 0.01])}")
    
    # Process missing and mismatched bills
    to_process = list(missing_locally) + mismatched
    inserted = 0
    
    for i, bid in enumerate(to_process):
        print(f"⚙️  Processing {i+1}/{len(to_process)}: {bid}")
        
        # Delete existing if mismatched
        if bid in mismatched:
            supabase.table('purchases').delete().eq('bill_id', bid).eq('source', 'zoho').execute()
        
        # Fetch and insert
        details = get_bill_details(bid, ORGANIZATION_ID, ACCESS_TOKEN)
        if details:
            records = bill_to_records(details, zoho_item_map, zoho_location_map)
            if records:
                supabase.table('purchases').upsert(records, on_conflict='line_item_id').execute()
                inserted += len(records)
        
        time.sleep(0.6)
    
    print(f"\n🎉 Done! Inserted {inserted} records")
    
    return {
        "missing_added": len(missing_locally),
        "extra_deleted": len(extra_locally),
        "mismatched_fixed": len(mismatched),
        "records_inserted": inserted
    }