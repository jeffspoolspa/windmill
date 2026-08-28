#requirements:
#requests
#google-auth
#wmill
# supabase==2.8.1
import time
import wmill
from supabase import create_client
from u.carter.get_est_emails import main as get_est_emails

def main(budget_seconds: int = 3000):
    url = wmill.get_variable("f/SUPABASE/URL")
    key = wmill.get_variable("f/SUPABASE/ANON_KEY")
    sb = create_client(url, key)

    rows = (sb.table('est_emails').select('wo_number')
            .is_('body_html', 'null').is_('body_text', 'null')
            .not_.is_('wo_number', 'null')
            .limit(5000).execute()).data
    wos = sorted({r['wo_number'] for r in rows})
    print(f"{len(wos)} WOs still snippet-only")

    start = time.time()
    done, errors = [], []
    for i, wo in enumerate(wos, 1):
        if time.time() - start > budget_seconds:
            break
        try:
            get_est_emails(wo, 'jpsbilling@jeffspoolspa.com')
            done.append(wo)
        except Exception as e:
            errors.append({'wo': wo, 'error': str(e)[:200]})
        if i % 25 == 0:
            print(f"--- {i}/{len(wos)} done, {len(errors)} errors, {int(time.time()-start)}s elapsed")
    return {'processed': len(done), 'errors': errors, 'remaining': len(wos) - len(done) - len(errors)}