
import wmill
from ringcentral import SDK
import requests
import io
import re
import time
from datetime import datetime, timedelta

PHONE_NUMBER = "443-686-0297"
DAYS_BACK = 31
TRANSCRIBE = True
INCLUDE_LEGS = True

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

def main():
    digits = re.sub(r'\D', '', PHONE_NUMBER)
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    if len(digits) != 10:
        return {"error": f"Could not parse phone number: {PHONE_NUMBER}"}

    target = digits
    formatted = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    print(f"Searching for: {formatted} ({target}), {DAYS_BACK} days")

    rc_resource = wmill.get_resource("u/carter/ring_central")
    rcsdk = SDK(
        rc_resource.get('RC_APP_CLIENT_ID'),
        rc_resource.get('RC_APP_CLIENT_SECRET'),
        "https://platform.ringcentral.com"
    )
    platform = rcsdk.platform()
    platform.login(jwt=rc_resource.get('RC_USER_JWT'))
    print("RC authenticated")

    date_from = (datetime.utcnow() - timedelta(days=DAYS_BACK)).strftime('%Y-%m-%dT00:00:00.000Z')

    all_matches = []
    view = 'Detailed' if INCLUDE_LEGS else 'Simple'

    for page in range(1, 20):
        resp = safe_get(platform, '/restapi/v1.0/account/~/call-log', {
            'type': 'Voice', 'dateFrom': date_from, 'perPage': 250, 'view': view, 'page': page
        })
        data = resp.json()
        records = data.records if hasattr(data, 'records') else []
        if not records:
            break
        for r in records:
            from_num = ''; from_name = ''; to_num = ''; to_name = ''
            try:
                from_obj = r.__dict__.get('from', None) or (r.from_ if hasattr(r, 'from_') else None)
                if from_obj:
                    from_num = from_obj.phoneNumber if hasattr(from_obj, 'phoneNumber') else ''
                    from_name = from_obj.name if hasattr(from_obj, 'name') else ''
            except: pass
            try:
                if hasattr(r, 'to') and r.to:
                    to_num = r.to.phoneNumber if hasattr(r.to, 'phoneNumber') else ''
                    to_name = r.to.name if hasattr(r.to, 'name') else ''
            except: pass
            if target in (from_num or '') or target in (to_num or ''):
                has_rec = hasattr(r, 'recording') and r.recording is not None
                rec_id = r.recording.id if has_rec else None
                match = {
                    'date': r.startTime if hasattr(r, 'startTime') else '?',
                    'duration_seconds': r.duration if hasattr(r, 'duration') else 0,
                    'direction': r.direction if hasattr(r, 'direction') else '?',
                    'result': r.result if hasattr(r, 'result') else '?',
                    'from': f"{from_num} ({from_name})" if from_name else from_num,
                    'to': f"{to_num} ({to_name})" if to_name else to_num,
                    'has_recording': has_rec,
                    'recording_id': rec_id,
                }
                extra_recordings = []
                if INCLUDE_LEGS and hasattr(r, 'legs') and r.legs:
                    for leg in r.legs:
                        leg_rec = leg.recording.id if hasattr(leg, 'recording') and leg.recording else None
                        if leg_rec and leg_rec != rec_id:
                            leg_action = leg.action if hasattr(leg, 'action') else '?'
                            leg_dur = leg.duration if hasattr(leg, 'duration') else 0
                            extra_recordings.append({'recording_id': leg_rec, 'action': leg_action, 'duration_seconds': leg_dur})
                if extra_recordings:
                    match['additional_recordings'] = extra_recordings
                all_matches.append(match)
        nav = data.navigation if hasattr(data, 'navigation') else None
        if not (nav and hasattr(nav, 'nextPage') and nav.nextPage):
            break
        time.sleep(1)

    all_matches.sort(key=lambda x: x['date'])
    print(f"Found {len(all_matches)} calls for {formatted}")

    all_recording_ids = []
    for m in all_matches:
        if m.get('recording_id'):
            all_recording_ids.append((m['recording_id'], m['date'], m['direction'], m['duration_seconds']))
        for extra in m.get('additional_recordings', []):
            if extra.get('recording_id'):
                all_recording_ids.append((extra['recording_id'], m['date'], extra['action'], extra['duration_seconds']))
    seen = set(); unique_recordings = []
    for rec in all_recording_ids:
        if rec[0] not in seen:
            seen.add(rec[0]); unique_recordings.append(rec)
    print(f"{len(unique_recordings)} unique recordings")

    transcripts = []
    if TRANSCRIBE and unique_recordings:
        openai_key = wmill.get_variable("u/carter/openai_api_key")
        for rec_id, date, direction, duration in unique_recordings:
            try:
                rec_resp = platform.get(f"/restapi/v1.0/account/~/recording/{rec_id}/content")
                audio_bytes = rec_resp.response().content
                openai_resp = requests.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {openai_key}"},
                    files={"file": ("rec.mp3", io.BytesIO(audio_bytes), "audio/mpeg")},
                    data={
                        "model": "gpt-4o-transcribe",
                        "response_format": "json",
                        "language": "en",
                        "prompt": "Phone call with Jeff's Pool and Spa Service / Perfect Pools in coastal Georgia."
                    },
                    timeout=300
                )
                if openai_resp.status_code == 200:
                    text = openai_resp.json().get('text', '')
                    transcripts.append({
                        'recording_id': rec_id, 'date': date, 'direction': direction,
                        'duration_seconds': duration, 'transcript': text
                    })
                    print(f"  OK {date}: {text[:60]}...")
                else:
                    transcripts.append({'recording_id': rec_id, 'date': date,
                                        'error': f"OpenAI {openai_resp.status_code}: {openai_resp.text[:200]}"})
                    print(f"  FAIL {openai_resp.status_code}")
                time.sleep(1)
            except Exception as e:
                transcripts.append({'recording_id': rec_id, 'date': date, 'error': str(e)[:200]})
                print(f"  ERR {str(e)[:100]}")

    result = {
        'phone_number': formatted, 'search_days': DAYS_BACK,
        'total_calls': len(all_matches), 'calls_with_recordings': len(unique_recordings),
        'calls': all_matches,
    }
    if transcripts:
        result['transcripts'] = transcripts
    return result
