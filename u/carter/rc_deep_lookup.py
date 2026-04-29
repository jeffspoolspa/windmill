
import wmill, time, re, requests, io
from ringcentral import SDK
from datetime import datetime, timedelta

def safe_get(platform, url, params, max_retries=5):
    for attempt in range(max_retries):
        try:
            return platform.get(url, params)
        except Exception as e:
            if '429' in str(e):
                wait = 30 * (attempt + 1)
                print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
            else:
                raise
    raise Exception("Max retries exceeded")

def main(phone_number: str, days_back: int = 90, transcribe: bool = False):
    """
    Deep search: look up all calls to/from a phone number by scanning every day individually.
    This avoids the 250-per-page pagination issue with broader date ranges.
    Slower but guaranteed to find every call.
    
    Args:
        phone_number: Any format
        days_back: How far back to search (default 90)
        transcribe: Transcribe recordings via OpenAI
    """
    digits = re.sub(r'\D', '', phone_number)
    if len(digits) == 11 and digits.startswith('1'): digits = digits[1:]
    if len(digits) != 10:
        return {"error": f"Bad number: {phone_number} -> {digits}"}
    
    target = digits
    formatted = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    print(f"Deep searching for: {formatted} over {days_back} days (day-by-day)")
    
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
        
        resp = safe_get(platform, '/restapi/v1.0/account/~/call-log', {
            'type': 'Voice', 'dateFrom': df, 'dateTo': dt, 'perPage': 250, 'view': 'Detailed'
        })
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
                m = {
                    'date': r.startTime if hasattr(r, 'startTime') else '?',
                    'duration_seconds': r.duration if hasattr(r, 'duration') else 0,
                    'direction': r.direction if hasattr(r, 'direction') else '?',
                    'result': r.result if hasattr(r, 'result') else '?',
                    'from': f"{fn} ({nm})" if nm else fn,
                    'to': tn, 'has_recording': has_rec, 'recording_id': rec_id
                }
                # Check legs
                if hasattr(r, 'legs') and r.legs:
                    extras = []
                    for leg in r.legs:
                        lr = leg.recording.id if hasattr(leg, 'recording') and leg.recording else None
                        if lr and lr != rec_id:
                            extras.append({'recording_id': lr, 'action': leg.action if hasattr(leg, 'action') else '?', 'duration_seconds': leg.duration if hasattr(leg, 'duration') else 0})
                    if extras: m['additional_recordings'] = extras
                matches.append(m)
                print(f"  ✓ MATCH: {day.strftime('%m/%d')} {m['direction']} {m['duration_seconds']}s {'📼' if has_rec else ''}")
        
        day += timedelta(days=1)
        time.sleep(0.5)
    
    matches.sort(key=lambda x: x['date'])
    
    unique_recs = set()
    for m in matches:
        if m.get('recording_id'): unique_recs.add(m['recording_id'])
        for e in m.get('additional_recordings', []):
            if e.get('recording_id'): unique_recs.add(e['recording_id'])
    
    print(f"\n✓ Done: {len(matches)} calls, {len(unique_recs)} recordings")
    
    # Transcribe if requested
    transcripts = []
    if transcribe and unique_recs:
        openai_key = wmill.get_variable("u/carter/openai_api_key")
        done = set()
        for m in matches:
            for rec_id in [m.get('recording_id')] + [e.get('recording_id') for e in m.get('additional_recordings', [])]:
                if not rec_id or rec_id in done: continue
                done.add(rec_id)
                try:
                    audio = platform.get(f"/restapi/v1.0/account/~/recording/{rec_id}/content").response().content
                    r = requests.post("https://api.openai.com/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {openai_key}"},
                        files={"file": ("rec.mp3", io.BytesIO(audio), "audio/mpeg")},
                        data={"model": "gpt-4o-transcribe", "response_format": "json", "language": "en",
                              "prompt": "Phone call with Jeff's Pool and Spa Service / Perfect Pools in coastal Georgia."},
                        timeout=300)
                    transcripts.append({'recording_id': rec_id, 'date': m['date'],
                        'transcript': r.json().get('text', '') if r.status_code == 200 else f"ERROR {r.status_code}"})
                except Exception as e:
                    transcripts.append({'recording_id': rec_id, 'date': m['date'], 'error': str(e)[:200]})
                time.sleep(1)
    
    result = {'phone_number': formatted, 'search_days': days_back, 'total_calls': len(matches), 'calls_with_recordings': len(unique_recs), 'calls': matches}
    if transcripts: result['transcripts'] = transcripts
    return result
