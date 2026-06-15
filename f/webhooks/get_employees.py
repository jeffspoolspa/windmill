import re
import time
import wmill
import requests
from supabase import create_client
from datetime import datetime

GUSTO_API = "https://api.gusto.com"
MAINTENANCE_DEPARTMENT_ID = "757659e3-d73f-48c3-999f-6f071f1e3587"
TECH_EMAIL_DOMAIN = "techs.jeffspoolspa.internal"
DEFAULT_TECH_PASSWORD = "Swimming#1"
NAME_SUFFIX_RE = re.compile(r"[\s,]+(jr|sr|ii|iii|iv|v)\.?$", re.IGNORECASE)


def gusto_get(url, headers, max_retries=5):
    """GET with 429 backoff using the Retry-After header (default 30s)."""
    for attempt in range(max_retries):
        resp = requests.get(url, headers=headers)
        if resp.status_code != 429:
            return resp
        wait = int(resp.headers.get("Retry-After", "30"))
        print(f"429 from {url}; sleeping {wait}s (attempt {attempt + 1}/{max_retries})")
        time.sleep(wait)
    return resp


def strip_name_suffix(last_name):
    """Strip trailing generational suffixes like Jr, Sr, II, III, IV, V."""
    if not last_name:
        return last_name
    return NAME_SUFFIX_RE.sub("", last_name).strip()


def derive_base_username(first_name, last_name):
    """first letter of first name + last name (with generational suffix stripped),
    sanitized to a-z, lowercased."""
    if not first_name or not last_name:
        return None
    last_name = strip_name_suffix(last_name)
    first_clean = re.sub(r"[^a-z]", "", first_name.lower())
    last_clean = re.sub(r"[^a-z]", "", last_name.lower())
    if not first_clean or not last_clean:
        return None
    return f"{first_clean[0]}{last_clean}"


def unique_tech_username(supabase, base, employee_id):
    """Append digits if needed to avoid colliding with another employee's tech_username."""
    candidate = base
    suffix = 2
    while True:
        existing = (
            supabase.table("employees")
            .select("id")
            .eq("tech_username", candidate)
            .neq("id", employee_id)
            .limit(1)
            .execute()
        )
        if not existing.data:
            return candidate
        candidate = f"{base}{suffix}"
        suffix += 1


def ensure_tech_login(supabase, emp_row):
    """Create a tech login for active maintenance employees without one.
    Returns True if a new login was created, False otherwise."""
    if emp_row.get("department_id") != MAINTENANCE_DEPARTMENT_ID:
        return False
    if emp_row.get("status") != "active":
        return False
    if emp_row.get("auth_user_id"):
        return False

    base = derive_base_username(emp_row.get("first_name"), emp_row.get("last_name"))
    if not base or len(base) < 2:
        print(f"Skipping tech login: name doesn't sanitize to a valid username (emp {emp_row.get('id')})")
        return False

    username = unique_tech_username(supabase, base, emp_row["id"])
    synthetic_email = f"{username}@{TECH_EMAIL_DOMAIN}"

    try:
        auth_resp = supabase.auth.admin.create_user({
            "email": synthetic_email,
            "password": DEFAULT_TECH_PASSWORD,
            "email_confirm": True,
        })
    except Exception as e:
        print(f"Failed to create auth user {synthetic_email}: {e}")
        return False

    auth_user_id = auth_resp.user.id

    try:
        supabase.table("employees").update({
            "auth_user_id": auth_user_id,
            "tech_username": username,
        }).eq("id", emp_row["id"]).execute()
        print(f"Created tech login {username} for {emp_row.get('first_name')} {emp_row.get('last_name')}")
        return True
    except Exception as e:
        # Roll back orphaned auth user so retry has a clean slate
        try:
            supabase.auth.admin.delete_user(auth_user_id)
        except Exception:
            pass
        print(f"Failed to link auth user to employee {emp_row['id']}: {e}")
        return False


def main():
    supa_url = wmill.get_variable("f/SUPABASE/URL")
    supa_key = wmill.get_variable("f/SUPABASE/SERVICE_ROLE_KEY")
    supabase = create_client(supa_url, supa_key)

    company_id = wmill.get_variable("f/gusto/company_id")
    token = wmill.get_variable("f/gusto/personal_access_token")

    headers = {
        'Authorization': f'Bearer {token}',
        'X-Gusto-API-Version': '2025-06-15',
        'Accept': 'application/json'
    }

    emp_response = gusto_get(f"{GUSTO_API}/v1/companies/{company_id}/employees", headers)
    emp_response.raise_for_status()
    employees = emp_response.json()

    results = []
    new_logins = 0

    for emp in employees:
        emp_uuid = emp['uuid']

        detail_response = gusto_get(f"{GUSTO_API}/v1/employees/{emp_uuid}", headers)
        detail_response.raise_for_status()
        emp_data = detail_response.json()

        dept_id = None
        dept_name = emp_data.get('department')
        if dept_name:
            dept = supabase.table('departments').select('id').eq('name', dept_name).execute()
            if dept.data:
                dept_id = dept.data[0]['id']
            else:
                new_dept = supabase.table('departments').insert({'name': dept_name}).execute()
                dept_id = new_dept.data[0]['id']

        # FK the employee to their office (branch) by Gusto location_uuid. The
        # office table is maintained by f/gusto/sync_offices; this sync no longer
        # creates branches. If the office isn't synced yet, branch_id stays null
        # until the next weekly office sync fills it in.
        branch_id = None
        work_addresses_response = gusto_get(f"{GUSTO_API}/v1/employees/{emp_uuid}/work_addresses", headers)

        if work_addresses_response.status_code == 200:
            work_addresses = work_addresses_response.json() or []
            active_addrs = [w for w in work_addresses if w.get('active')]
            wa = active_addrs[-1] if active_addrs else (work_addresses[-1] if work_addresses else None)
            loc_uuid = wa.get('location_uuid') if wa else None
            if loc_uuid:
                branch = (
                    supabase.table('branches')
                    .select('id')
                    .eq('gusto_location_uuid', loc_uuid)
                    .limit(1)
                    .execute()
                )
                if branch.data:
                    branch_id = branch.data[0]['id']

        if emp_data.get('terminated'):
            status = 'terminated'
        elif emp_data.get('onboarding_status') != 'onboarding_completed':
            status = 'onboarding'
        else:
            status = 'active'

        hire_date = None
        jobs = emp_data.get('jobs', [])
        if jobs:
            hire_date = jobs[0].get('hire_date')

        employee_record = {
            'gusto_uuid': emp_uuid,
            'employee_code': emp_data.get('employee_code'),
            'first_name': emp_data.get('first_name'),
            'last_name': emp_data.get('last_name'),
            'hire_date': hire_date,
            'status': status,
            'email': emp_data.get('email'),
            'phone': emp_data.get('phone'),
            'department_id': dept_id,
            'branch_id': branch_id,
            'updated_at': datetime.now().isoformat()
        }

        result = supabase.table('employees').upsert(
            employee_record,
            on_conflict='gusto_uuid'
        ).execute()

        emp_row = result.data[0]
        results.append(emp_row)

        try:
            if ensure_tech_login(supabase, emp_row):
                new_logins += 1
        except Exception as e:
            print(f"Error while ensuring tech login for {emp_uuid}: {e}")

        time.sleep(0.15)

    return {"synced": len(results), "new_tech_logins": new_logins}
