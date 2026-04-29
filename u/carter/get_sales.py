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

def fetch_all_invoices(organization_id, access_token):
    invoices = []
    page = 1
    
    while True:
        url = "https://www.zohoapis.com/inventory/v1/invoices"
        params = {'organization_id': organization_id, 'page': page, 'per_page': 1000}
        
        response = requests.get(url, headers=get_zoho_headers(access_token), params=params)
        data = response.json()
        
        if data['code'] != 0:
            break
            
        batch = data.get('invoices', [])
        if not batch:
            break
            
        invoices.extend(batch)
        print(f"📦 Page {page}: {len(invoices)} invoices")
        time.sleep(0.6)
        page += 1
    invoices = [inv for inv in invoices if inv.get('status') != 'draft']
    return invoices

def fetch_all_sales(supabase):
    sales = []
    start = 0
    
    while True:
        result = (
            supabase.table('sales')
            .select('invoice_id, quantity, unit_price, line_item_id')
            .eq('source', 'zoho')
            .range(start, start + 999)
            .execute()
        )
        
        if not result.data:
            break
        sales.extend(result.data)
        start += 1000
    
    return sales

def get_invoice_details(invoice_id, organization_id, access_token):
    url = f"https://www.zohoapis.com/inventory/v1/invoices/{invoice_id}"
    params = {'organization_id': organization_id}
    
    response = requests.get(url, headers=get_zoho_headers(access_token), params=params)
    data = response.json()
    
    if data['code'] != 0:
        return None
    return data['invoice']

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

def invoice_to_records(inv_details, zoho_item_map, zoho_location_map):
    records = []
    inv_date = inv_details.get('date')
    
    for line in inv_details.get('line_items', []):
        zoho_item_id = str(line.get('item_id', ''))
        local_item = zoho_item_map.get(zoho_item_id)
        
        if not local_item:
            continue
        
        local_location = zoho_location_map.get(str(line.get('location_id', '')))
        
        records.append({
            'item_id': local_item['zoho_item_id'],
            'sku': local_item.get('sku') or local_item.get('lou_sku'),
            'date': inv_date,
            'quantity': float(line.get('quantity', 0)),
            'unit_price': float(line.get('item_total', 0)) / float(line.get('quantity', 1)),
            'customer': inv_details.get('customer_name'),
            'invoice_number': inv_details.get('invoice_number'),
            'sales_order_number': inv_details.get('salesorder_number'),
            'sales_order_id': inv_details.get('salesorder_id'),
            'line_item_id': line.get('line_item_id'),
            'source': 'zoho',
            'status': inv_details.get('status'),
            'location_id': local_location['id'] if local_location else None,
            'invoice_id': str(inv_details.get('invoice_id')),
            'reference_number': inv_details.get('reference_number'),
            'created_time': inv_details.get('created_time')
        })
    
    return records

def main():
    print("🚀 Starting invoice reconciliation...")
    
    supabase = create_client(
        wmill.get_variable("f/SUPABASE/URL"),
        wmill.get_variable("f/SUPABASE/ANON_KEY")
    )
    
    ORGANIZATION_ID = "870657839"
    ACCESS_TOKEN = get_access_token()
    
    # Build mappings
    zoho_item_map = build_item_mapping(supabase)
    zoho_location_map = build_location_mapping(supabase)
    
    # Fetch all invoices from Zoho
    invoices = fetch_all_invoices(ORGANIZATION_ID, ACCESS_TOKEN)
    
    # Print keys to confirm field name
    print(invoices[0].keys())
    
    zoho_invoices = {str(inv['invoice_id']): float(inv.get('total', 0)) for inv in invoices}
    print(f"📊 Zoho invoices: {len(zoho_invoices)}")
    
    # Fetch all sales and group by invoice_id
    sales = fetch_all_sales(supabase)
    local_by_invoice = {}
    for s in sales:
        iid = str(s.get('invoice_id', ''))
        if iid:
            if iid not in local_by_invoice:
                local_by_invoice[iid] = []
            local_by_invoice[iid].append(s)
    print(f"📊 Local invoices: {len(local_by_invoice)}")
    
    # Calculate local subtotals
    local_subtotals = {
        iid: sum(float(s['quantity'] or 0) * float(s['unit_price'] or 0) for s in items)
        for iid, items in local_by_invoice.items()
    }
    
    # Find differences
    zoho_ids = set(zoho_invoices.keys())
    local_ids = set(local_by_invoice.keys())
    
    missing_locally = zoho_ids - local_ids
    extra_locally = local_ids - zoho_ids
    common = zoho_ids & local_ids
    
    mismatched = [iid for iid in common if abs(zoho_invoices[iid] - local_subtotals[iid]) > 0.01]
    
    print(f"\n🆕 Missing locally: {len(missing_locally)}")
    print(f"🗑️  Extra locally (to delete): {len(extra_locally)}")
    print(f"⚠️  Mismatched totals: {len(mismatched)}")
    
    # Show top differences
    diffs = [(iid, abs(zoho_invoices[iid] - local_subtotals[iid])) for iid in common]
    diffs.sort(key=lambda x: x[1], reverse=True)
    
    print("Top 10 differences:")
    for iid, diff in diffs[:10]:
        print(f"  {iid}: ${diff:.2f} (Zoho: ${zoho_invoices[iid]:.2f}, Local: ${local_subtotals[iid]:.2f})")
    
    print(f"\nDifferences > $1: {len([d for d in diffs if d[1] > 1])}")
    print(f"Differences > $0.10: {len([d for d in diffs if d[1] > 0.10])}")
    print(f"Differences > $0.01: {len([d for d in diffs if d[1] > 0.01])}")
    
    # Delete extra invoices
    deleted = 0
    for iid in extra_locally:
        supabase.table('sales').delete().eq('invoice_id', iid).eq('source', 'zoho').execute()
        deleted += 1
    print(f"🗑️  Deleted {deleted} invoices")
    
    # Process missing and mismatched
    to_process = list(missing_locally) + mismatched
    inserted = 0
    
    for i, iid in enumerate(to_process):
        if i < 500:
            print(f"⚙️  Processing {i+1}/{len(to_process)}: {iid}")
            
            if iid in mismatched:
                supabase.table('sales').delete().eq('invoice_id', iid).eq('source', 'zoho').execute()
            
            details = get_invoice_details(iid, ORGANIZATION_ID, ACCESS_TOKEN)
            if details:
                records = invoice_to_records(details, zoho_item_map, zoho_location_map)
                if records:
                    supabase.table('sales').upsert(records, on_conflict='line_item_id').execute()
                    inserted += len(records)
            
            time.sleep(0.6)
    
    print(f"\n🎉 Done! Inserted {inserted} records")
    
    return {
        "zoho_invoices": len(zoho_invoices),
        "local_invoices": len(local_by_invoice),
        "missing": len(missing_locally),
        "extra": len(extra_locally),
        "mismatched": len(mismatched)
    }
