
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
            time.sleep(0)
        out[cust] = cust_calls
    return out
