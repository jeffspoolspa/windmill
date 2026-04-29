"""
Generic SMS sender via RingCentral.
Logs activity via public.log_lead_activity RPC.
"""
# requirements:
# wmill
# ringcentral
# requests
# supabase
import re
import time
import wmill
from ringcentral import SDK
from supabase import create_client

OFFICE_CONFIG = {
    "richmond_hill": ("+19124590160", "PP_JWT"),
    "brunswick":     ("+19125540636", "RC_USER_JWT"),
    "st_marys":      ("+19125540636", "RC_USER_JWT"),
}


def _normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10: return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"): return "+" + digits
    if raw and raw.startswith("+"): return raw
    raise Exception(f"Invalid phone number: {raw}")


def _get_extension_for_number(platform, phone_number: str) -> str:
    resp = platform.get("/restapi/v1.0/account/~/extension")
    for ext in resp.json().records:
        phones = platform.get(f"/restapi/v1.0/account/~/extension/{ext.id}/phone-number").json().records
        for num in phones:
            if num.phoneNumber == phone_number and "SmsSender" in num.features:
                return ext.id
    raise Exception(f"No SMS-capable extension found for {phone_number}")


def _check_status(platform, message_id: str, max_attempts: int = 20) -> str:
    endpoint = f"/restapi/v1.0/account/~/extension/~/message-store/{message_id}"
    for _ in range(max_attempts):
        status = platform.get(endpoint).json().messageStatus
        if status != "Queued": return status
        time.sleep(4)
    return "Timeout"


def _log_activity(lead_id, result, body):
    try:
        url = wmill.get_variable("f/SUPABASE/URL")
        key = wmill.get_variable("f/SUPABASE/SERVICE_ROLE_KEY")
        client = create_client(url, key)
        client.rpc("log_lead_activity", {
            "p_lead_id": lead_id,
            "p_activity_type": "sms_sent",
            "p_description": body[:500],
            "p_metadata": {
                "message_id": result.get("message_id"),
                "conversation_id": result.get("conversation_id"),
                "to": result.get("to"),
                "from": result.get("from"),
                "office": result.get("office"),
                "status": result.get("status"),
                "body": body,
            },
            "p_created_by": "system:send_sms",
        }).execute()
    except Exception as e:
        print(f"[send_sms] activity log failed (non-fatal): {e}")


def main(to: str, body: str, office: str, lead_id: str):
    if not lead_id: raise Exception("lead_id is required.")
    if office not in OFFICE_CONFIG:
        raise Exception(f"Unknown office '{office}'. Expected one of {list(OFFICE_CONFIG)}.")
    if not body or not body.strip(): raise Exception("Message body is required.")

    from_number, jwt_key = OFFICE_CONFIG[office]
    recipient = _normalize_phone(to)

    rc = wmill.get_resource("u/carter/ring_central")
    sdk = SDK(rc["RC_APP_CLIENT_ID"], rc["RC_APP_CLIENT_SECRET"], "https://platform.ringcentral.com")
    platform = sdk.platform()
    platform.login(jwt=rc[jwt_key])

    ext_id = _get_extension_for_number(platform, from_number)
    resp = platform.post(
        f"/restapi/v1.0/account/~/extension/{ext_id}/sms",
        {
            "from": {"phoneNumber": from_number},
            "to": [{"phoneNumber": recipient}],
            "text": body,
        },
    ).json()

    message_id = resp.id
    conversation_id = resp.conversationId
    status = _check_status(platform, message_id)

    if status in ("SendingFailed", "DeliveryFailed"):
        raise Exception(f"SMS failed with status: {status}")

    result = {
        "success": True,
        "message_id": message_id,
        "conversation_id": conversation_id,
        "from": from_number,
        "to": recipient,
        "office": office,
        "lead_id": lead_id,
        "status": status,
        "body": body,
    }
    _log_activity(lead_id, result, body)
    return result
