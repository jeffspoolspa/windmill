from datetime import date
import wmill
from ringcentral import SDK
import time
from supabase import create_client, Client
import base64
import re
import json

def retrieve_estimate(wo_number: str):
    """Retrieve estimate from Supabase"""
    url = wmill.get_variable("f/SUPABASE/URL")
    key = wmill.get_variable("f/SUPABASE/ANON_KEY")
    supabase: Client = create_client(url, key)
    
    response = supabase.rpc('get_estimate_with_pdf', {'p_wo_number': wo_number}).execute()
    
    if not response.data or len(response.data) == 0:
        return None
    
    return response.data[0]


def verify_and_send_sms(platform, company_number, recipient, estimate, pdf_url):
    """Verify the company number has SMS capability, then send"""
    try:
        # Get all phone numbers
        resp = platform.get("/restapi/v1.0/account/~/extension/~/phone-number")
        jsonObj = resp.json()
        
        # Look for the company number specifically
        for record in jsonObj.records:
            if record.phoneNumber == company_number:
                # Check if it has SMS capability
                if "SmsSender" in record.features:
                    print(f"✓ Verified {company_number} has SMS capability")
                    return send_sms(platform, company_number, recipient, estimate, pdf_url)
                else:
                    raise Exception({"error": f"Phone number {company_number} does not have SMS capability"})
        
        # Company number not found in account
        raise Exception({"error": f"Phone number {company_number} not found in your RingCentral account"})
        
    except Exception as e:
        error_str = str(e)
        
        raise Exception(f"{error_str}")

def get_extension_for_number(platform, phone_number):
    """Find which extension owns a phone number"""
    resp = platform.get("/restapi/v1.0/account/~/extension")
    extensions = resp.json().records
    
    for ext in extensions:
        # Get phone numbers for this extension
        phone_resp = platform.get(f"/restapi/v1.0/account/~/extension/{ext.id}/phone-number")
        numbers = phone_resp.json().records
        
        for num in numbers:
            if num.phoneNumber == phone_number and "SmsSender" in num.features:
                return ext.id
    
    raise Exception(f"No extension found with SMS capability for {phone_number}")

def send_sms(platform, from_number, recipient, estimate, message):
    """Send SMS with PDF link"""
    
    customer_name = estimate.get('customer', 'Valued Customer')
    extension_id = get_extension_for_number(platform, from_number)
    
    try:
        bodyParams = {
            'from': {'phoneNumber': from_number},
            'to': [{'phoneNumber': recipient}],
            'text': message
        }
        
        endpoint = f"/restapi/v1.0/account/~/extension/{extension_id}/sms"
        resp = platform.post(endpoint, bodyParams)
        jsonObj = resp.json()
        
        message_id = jsonObj.id
        conversation_id = jsonObj.conversationId
            # ✅ FIX: Access attributes directly instead of using .get()
        try:
            from_number_formatted = jsonObj.from_.phoneNumber  # Note: 'from' becomes 'from_' in Python
        except AttributeError:
            from_number_formatted = from_number
        
        print(conversation_id)
        print(f"SMS sent. Message id: {message_id}")
        
        status = check_message_status(platform, message_id)

        if status in ["SendingFailed", "DeliveryFailed"]:
            raise Exception(f"SMS failed with status: {status}")

        return {
            "success": True,
            "message_id": message_id,
            "message": message,
            "from": from_number_formatted,
            "to": recipient,
            "final_status": status,
            "conversation_id": conversation_id
        }
        
    except Exception as e:
        
        raise Exception(f"{e}")

def check_message_status(platform, message_id):
    try:
        endpoint = f"/restapi/v1.0/account/~/extension/~/message-store/{message_id}"
        
        max_attempts = 20
        attempts = 0
        
        while attempts < max_attempts:
            resp = platform.get(endpoint)
            jsonObj = resp.json()
            status = jsonObj.messageStatus
            
            print(f"Message status: {status}")
            
            if status != "Queued":
                return status
            
            time.sleep(4)
            attempts += 1
        
        return "Timeout - still queued"
        
    except Exception as e:
        print(f"Error checking status: {str(e)}")
        raise Exception("Message timed out")

def main(wo_number: str, home_phone: str, mobile_phone: str, office: str, message: str):
    """Main entry point"""
    # Step 1: Get estimate
    estimate = retrieve_estimate(wo_number)
    
    if not estimate:
        raise Exception({"error": f"Estimate {wo_number} not found"})
    
    print(f"✓ Retrieved estimate {wo_number}")
    
    # Step 3: Initialize RingCentral
    rc_resource = wmill.get_resource("u/carter/ring_central")
    
    rcsdk = SDK(
        rc_resource.get('RC_APP_CLIENT_ID'),
        rc_resource.get('RC_APP_CLIENT_SECRET'),
        "https://platform.ringcentral.com"
    )
    
    platform = rcsdk.platform()


        # Step 5: Verify and send SMS
    if office == "Brunswick" or office == "St Marys":
        company_number = "+19125540636"
        jwt_token = rc_resource.get('RC_USER_JWT')
    elif office == "Richmond Hill":
        company_number = "+19124590160"
        jwt_token = rc_resource.get('PP_JWT')
    else:
        raise Exception("Please select an office")

    recipient = re.sub(r'\D', '', mobile_phone or home_phone)
    
    # Step 4: Authenticate
    try:
        platform.login(jwt=jwt_token)
        print("✓ Successfully authenticated to RingCentral")
    except Exception as e:
        raise Exception({"error": f"Unable to authenticate: {str(e)}"})
    
    result = send_sms(platform, company_number, recipient, estimate, message)

    url = wmill.get_variable("f/SUPABASE/URL")
    key = wmill.get_variable("f/SUPABASE/ANON_KEY")
    supabase: Client = create_client(url, key)
    update = (
        supabase.table('estimates')
        .update({"sms_conversation_id": result.get("conversation_id"), "follow_up_date": str(date.today())})
        .eq("wo_number", wo_number)
        .execute()
    )

    text_message_data = {
        'message_id': result.get('message_id'),
        'to_number': result.get('to'),
        'from_number': result.get('from'),
        'message': result.get('message'),
        'direction': 'Outbound',  # You're sending the message
        'readStatus': 'Read',     # Outbound messages are marked as read
        'conversation_id': result.get('conversation_id'),
        'messageStatus': result.get('final_status'),  # "Delivered", "Sent", etc.
        'eventType': 'Create'     # This is a new message being created
    }

    # Insert into text_messages table
    insert_result = (
        supabase.table('text_messages')
        .insert(text_message_data)
        .execute()
    )

    update = (
        supabase.table('estimates')
        .update({'last_sent': date.today().isoformat()})
        .eq('wo_number', wo_number)
        .execute()
    )

    return result