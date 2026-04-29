"""
QBO Unapplied Credits Analysis
==============================
Path: u/carter/qbo_unapplied_credits_analysis

Fetches all payments with unapplied balances and classifies them
for automated cleanup.
"""

import requests
from datetime import datetime
import wmill


def refresh_tokens(resource_path: str) -> tuple[str, dict]:
    """
    Refresh QBO tokens and save new refresh_token back to resource.
    Returns (access_token, full_resource)
    """
    resource = wmill.get_resource(resource_path)
    response = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": resource["refresh_token"]
        },
        auth=(resource["client_id"], resource["client_secret"])
    )
    if not response.ok:
        raise Exception(f"Token refresh failed: {response.status_code} - {response.text}")
    
    tokens = response.json()
    
    # Update resource with new refresh token
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(resource_path, resource)
    print(f"✅ Tokens refreshed at {datetime.now().isoformat()}")
    
    return tokens["access_token"], resource


def qbo_query(access_token: str, realm_id: str, query: str) -> dict:
    """Execute a QBO query"""
    response = requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/query",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json"
        },
        params={"query": query}
    )
    if not response.ok:
        raise Exception(f"QBO query failed: {response.status_code} - {response.text}")
    return response.json()


def fetch_all(access_token: str, realm_id: str, entity: str) -> list:
    """Fetch all records with pagination"""
    all_records = []
    start = 1
    
    while True:
        query = f"SELECT * FROM {entity} STARTPOSITION {start} MAXRESULTS 1000"
        result = qbo_query(access_token, realm_id, query)
        records = result.get('QueryResponse', {}).get(entity, [])
        
        if not records:
            break
        
        all_records.extend(records)
        print(f"  {entity}: {len(all_records)}")
        
        if len(records) < 1000:
            break
        start += 1000
    
    return all_records


def main():
    resource_path = "u/carter/quickbooks_api"
    
    # Get fresh access token
    access_token, resource = refresh_tokens(resource_path)
    realm_id = resource["realm_id"]
    
    print("=" * 50)
    print("QBO UNAPPLIED CREDITS ANALYSIS")
    print("=" * 50)
    
    # Fetch payments
    print("\n📥 Fetching data...")
    payments = fetch_all(access_token, realm_id, 'Payment')
    invoices = fetch_all(access_token, realm_id, 'Invoice')
    
    # Filter unapplied
    unapplied = [p for p in payments if float(p.get('UnappliedAmt', 0)) > 0]
    total_unapplied = sum(float(p.get('UnappliedAmt', 0)) for p in unapplied)
    
    print(f"\n📊 Results:")
    print(f"  Total payments: {len(payments)}")
    print(f"  Unapplied: {len(unapplied)}")
    print(f"  Total unapplied: ${total_unapplied:,.2f}")
    
    # Get customer IDs with unapplied
    cust_ids = {p.get('CustomerRef', {}).get('value') for p in unapplied if p.get('CustomerRef')}
    print(f"  Customers affected: {len(cust_ids)}")
    
    # Build analysis
    analysis = {}
    
    for p in unapplied:
        cid = p.get('CustomerRef', {}).get('value')
        cname = p.get('CustomerRef', {}).get('name', 'Unknown')
        
        if cid not in analysis:
            analysis[cid] = {
                'name': cname,
                'payments': [],
                'open_invoices': [],
                'total_unapplied': 0,
                'total_open': 0,
                'exact_matches': []
            }
        
        pmt = {
            'id': p.get('Id'),
            'date': p.get('TxnDate'),
            'total': float(p.get('TotalAmt', 0)),
            'unapplied': float(p.get('UnappliedAmt', 0)),
            'method': p.get('PaymentMethodRef', {}).get('name') if p.get('PaymentMethodRef') else None,
            'ref_num': p.get('PaymentRefNum'),
            'memo': p.get('PrivateNote')
        }
        analysis[cid]['payments'].append(pmt)
        analysis[cid]['total_unapplied'] += pmt['unapplied']
    
    # Add open invoices
    for inv in invoices:
        cid = inv.get('CustomerRef', {}).get('value')
        if cid not in analysis:
            continue
        
        bal = float(inv.get('Balance', 0))
        if bal > 0:
            analysis[cid]['open_invoices'].append({
                'id': inv.get('Id'),
                'number': inv.get('DocNumber'),
                'date': inv.get('TxnDate'),
                'total': float(inv.get('TotalAmt', 0)),
                'balance': bal
            })
            analysis[cid]['total_open'] += bal
    
    # Find exact matches
    for cid, data in analysis.items():
        for pmt in data['payments']:
            for inv in data['open_invoices']:
                if abs(inv['balance'] - pmt['unapplied']) < 0.01:
                    data['exact_matches'].append({
                        'payment_id': pmt['id'],
                        'payment_amt': pmt['unapplied'],
                        'invoice_id': inv['id'],
                        'invoice_num': inv['number']
                    })
    
    # Classify
    sorted_custs = sorted(analysis.items(), key=lambda x: x[1]['total_unapplied'], reverse=True)
    
    exact = [(c, d) for c, d in sorted_custs if d['exact_matches']]
    has_open = [(c, d) for c, d in sorted_custs if d['open_invoices'] and not d['exact_matches']]
    no_inv = [(c, d) for c, d in sorted_custs if not d['open_invoices']]
    
    exact_amt = sum(d['total_unapplied'] for _, d in exact)
    open_amt = sum(d['total_unapplied'] for _, d in has_open)
    no_inv_amt = sum(d['total_unapplied'] for _, d in no_inv)
    
    # Print report
    print("\n" + "=" * 50)
    print("CLASSIFICATION")
    print("=" * 50)
    
    print(f"\n🎯 EXACT MATCHES: {len(exact)} | ${exact_amt:,.2f}")
    for cid, d in exact[:10]:
        print(f"   {d['name'][:30]:<30} ${d['total_unapplied']:>10,.2f}")
        for m in d['exact_matches']:
            print(f"      Pmt {m['payment_id']} → Inv #{m['invoice_num']}")
    
    print(f"\n📋 HAS OPEN INVOICES: {len(has_open)} | ${open_amt:,.2f}")
    for cid, d in has_open[:10]:
        print(f"   {d['name'][:30]:<30} ${d['total_unapplied']:>10,.2f} (open: ${d['total_open']:,.2f})")
    
    print(f"\n⚠️  NO INVOICES: {len(no_inv)} | ${no_inv_amt:,.2f}")
    for cid, d in no_inv[:10]:
        print(f"   {d['name'][:30]:<30} ${d['total_unapplied']:>10,.2f}")
    
    print(f"\n{'=' * 50}")
    print(f"💰 TOTAL: ${total_unapplied:,.2f}")
    
    return {
        'summary': {
            'total_unapplied': total_unapplied,
            'count': len(unapplied),
            'customers': len(cust_ids),
            'exact_match_count': len(exact),
            'exact_match_amt': exact_amt,
            'has_open_count': len(has_open),
            'has_open_amt': open_amt,
            'no_inv_count': len(no_inv),
            'no_inv_amt': no_inv_amt
        },
        'analysis': analysis,
        'raw_payments': unapplied
    }