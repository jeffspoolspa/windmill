
import wmill
from ringcentral import SDK
import time

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
    targets = {
        "Smith, Jo (pmt 5/22, $25.38)": ["9125524127"],
        "Nunn, Sam (pmt 6/01, $135.00)": ["4043856544", "4042291670", "4043858757", "2024151092"],
        "Cowsert, Amy (pmt 6/03, $135.00)": ["7062023210"],
        "Dake, Warren (pmt 6/05, $3136.47)": ["9125734330"],
        "Waites, Adam (pmt 6/09, $135.00)": ["4046335140", "4045198013", "9103791409"],
        "Gray, Anne (pmt 6/10, $150.00)": ["4043123001"],
        "Lindsay, Kathy (pmt 6/23, $135.00)": ["9122619702"],
    }

    rc_resource = wmill.get_resource("u/carter/ring_central")
    rcsdk = SDK(
        rc_resource.get('RC_APP_CLIENT_ID'),
        rc_resource.get('RC_APP_CLIENT_SECRET'),
        "https://platform.ringcentral.com"
    )
    platform = rcsdk.platform()
    platform.login(jwt=rc_resource.get('RC_USER_JWT'))
    print("RC authenticated")

    date_from = '2026-05-12T00:00:00.000Z'
    out = {}
    for cust, numbers in targets.items():
        cust_calls = []
        for digits in numbers:
            resp = safe_get(platform, '/restapi/v1.0/account/~/call-log', {
                'type': 'Voice',
                'dateFrom': date_from,
                'phoneNumber': digits,
                'perPage': 250,
                'view': 'Detailed'
            })
            data = resp.json()
            records = data.records if hasattr(data, 'records') else []
            for r in records:
                from_num = ''
                from_name = ''
                to_num = ''
                try:
                    from_obj = r.__dict__.get('from', None) or (r.from_ if hasattr(r, 'from_') else None)
                    if from_obj:
                        from_num = from_obj.phoneNumber if hasattr(from_obj, 'phoneNumber') else ''
                        from_name = from_obj.name if hasattr(from_obj, 'name') else ''
                except Exception:
                    pass
                try:
                    if hasattr(r, 'to') and r.to:
                        to_num = r.to.phoneNumber if hasattr(r.to, 'phoneNumber') else ''
                except Exception:
                    pass

                has_rec = hasattr(r, 'recording') and r.recording is not None
                rec_id = r.recording.id if has_rec else None
                extra = []
                if hasattr(r, 'legs') and r.legs:
                    for leg in r.legs:
                        leg_rec = leg.recording.id if hasattr(leg, 'recording') and leg.recording else None
                        if leg_rec and leg_rec != rec_id:
                            extra.append(leg_rec)
                cust_calls.append({
                    'matched_number': digits,
                    'date': r.startTime if hasattr(r, 'startTime') else '?',
                    'duration_s': r.duration if hasattr(r, 'duration') else 0,
                    'direction': r.direction if hasattr(r, 'direction') else '?',
                    'result': r.result if hasattr(r, 'result') else '?',
                    'from': f"{from_num} ({from_name})" if from_name else from_num,
                    'to': to_num,
                    'recording_id': rec_id,
                    'extra_recordings': extra
                })
            time.sleep(1.2)
        seen = set()
        uniq = []
        for c in sorted(cust_calls, key=lambda x: x['date']):
            k = (c['date'], c['from'], c['to'])
            if k not in seen:
                seen.add(k)
                uniq.append(c)
        out[cust] = uniq
        print(f"{cust}: {len(uniq)} calls")
    return out
