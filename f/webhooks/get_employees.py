import wmill
import requests
from supabase import create_client
from datetime import datetime

def main():
    supa_url = wmill.get_variable("f/SUPABASE/URL")
    supa_key = wmill.get_variable("f/SUPABASE/ANON_KEY")
    supabase = create_client(supa_url, supa_key)
    
    company_id = wmill.get_variable("f/gusto/company_id")
    token = wmill.get_variable("f/gusto/personal_access_token")
    
    headers = {
        'Authorization': f'Bearer {token}',
        'X-Gusto-API-Version': '2025-06-15',
        'Accept': 'application/json'
    }
    
    # Get all employees
    emp_url = f"https://api.gusto.com/v1/companies/{company_id}/employees"
    emp_response = requests.get(emp_url, headers=headers)
    emp_response.raise_for_status()
    employees = emp_response.json()
    
    results = []
    
    for emp in employees:
        emp_uuid = emp['uuid']
        
        # Fetch detailed employee data
        detail_url = f"https://api.gusto.com/v1/employees/{emp_uuid}"
        detail_response = requests.get(detail_url, headers=headers)
        detail_response.raise_for_status()
        emp_data = detail_response.json()
        
        # Get or create department
        dept_id = None
        dept_name = emp_data.get('department')
        if dept_name:
            dept = supabase.table('departments').select('id').eq('name', dept_name).execute()
            if dept.data:
                dept_id = dept.data[0]['id']
            else:
                new_dept = supabase.table('departments').insert({'name': dept_name}).execute()
                dept_id = new_dept.data[0]['id']
        
        # Get work addresses for employee
        branch_id = None
        work_addresses_url = f"https://api.gusto.com/v1/employees/{emp_uuid}/work_addresses"
        work_addresses_response = requests.get(work_addresses_url, headers=headers)
        
        if work_addresses_response.status_code == 200:
            work_addresses = work_addresses_response.json()
            if work_addresses and len(work_addresses) > 0:
                # Take the last address
                location = work_addresses[-1]
                branch_name = f"{location.get('city', '')}, {location.get('state', '')}".strip(', ')
                
                branch = supabase.table('branches').select('id').eq('name', branch_name).execute()
                if branch.data:
                    branch_id = branch.data[0]['id']
                else:
                    new_branch = supabase.table('branches').insert({'name': branch_name}).execute()
                    branch_id = new_branch.data[0]['id']
        
        # Determine status
        if emp_data.get('terminated'):
            status = 'terminated'
        elif emp_data.get('onboarding_status') != 'onboarding_completed':
            status = 'onboarding'
        else:
            status = 'active'
        
        # Get hire date
        hire_date = None
        jobs = emp_data.get('jobs', [])
        if jobs:
            hire_date = jobs[0].get('hire_date')
        
        # Insert/update employee
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
    
    return {"synced": len(results), "employees": results}