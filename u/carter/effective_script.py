from bs4 import BeautifulSoup
from html import unescape
import re
import wmill
from supabase import create_client

def clean_text(s: str | None) -> str | None:
    """Convert HTML → plain text for database storage (no tabs/newlines/NBSP)."""
    if not s:
        return None
    s = unescape(s)
    # Normalize common HTML/email whitespace issues
    s = s.replace('\xa0', ' ')  # NBSP -> space
    s = s.replace('\t', ' ')    # tabs -> space
    s = s.replace('\r', ' ')    # CR -> space
    s = s.replace('\n', ' ')    # LF -> space
    # Collapse all runs of whitespace to a single space
    s = re.sub(r'\s+', ' ', s).strip()
    # Also handle literal escape sequences like "\n" or "\t" if they appear
    s = re.sub(r'\\[nrt]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s or None

def extract_details(html_b: str):
    results = {}
    soup = BeautifulSoup(html_b, "html.parser")
    
    # Find header row by its text
    meta_labels_row = soup.find(lambda t: t.name == "tr" and "WO Type" in t.get_text())
    print("Found meta row:", meta_labels_row is not None)
    
    if meta_labels_row:
        values_row = meta_labels_row.find_next_sibling("tr")
        labels = [clean_text(td.get_text(separator=" ", strip=True))
                  for td in meta_labels_row.find_all("td")]
        vals = [clean_text(td.get_text(separator=" ", strip=True))
                for td in values_row.find_all("td")] if values_row else []
        
        for k, v in zip(labels, vals):
            results[k] = v
        
        # Sanity check: no control chars
        for k, v in results.items():
            assert not re.search(r'[\t\r\n]', v or ""), f"Control char in {k}: {repr(v)}"
    
    # Description & Instructions
    details_td = soup.find('td', width="30%")
    if details_td:
        text = details_td.get_text()
        if 'Description' in text:
            after = text.split('Description', 1)[1]
            if 'Instructions' in after:
                desc, inst = after.split('Instructions', 1)
                results['Description'] = clean_text(desc)
                results['Instructions'] = clean_text(inst)
    
    # Work Order Number
    header = soup.find("h3")
    if header:
        wo_number = re.search(r"\d+", header.get_text()).group(0)
        results["Work Order #"] = wo_number
    
    # Customer Name
    customer_name_tag = soup.find('b')
    if customer_name_tag:
        results['Customer'] = clean_text(customer_name_tag.text)
    
    return results

def map_to_db_columns(wo_details: dict) -> dict:
    """Map extracted fields to database column names (snake_case)."""
    # Adjust these column names to match your actual Supabase table schema
    field_mapping = {
        'Work Order #': 'wo_number',
        'Customer': 'customer',
        'WO Type': 'type',
        'Terms': 'inv_terms',
        'Scheduled For': 'scheduled',
        'Assigned To': 'assigned_to',
        'Work Description': 'work_description',
        'Instructions': 'technician_instructions'
    }
    
    db_data = {}
    for key, value in wo_details.items():
        if key in field_mapping:
            db_data[field_mapping[key]] = value
    
    return db_data

def upsert_work_order(client, wo_details: dict):
    """Simpler upsert using Supabase's upsert feature."""
    
    db_data = map_to_db_columns(wo_details)
    wo_number = db_data.get('wo_number')
    
    if not wo_number:
        raise ValueError("Work Order # is required")
    
    print(f"Upserting Work Order #{wo_number}")
    
    # Supabase upsert: insert if not exists, update if exists
    # NOTE: Requires 'work_order_number' to be a unique key in your table
    response = client.table('work_orders')\
        .upsert(db_data, on_conflict='wo_number')\
        .execute()
    
    print(f"Upserted Work Order #{wo_number}")
    return {
        'work_order_number': wo_number,
        'data': response.data
    }

def main(raw_email: dict, parsed_email: dict):
    # Extract work order details from HTML
    wo_details = extract_details(parsed_email.get('html_body'))
    print("Extracted details:", wo_details)
    
    # Connect to Supabase
    url = wmill.get_variable("f/SUPABASE/URL")
    key = wmill.get_variable("f/SUPABASE/ANON_KEY")
    client = create_client(url, key)
    
    # Upsert to database
    try:
        result = upsert_work_order(client, wo_details)
        return {
            'success': True,
            'result': result,
            'extracted_data': wo_details
        }
    except Exception as e:
        print(f"Error upserting work order: {str(e)}")
        return {
            'success': False,
            'error': str(e),
            'extracted_data': wo_details
        }