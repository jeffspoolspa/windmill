
import wmill
from ringcentral import SDK
from datetime import datetime, timedelta
import time

def safe_get(platform, url, params, max_retries=5):
    for attempt in range(max_retries):
        try:
            resp = platform.get(url, params)
            return resp
        except Exception as e:
            if '429' in str(e):
                wait = 30 * (attempt + 1)
                print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
            else:
                raise
    raise Exception("Max retries exceeded")

def analyze_month(platform, ext_id, date_from, date_to, label, est_offset):
    all_calls = []
    for page in range(1, 25):
        resp = safe_get(platform, f'/restapi/v1.0/account/~/extension/{ext_id}/call-log', {
            'type': 'Voice', 'dateFrom': date_from, 'dateTo': date_to,
            'perPage': 250, 'view': 'Simple', 'page': page
        })
        data = resp.json()
        records = data.records if hasattr(data, 'records') else []
        if not records: break
        all_calls.extend(records)
        nav = data.navigation if hasattr(data, 'navigation') else None
        if not (nav and hasattr(nav, 'nextPage') and nav.nextPage): break
        time.sleep(2)
    
    calls = []
    for r in all_calls:
        result = r.result if hasattr(r, 'result') else '?'
        direction = r.direction if hasattr(r, 'direction') else '?'
        duration = r.duration if hasattr(r, 'duration') else 0
        start = r.startTime if hasattr(r, 'startTime') else None
        if not start: continue
        start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
        et_dt = start_dt - timedelta(hours=est_offset)
        calls.append({'start_dt': start_dt, 'et_dt': et_dt, 'duration': duration or 0, 'direction': direction, 'result': result})
    
    calls.sort(key=lambda x: x['start_dt'])
    
    answered = missed = outbound = other = talk_sec = 0
    missed_busy = missed_1min = missed_avoidable = 0
    avoidable_by_bucket = {}
    avoidable_by_dow = {}
    
    for i, c in enumerate(calls):
        d, r = c['direction'], c['result']
        if d == 'Outbound': outbound += 1; talk_sec += c['duration']
        elif r == 'Accepted': answered += 1; talk_sec += c['duration']
        elif r == 'Missed':
            missed += 1
            miss_time = c['start_dt']; et = c['et_dt']
            was_busy = within_1min = False
            for j, o in enumerate(calls):
                if i == j: continue
                o_start = o['start_dt']
                o_end = datetime.fromtimestamp(o_start.timestamp() + o['duration'], tz=o_start.tzinfo)
                if o_start <= miss_time <= o_end and o['result'] in ['Accepted', 'Call connected']:
                    was_busy = True; break
                gap = (miss_time - o_end).total_seconds()
                if 0 <= gap <= 60 and o['result'] in ['Accepted', 'Call connected']: within_1min = True
            if was_busy: missed_busy += 1
            elif within_1min: missed_1min += 1
            else: missed_avoidable += 1
            if not (was_busy or within_1min):
                h = et.hour
                b = 'Before 9am' if h<9 else '9am-12pm' if h<12 else '12-1pm' if h<13 else '1-3pm' if h<15 else '3-5pm' if h<17 else 'After 5pm'
                avoidable_by_bucket[b] = avoidable_by_bucket.get(b, 0) + 1
                dow = et.strftime('%A')
                avoidable_by_dow[dow] = avoidable_by_dow.get(dow, 0) + 1
        else: other += 1
    
    total_inbound = answered + missed + other
    return {
        'total_calls': len(calls), 'answered': answered, 'missed': missed,
        'outbound': outbound, 'other': other, 'talk_hours': round(talk_sec/3600, 1),
        'answer_rate_pct': round(answered/total_inbound*100, 1) if total_inbound > 0 else 0,
        'missed_while_busy': missed_busy, 'missed_within_1min': missed_1min,
        'avoidable_misses': missed_avoidable,
        'avoidable_by_time': avoidable_by_bucket, 'avoidable_by_dow': avoidable_by_dow,
    }

def main(extension_id: str = "1297600051", months_back: int = 6):
    """
    Analyze call patterns for an extension over multiple months.
    Tracks answered/missed/outbound, avoidable missed calls, and patterns by time/day.
    
    Args:
        extension_id: RC extension ID (default Anna Osorio)
        months_back: How many months to analyze
    """
    rc_resource = wmill.get_resource("u/carter/ring_central")
    rcsdk = SDK(rc_resource.get('RC_APP_CLIENT_ID'), rc_resource.get('RC_APP_CLIENT_SECRET'), "https://platform.ringcentral.com")
    platform = rcsdk.platform()
    platform.login(jwt=rc_resource.get('RC_USER_JWT'))
    
    # Build month ranges dynamically
    now = datetime.utcnow()
    months = []
    for i in range(months_back, 0, -1):
        # First of month i months ago
        m_start = (now.replace(day=1) - timedelta(days=30*i)).replace(day=1)
        # First of next month
        if m_start.month == 12:
            m_end = m_start.replace(year=m_start.year+1, month=1)
        else:
            m_end = m_start.replace(month=m_start.month+1)
        
        # EDT (Mar-Nov) vs EST (Nov-Mar)
        offset = 4 if 3 <= m_start.month <= 10 else 5
        label = m_start.strftime('%b %Y')
        months.append((
            m_start.strftime('%Y-%m-%dT%H:00:00.000Z').replace(m_start.strftime('%H'), f"{offset:02d}"),
            m_end.strftime('%Y-%m-%dT%H:00:00.000Z').replace(m_end.strftime('%H'), f"{(4 if 3<=m_end.month<=10 else 5):02d}"),
            label, offset
        ))
    
    results = {}
    for df, dt, label, offset in months:
        print(f"\nProcessing {label}...")
        data = analyze_month(platform, extension_id, df, dt, label, offset)
        results[label] = data
        print(f"✓ {label}: {data['total_calls']} calls, {data['missed']} missed ({data['avoidable_misses']} avoidable), {data['answer_rate_pct']}%")
        time.sleep(5)
    
    return results
