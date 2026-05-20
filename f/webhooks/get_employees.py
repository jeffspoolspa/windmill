import time
import wmill
import requests
from supabase import create_client
from datetime import datetime

GUSTO_API = "https://api.gusto-demo.com"
GUSTO_REDIRECT_URI = "https://jeffspoolspa.com"


def get_access_token():
    """Exchange refresh_token for a fresh access token. Gusto rotates refresh
    tokens on use, so we save the new one back to Windmill before returning."""
    client_id = wmill.get_variable("f/gusto/client_id")
    client_secret = wmill.get_variable("f/gusto/client_secret")
    refresh_token = wmill.get_variable("f/gusto/refresh_token")

    resp = requests.post(
        f"{GUSTO_API}/oauth/token",
        json={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": GUSTO_REDIRECT_URI,
        },
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    if resp.status_code != 200:
        print(f"Gusto OAuth failed: {resp.status_code} {resp.text[:500]}")
        resp.raise_for_status()
    payload = resp.json()

    new_refresh = payload.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        wmill.set_variable("f/gusto/refresh_token", new_refresh)

    return payload["access_token"]


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


def main():
    supa_url = wmill.get_variable("f/SUPABASE/URL")
    supa_key = wmill.get_variable("f/SUPABASE/ANON_KEY")
    supabase = create_client(supa_url, supa_key)

    company_id = wmill.get_variable("f/gusto/company_id")
    access_token = get_access_token()

    headers = {
        'Authorization': f'Bearer {access_token}',
        'X-Gusto-API-Version': '2025-06-15',
        'Accept': 'application/json'
    }

    emp_response = gusto_get(f"{GUSTO_API}/v1/companies/{company_id}/employees", headers)
    emp_response.raise_for_status()
    employees = emp_response.json()

    results = []

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

        branch_id = None
        work_addresses_response = gusto_get(f"{GUSTO_API}/v1/employees/{emp_uuid}/work_addresses", headers)

        if work_addresses_response.status_code == 200:
            work_addresses = work_addresses_response.json()
            if work_addresses and len(work_addresses) > 0:
                location = work_addresses[-1]
                branch_name = f"{location.get('city', '')}, {location.get('state', '')}".strip(', ')

                branch = supabase.table('branches').select('id').eq('name', branch_name).execute()
                if branch.data:
                    branch_id = branch.data[0]['id']
                else:
                    new_branch = supabase.table('branches').insert({'name': branch_name}).execute()
                    branch_id = new_branch.data[0]['id']

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

        results.append(result.data[0])

        time.sleep(0.15)

    return {"synced": len(results), "employees": results}
