import wmill
from supabase import create_client

def main(body: dict):
    # Extract message body from nested structure
    message_body = body
    supabase_url = wmill.get_variable("f/SUPABASE/URL")
    supabase_key = wmill.get_variable("f/SUPABASE/ANON_KEY")
    
    # Map to your database columns
    message_data = {
        'message_id': message_body.get('id'),
        'to_number': message_body.get('to', [{}])[0].get('phoneNumber'),
        'from_number': message_body.get('from', {}).get('phoneNumber'),
        'message': message_body.get('subject'),
        'direction': message_body.get('direction'),
        'readStatus': message_body.get('readStatus'),
        'conversation_id': message_body.get('conversation', {}).get('id'),
        'messageStatus': message_body.get('messageStatus'),
        'eventType': message_body.get('eventType')
    }
    
    print(f"💾 Saving message {message_data['message_id']}")
    print(f"   From: {message_data['from_number']}")
    print(f"   Text: {message_data['message']}")
    
    # Upsert to Supabase
    supabase = create_client(supabase_url, supabase_key)
    supabase.table('text_messages').upsert(
        message_data,
        on_conflict='message_id'
    ).execute()
    
    print("✅ Saved to Supabase!")
    return {'success': True, 'message_id': message_data['message_id']}






