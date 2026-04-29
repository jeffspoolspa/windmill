#requirements:
#pdfplumber
#psycopg2-binary
#wmill

import pdfplumber
import psycopg2
import wmill
import base64
import re
import json
import tempfile
import os


def get_supabase_conn():
    db = wmill.get_resource("u/carter/supabase")
    return psycopg2.connect(
        host=db["host"], port=db["port"], dbname=db["dbname"],
        user=db["user"], password=db["password"],
        sslmode=db.get("sslmode", "require")
    )


def extract_invoice_data(pdf_bytes):
    """Extract structured data from an Allied Universal invoice PDF."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        with pdfplumber.open(tmp_path) as pdf:
            page = pdf.pages[0]
            text = page.extract_text()
            if not text:
                return {"error": "No text extracted from PDF"}

            full_text = text

            # Invoice Number
            inv_match = re.search(r'Invoice No[:\s]+(\S+)', full_text)
            invoice_number = inv_match.group(1) if inv_match else None

            # Invoice Date
            inv_date_match = re.search(r'Invoice Date[:\s]+([\d/]+)', full_text)
            invoice_date = inv_date_match.group(1) if inv_date_match else None

            # Ship Date
            ship_date_match = re.search(r'Ship Date[:\s]+([\d/]+)', full_text)
            ship_date = ship_date_match.group(1) if ship_date_match else None

            # Delivery Ticket #
            ticket_match = re.search(r'DELIVERY TICKET\s*#?\s*\n.*?([A-Z]?\d{5,})', full_text, re.DOTALL)
            if not ticket_match:
                ticket_match = re.search(r'e-Check\s+([A-Z]?\d{5,})', full_text)
            delivery_ticket = ticket_match.group(1) if ticket_match else None

            # Ship To city
            delivery_location = None
            svc_match = re.search(r'(?:Pool and Spa|Pool & Spa) Service\s*-\s*([A-Za-z\s]+?)(?:\n|$)', full_text)
            if svc_match:
                delivery_location = svc_match.group(1).strip()
            else:
                ship_block_match = re.search(r'SHIP TO.*?(?=F\.O\.B)', full_text, re.DOTALL)
                if ship_block_match:
                    ship_block = ship_block_match.group(0)
                    all_cities = re.findall(r'([A-Za-z][A-Za-z\s]+?)\s+(?:GA|FL|SC)\s+\d{5}', ship_block)
                    if all_cities:
                        delivery_location = all_cities[-1].strip()
            if not delivery_location:
                city_matches = re.findall(r'([A-Za-z][A-Za-z\s]+?)\s+(?:GA|FL|SC)\s+\d{5}', full_text[:full_text.find('F.O.B')] if 'F.O.B' in full_text else full_text)
                if city_matches:
                    delivery_location = city_matches[-1].strip()

            # Line items
            gallons = None
            price_per_gallon = None
            chlorine_total = None
            excise_tax = 0.0
            fuel_surcharge = 0.0

            # Part 6800 (chlorine)
            p6800 = re.search(r'6800\s+([\d,]+\.?\d*)\s+GAL\s+([\d,]+\.?\d*)\s+[\d.]+\s+([\d.]+)\s+[\d.]+\s+([\d,]+\.\d{2})', full_text)
            if p6800:
                gallons = int(float(p6800.group(2).replace(',', '')))
                price_per_gallon = float(p6800.group(3))
                chlorine_total = float(p6800.group(4).replace(',', ''))

            # Part 5501 (excise tax)
            p5501 = re.search(r'5501\s+[\d,]+\.?\d*\s+GAL\s+[\d,]+\.?\d*\s+[\d.]+\s+([\d.]+)\s+[\d.]+\s+([\d,]+\.\d{2})', full_text)
            if p5501:
                excise_tax = float(p5501.group(2).replace(',', ''))

            # Part 91467 (fuel surcharge)
            p91467 = re.search(r'91467\s+[\d,]+\.?\d*\s+EACH\s+[\d,]+\.?\d*\s+[\d.]+\s+([\d.]+)\s+[\d.]+\s+([\d,]+\.\d{2})', full_text)
            if p91467:
                fuel_surcharge = float(p91467.group(2).replace(',', ''))

            # Invoice Total
            total_match = re.search(r'TOTAL\s+([\d,]+\.\d{2})', full_text)
            invoice_total = float(total_match.group(1).replace(',', '')) if total_match else None

            return {
                "invoice_number": invoice_number,
                "invoice_date": invoice_date,
                "ship_date": ship_date,
                "delivery_ticket": delivery_ticket,
                "delivery_location": delivery_location,
                "gallons_delivered": gallons,
                "price_per_gallon": price_per_gallon,
                "chlorine_line_total": chlorine_total,
                "excise_tax": excise_tax,
                "fuel_surcharge": fuel_surcharge,
                "invoice_total": invoice_total,
            }
    finally:
        os.unlink(tmp_path)


def parse_date(date_str):
    """Convert MM/DD/YYYY to YYYY-MM-DD."""
    if not date_str:
        return None
    try:
        parts = date_str.split('/')
        if len(parts) == 3:
            return f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
    except:
        pass
    return date_str


def main(project_name: str = "allied_universal_2025"):
    """Extract data from all pending PDFs and write to extraction_results."""
    conn = get_supabase_conn()
    cur = conn.cursor()

    # Get all pending attachments
    cur.execute("""
        SELECT id, filename, pdf_base64 
        FROM email_extraction.email_attachments 
        WHERE project_name = %s AND extraction_status = 'pending'
        ORDER BY date_sent
    """, (project_name,))
    rows = cur.fetchall()
    print(f"Found {len(rows)} pending attachments")

    extracted = 0
    errors = []

    for row_id, filename, pdf_b64 in rows:
        try:
            pdf_bytes = base64.b64decode(pdf_b64)
            data = extract_invoice_data(pdf_bytes)

            if "error" in data:
                raise Exception(data["error"])

            if not data.get("invoice_number"):
                raise Exception("Could not extract invoice number")

            # Insert extraction result
            cur.execute("""
                INSERT INTO email_extraction.extraction_results 
                (attachment_id, project_name, invoice_number, invoice_date, ship_date,
                 delivery_ticket, delivery_location, gallons_delivered, price_per_gallon,
                 chlorine_line_total, excise_tax, fuel_surcharge, invoice_total, raw_extraction)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (attachment_id) DO NOTHING
            """, (
                row_id, project_name,
                data["invoice_number"],
                parse_date(data.get("invoice_date")),
                parse_date(data.get("ship_date")),
                data.get("delivery_ticket"),
                data.get("delivery_location"),
                data.get("gallons_delivered"),
                data.get("price_per_gallon"),
                data.get("chlorine_line_total"),
                data.get("excise_tax", 0),
                data.get("fuel_surcharge", 0),
                data.get("invoice_total"),
                json.dumps(data)
            ))

            # Update status
            cur.execute("""
                UPDATE email_extraction.email_attachments 
                SET extraction_status = 'extracted' 
                WHERE id = %s
            """, (row_id,))
            conn.commit()
            extracted += 1
            print(f"  OK: {filename} -> {data['invoice_number']} ({data.get('delivery_location')}, {data.get('gallons_delivered')} gal)")

        except Exception as e:
            error_msg = str(e)
            errors.append({"filename": filename, "id": str(row_id), "error": error_msg})
            print(f"  ERROR: {filename} -> {error_msg}")
            cur.execute("""
                UPDATE email_extraction.email_attachments 
                SET extraction_status = 'error', extraction_error = %s 
                WHERE id = %s
            """, (error_msg, row_id))
            conn.commit()

    cur.close()
    conn.close()

    result = {"total_pending": len(rows), "extracted": extracted, "errors": errors}
    print(f"\nComplete: {json.dumps(result, indent=2)}")
    return result
