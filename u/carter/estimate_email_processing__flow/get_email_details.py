#requirements:
#google-auth
#requests
#wmill

import wmill
import requests as http_requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request as AuthRequest

def main(message_id_header: str):
    service_account_json = wmill.get_resource("u/carter/gmail_gcp_service_account")
    
    if 'private_key' in service_account_json:
        service_account_json['private_key'] = service_account_json['private_key'].replace('\\n', '\n')
    
    credentials = service_account.Credentials.from_service_account_info(
        service_account_json,
        scopes=['https://www.googleapis.com/auth/gmail.readonly']
    )
    
    delegated_credentials = credentials.with_subject('jpsbilling@jeffspoolspa.com')
    delegated_credentials.refresh(AuthRequest())
    
    headers = {'Authorization': f'Bearer {delegated_credentials.token}'}
    
    search_url = f'https://gmail.googleapis.com/gmail/v1/users/me/messages?q=rfc822msgid:{message_id_header}'
    search_response = http_requests.get(search_url, headers=headers)
    search_response.raise_for_status()
    
    results = search_response.json()
    if not results.get('messages'):
        return {"error": "Message not found"}
    
    gmail_id = results['messages'][0]['id']
    
    msg_url = f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{gmail_id}?format=full'
    response = http_requests.get(msg_url, headers=headers)
    response.raise_for_status()
    
    return response.json()