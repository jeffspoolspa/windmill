import requests
import wmill
from supabase import create_client
import time

def get_access_token():
    state = wmill.get_state() or {}
    
    # Return cached token if still valid
    if state.get("token") and state.get("expires_at", 0) > time.time():
        return state["token"]
    
    # Refresh token
    token_url = "https://accounts.zoho.com/oauth/v2/token"
    data = {
        "refresh_token": wmill.get_variable("u/ZOHO/REFRESH_TOKEN"),
        "client_id": wmill.get_variable("u/ZOHO/CLIENT_ID"),
        "client_secret": wmill.get_variable("u/ZOHO/CLIENT_SECRET"),
        "grant_type": "refresh_token"
    }
    
    token_response = requests.post(token_url, data=data)
    token = token_response.json().get("access_token")
    
    # Cache for 50 minutes
    wmill.set_state({"token": token, "expires_at": time.time() + 3000})
    return token

def main(zoho_item_id: str, zoho_location_id: int):
    # Get Zoho stock for this item
    org_id = "870657839"
    access_token = get_access_token()
    response = requests.get(
        f"https://www.zohoapis.com/inventory/v1/items/{zoho_item_id}?organization_id={org_id}",
        headers={"Authorization": f"Zoho-oauthtoken {access_token}"}
    )
    detail = response.json()
    
    if detail.get('code') != 0 or "item" not in detail:
        raise Exception("Item not found")
    
    # Find stock at this location
    locations = detail["item"].get("locations", [])
    location_stock = next(
        (loc for loc in locations if loc["location_id"] == zoho_location_id),
        None
    )
    
    return {
        "success": True,
        "item_name": detail["item"].get("name"),
        "sku": detail["item"].get("sku"),
        "quantity_on_hand": location_stock["location_stock_on_hand"] if location_stock else 0,
        "available_stock": location_stock["location_available_stock"] if location_stock else 0,
        "zoho_item_id": zoho_item_id
    }