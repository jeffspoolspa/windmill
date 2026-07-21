
import wmill
from ringcentral import SDK
import time

TARGETS = {
    "9125524127": "SMITH, JO (pmt 5/22)",
    "4043856544": "NUNN, SAM (pmt 6/1)",
    "7062023210": "COWSERT, AMY (pmt 6/3)",
    "9125734330": "DAKE, WARREN (pmt 6/5)",
    "4046335140": "WAITES, ADAM (pmt 6/9)",
    "9103791409": "WAITES, MELISSA (pmt 6/9)",
    "4043123001": "GRAY, ANNE (pmt 6/10)",
    "9122619702": "LINDSAY, KATHY (pmt 6/23)",
}

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

def main(days_back: int = 68):
    rc_resource = wmill.get_resource("u/carter/ring_central")
    rcsdk = SDK(
        rc_resource.get('RC_APP_CLIENT_ID'),
        rc_resource.get('RC_APP_CLIENT_SECRET'),
        "https://platform.ringcentral.com"
    )
    platform = rcsdk.platform()
    platform.login(jwt=rc_resource.get('RC_USER_JWT'))
    print("RC authenticated")

    from datetime import datetime, timedelta
    date_from = (datetime.utcnow() - timedelta(days=days_back)).strftime('%Y-%m-%dT00:00:00.000Z')

    results = {label: [] for label in TARGETS.values()}
    total_scanned = 0
    pages_hit = 0

    for page in range(1, 60):
        resp = safe_get(platform, '/restapi/v1.0/account/~/call-log', {
            'type': 'Voice',
            'dateFrom': date_from,
            'perPage': 250,
            'view': 'Detailed',
            'page': page
        })
        data = resp.json()
        records = data.records if hasattr(data, 'records') else []
        if not records:
            break
        pages_hit = page
        total_scanned += len(records)

        for r in records:
            from_num = ''
            to_num = ''
            from_name = ''
            to_name = ''
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

            hit_label = None
            for digits, label in TARGETS.items():
                if digits in (from_num or '') or digits in (to_num or ''):
                    hit_label = label
                    break
            if not hit_label:
                continue

            has_rec = hasattr(r, 'recording') and r.recording is not None
            rec_id = r.recording.id if has_rec else None
            match = {
                'date': r.startTime if hasattr(r, 'startTime') else '?',
                'duration_seconds': r.duration if hasattr(r, 'duration') else 0,
                'direction': r.direction if hasattr(r, 'direction') else '?',
                'result': r.result if hasattr(r, 'result') else '?',
                'from': f"{from_num} ({from_name})" if from_name else from_num,
                'to': f"{to_num} ({to_name})" if to_name else to_num,
                'recording_id': rec_id,
            }
            extra = []
            if hasattr(r, 'legs') and r.legs:
                for leg in r.legs:
                    leg_rec = leg.recording.id if hasattr(leg, 'recording') and leg.recording else None
                    if leg_rec and leg_rec != rec_id:
                        extra.append({
                            'recording_id': leg_rec,
                            'action': leg.action if hasattr(leg, 'action') else '?',
                            'duration_seconds': leg.duration if hasattr(leg, 'duration') else 0
                        })
            if extra:
                match['additional_recordings'] = extra
            results[hit_label].append(match)

        nav = data.navigation if hasattr(data, 'navigation') else None
        if not (nav and hasattr(nav, 'nextPage') and nav.nextPage):
            break
        time.sleep(1)

    for label in results:
        results[label].sort(key=lambda x: x['date'])

    print(f"Scanned {total_scanned} call records over {pages_hit} pages")
    summary = {label: len(calls) for label, calls in results.items()}
    print(f"Matches: {summary}")

    return {
        'total_records_scanned': total_scanned,
        'pages': pages_hit,
        'match_counts': summary,
        'matches': results
    }
