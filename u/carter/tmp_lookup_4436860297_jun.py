
import wmill
from ringcentral import SDK
import requests
import io
import re
import time

PHONE_NUMBER = "443-686-0297"
DATE_FROM = "2026-06-01T00:00:00.000Z"
DATE_TO   = "2026-06-08T00:00:00.000Z"   # exclusive-ish upper bound; covers Jun 1-7

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

def transcribe_bytes(openai_key, audio_bytes):
    resp = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {openai_key}"},
        files={"file": ("rec.mp3", io.BytesIO(audio_bytes), "audio/mpeg")},
        data={
            "model": "whisper-1",
            "response_format": "verbose_json",
            "language": "en",
            "prompt": "Phone call with Jeff's Pool and Spa Service / Perfect Pools in coastal Georgia. Office staff include Mary and Anna. Topics may include billing, credits, refunds, invoices, and pool service."
        },
        timeout=600
    )
    if resp.status_code != 200:
        return None, f"OpenAI {resp.status_code}: {resp.text[:300]}"
    j = resp.json()
    segs = [{"start": round(s.get("start",0),1), "end": round(s.get("end",0),1),
             "text": s.get("text","").strip()} for s in j.get("segments", [])]
    return {"duration_sec": round(j.get("duration",0),1),
            "full_text": j.get("text",""), "segments": segs}, None

def main():
    digits = re.sub(r'\D', '', PHONE_NUMBER)
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    target = digits
    formatted = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    print(f"Searching {formatted} from {DATE_FROM} to {DATE_TO}")

    rc_resource = wmill.get_resource("u/carter/ring_central")
    rcsdk = SDK(rc_resource.get('RC_APP_CLIENT_ID'), rc_resource.get('RC_APP_CLIENT_SECRET'),
                "https://platform.ringcentral.com")
    platform = rcsdk.platform()
    platform.login(jwt=rc_resource.get('RC_USER_JWT'))
    print("RC authenticated")

    all_matches = []
    for page in range(1, 20):
        resp = safe_get(platform, '/restapi/v1.0/account/~/call-log', {
            'type': 'Voice', 'dateFrom': DATE_FROM, 'dateTo': DATE_TO,
            'perPage': 250, 'view': 'Detailed', 'page': page
        })
        data = resp.json()
        records = data.records if hasattr(data, 'records') else []
        if not records:
            break
        for r in records:
            from_num=''; from_name=''; to_num=''; to_name=''
            try:
                fo = r.__dict__.get('from', None) or (r.from_ if hasattr(r,'from_') else None)
                if fo:
                    from_num = fo.phoneNumber if hasattr(fo,'phoneNumber') else ''
                    from_name = fo.name if hasattr(fo,'name') else ''
            except: pass
            try:
                if hasattr(r,'to') and r.to:
                    to_num = r.to.phoneNumber if hasattr(r.to,'phoneNumber') else ''
                    to_name = r.to.name if hasattr(r.to,'name') else ''
            except: pass
            if target in (from_num or '') or target in (to_num or ''):
                has_rec = hasattr(r,'recording') and r.recording is not None
                rec_id = r.recording.id if has_rec else None
                m = {'date': r.startTime if hasattr(r,'startTime') else '?',
                     'duration_seconds': r.duration if hasattr(r,'duration') else 0,
                     'direction': r.direction if hasattr(r,'direction') else '?',
                     'result': r.result if hasattr(r,'result') else '?',
                     'from': f"{from_num} ({from_name})" if from_name else from_num,
                     'to': f"{to_num} ({to_name})" if to_name else to_num,
                     'has_recording': has_rec, 'recording_id': rec_id}
                extra=[]
                if hasattr(r,'legs') and r.legs:
                    for leg in r.legs:
                        lr = leg.recording.id if hasattr(leg,'recording') and leg.recording else None
                        if lr and lr != rec_id:
                            extra.append({'recording_id': lr,
                                          'action': leg.action if hasattr(leg,'action') else '?',
                                          'duration_seconds': leg.duration if hasattr(leg,'duration') else 0})
                if extra: m['additional_recordings']=extra
                all_matches.append(m)
        nav = data.navigation if hasattr(data,'navigation') else None
        if not (nav and hasattr(nav,'nextPage') and nav.nextPage):
            break
        time.sleep(1)

    all_matches.sort(key=lambda x: x['date'])
    print(f"Found {len(all_matches)} calls")

    rec_list=[]
    for m in all_matches:
        if m.get('recording_id'):
            rec_list.append((m['recording_id'], m['date'], m['direction'], m['duration_seconds']))
        for e in m.get('additional_recordings', []):
            if e.get('recording_id'):
                rec_list.append((e['recording_id'], m['date'], e['action'], e['duration_seconds']))
    seen=set(); uniq=[]
    for rc in rec_list:
        if rc[0] not in seen:
            seen.add(rc[0]); uniq.append(rc)
    print(f"{len(uniq)} unique recordings")

    transcripts=[]
    if uniq:
        openai_key = wmill.get_variable("u/carter/openai_api_key")
        for rec_id, date, direction, duration in uniq:
            try:
                rr = platform.get(f"/restapi/v1.0/account/~/recording/{rec_id}/content")
                ab = rr.response().content
                size_mb = len(ab)/(1024*1024)
                if size_mb > 24.5:
                    transcripts.append({'recording_id':rec_id,'date':date,'direction':direction,
                                        'duration_seconds':duration,'size_mb':round(size_mb,2),
                                        'error':'exceeds 25MB whisper limit; needs chunking'})
                    continue
                t, err = transcribe_bytes(openai_key, ab)
                if err:
                    transcripts.append({'recording_id':rec_id,'date':date,'error':err})
                    print(f"  FAIL {date}: {err[:80]}")
                else:
                    t.update({'recording_id':rec_id,'date':date,'direction':direction,
                              'duration_seconds':duration,'size_mb':round(size_mb,2)})
                    transcripts.append(t)
                    print(f"  OK {date}: {t['full_text'][:60]}...")
                time.sleep(1)
            except Exception as e:
                transcripts.append({'recording_id':rec_id,'date':date,'error':str(e)[:200]})
                print(f"  ERR {str(e)[:100]}")

    out = {'phone_number':formatted,'window':[DATE_FROM,DATE_TO],
           'total_calls':len(all_matches),'calls_with_recordings':len(uniq),
           'calls':all_matches}
    if transcripts: out['transcripts']=transcripts
    return out
