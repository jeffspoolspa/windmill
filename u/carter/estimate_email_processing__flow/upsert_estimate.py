import wmill
from supabase import create_client
from datetime import date

def map_to_db_columns(wo_details: dict) -> dict:
    field_mapping = {
        'Work Order #': 'wo_number',
        'Acceptance Link': 'acceptance_link',
        'Scheduled For': 'scheduled',
        'Assigned To': 'assigned_to',
        'Work Description': 'work_description',
        'Subtotal': 'sub_total',
        "To" : "email_address",
        "Cc": "additional_contacts",
    }
    db_data = {}
    for key, value in wo_details.items():
        if key in field_mapping:
            db_data[field_mapping[key]] = value
    db_data['status'] = 'active'
    db_data['approval_status'] = 'Pending Approval'
    db_data['last_sent'] = date.today().isoformat()
    return db_data

def upsert_work_order(client, wo_details: dict):
    db_data = map_to_db_columns(wo_details)
    db_data['sub_total'] = float(db_data.get('sub_total').replace('$', '').replace(',', '').strip())
    wo_number = db_data.get('wo_number')
    if not wo_number:
        raise ValueError("Work Order # is required")
    print(f"Upserting Work Order #{wo_number}")
    response = client.table('estimates').upsert(db_data, on_conflict='wo_number').execute()
    print(f"Upserted Work Order #{wo_number}")
    return {'work_order_number': wo_number, 'data': response.data}

def main(x: dict):
    url = wmill.get_variable("f/SUPABASE/URL")
    key = wmill.get_variable("f/SUPABASE/ANON_KEY")
    client = create_client(url,key)
    result = upsert_work_order(client,x)
    return result