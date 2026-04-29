import wmill
from supabase import create_client

def main(wo_number: str, work_description: str = None, assigned_to: str = None, email_address: str = None):
    """
    Ensure a work_orders row exists for the given WO number before
    we try to insert into the estimates table (which has a FK to work_orders).
    Uses INSERT ... ON CONFLICT DO NOTHING so existing rows from ION sync
    are never overwritten.

    Uses SERVICE_ROLE_KEY because work_orders has RLS that only allows
    anon SELECT, not INSERT. Service role bypasses RLS.
    """
    url = wmill.get_variable("f/SUPABASE/URL")
    key = wmill.get_variable("f/SUPABASE/SERVICE_ROLE_KEY")
    client = create_client(url, key)

    stub = {
        'wo_number': wo_number,
        'type': 'ESTIMATE',
        'wo_status': 'Open',
        'work_description': work_description,
        'assigned_to': assigned_to,
        'email_address': email_address,
    }
    stub = {k: v for k, v in stub.items() if v is not None}

    response = client.table('work_orders').upsert(
        stub,
        on_conflict='wo_number',
        ignore_duplicates=True
    ).execute()

    print(f"Ensured work_orders row exists for WO #{wo_number}")
    return {'wo_number': wo_number, 'created': len(response.data) > 0}