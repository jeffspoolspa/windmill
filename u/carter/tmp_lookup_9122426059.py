
import wmill
import time
import subprocess
import json

def main():
    """
    One-off wrapper to run rc_deep_lookup with specific args.
    Delete after use.
    """
    # Import and call the deep lookup directly
    import importlib.util
    import sys
    
    # Just inline the call since we can't easily import
    from datetime import datetime, timedelta
    import re, requests, io
    from ringcentral import SDK
    
    phone_number = "912-242-6059"
    days_back = 90
    transcribe = False
    
    digits = re.sub(r'\D', '', phone_number)
    if len(digits) == 11 and digits.startswith('1'): digits = digits[1:]
    target = digits
    formatted = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    print(f"Deep searching for: {formatted} over {days_back} days")
    
    rc_resource = wmill.get_resource("u/carter/ring_central")
    rcsdk = SDK(rc_resource.get('RC_APP_CLIENT_ID'), rc_resource.get('RC_APP_CLIENT_SECRET'), "https://platform.ringcentral.com")
    platform = rcsdk.platform()
    platform.login(jwt=rc_resource.get('RC_USER_JWT'))
    print("✓ RC authenticated")
    
    matches = []
    start = datetime.utcnow() - timedelta(days=days_back)
    end = datetime.utcnow()
    day = start
    
    while day <= end:
        df = day.strftime('%Y-%m-%dT00:00:00.000Z')
        dt = day.strftime('%Y-%m-%dT23:59:59.000Z')
        
        for attempt in range(5):
            try:
                resp = platform.get('/restapi/v1.0/account/~/call-log', {
                    'type': 'Voice', 'dateFrom': df, 'dateTo': dt, 'perPage': 250, 'view': 'Detailed'
                })
                break
            except Exception as e:
                if '429' in str(e):
                    wait = 30 * (attempt + 1)
                    print(f"  Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    raise
        
        records = resp.json().records if hasattr(resp.json(), 'records') else []
        
        for r in records:
            fn = tn = nm = ''
            try:
                fo = r.__dict__.get('from', None) or (r.from_ if hasattr(r, 'from_') else None)
                if fo:
                    fn = fo.phoneNumber if hasattr(fo, 'phoneNumber') and fo.phoneNumber else ''
                    nm = fo.name if hasattr(fo, 'name') and fo.name else ''
            except: pass
            try:
                if hasattr(r, 'to') and r.to and hasattr(r.to, 'phoneNumber'):
                    tn = r.to.phoneNumber or ''
            except: pass
            
            if target in fn or target in tn:
                has_rec = hasattr(r, 'recording') and r.recording is not None
                rec_id = r.recording.id if has_rec else None
                m = {'date': r.startTime if hasattr(r, 'startTime') else '?', 'duration_seconds': r.duration if hasattr(r, 'duration') else 0,
                     'direction': r.direction if hasattr(r, 'direction') else '?', 'result': r.result if hasattr(r, 'result') else '?',
                     'from': f"{fn} ({nm})" if nm else fn, 'to': tn, 'has_recording': has_rec, 'recording_id': rec_id}
                if hasattr(r, 'legs') and r.legs:
                    extras = []
                    for leg in r.legs:
                        lr = leg.recording.id if hasattr(leg, 'recording') and leg.recording else None
                        if lr and lr != rec_id:
                            extras.append({'recording_id': lr, 'action': leg.action if hasattr(leg, 'action') else '?', 'duration_seconds': leg.duration if hasattr(leg, 'duration') else 0})
                    if extras: m['additional_recordings'] = extras
                matches.append(m)
                print(f"  ✓ MATCH: {day.strftime('%m/%d')} {m['direction']} {m['duration_seconds']}s {'REC' if has_rec else ''}")
        
        day += timedelta(days=1)
        time.sleep(0.5)
    
    matches.sort(key=lambda x: x['date'])
    unique_recs = set()
    for m in matches:
        if m.get('recording_id'): unique_recs.add(m['recording_id'])
        for e in m.get('additional_recordings', []):
            if e.get('recording_id'): unique_recs.add(e['recording_id'])
    
    print(f"\n✓ Done: {len(matches)} calls, {len(unique_recs)} recordings")
    return {'phone_number': formatted, 'search_days': days_back, 'total_calls': len(matches), 'calls_with_recordings': len(unique_recs), 'calls': matches}
