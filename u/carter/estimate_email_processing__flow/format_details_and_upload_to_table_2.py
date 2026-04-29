from supabase import create_client
import wmill

def main(emails: list):
    url = wmill.get_variable("f/SUPABASE/URL")
    key = wmill.get_variable("f/SUPABASE/ANON_KEY")
    supabase = create_client(url, key)
    
    response = supabase.table('est_emails').upsert(
        emails,
        on_conflict='message_id'
    ).execute()
    
    return {"success": True, "inserted": len(response.data)}