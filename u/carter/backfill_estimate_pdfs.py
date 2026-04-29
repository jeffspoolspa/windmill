#requirements:
#wmill
#supabase
#weasyprint
#psycopg2-binary

import wmill
import re
from weasyprint import HTML
from supabase import create_client

def clean_html_for_pdf(html_content: str):
    """Move logo to top right and clean up HTML — same logic as step h"""
    logo_match = re.search(r'<img[^>]*src="([^"]*)"[^>]*>', html_content)
    logo_url = logo_match.group(1) if logo_match else ""
    
    header = f'''
    <table width="650" border="0" style="margin-bottom: 20px;">
        <tr>
            <td align="left" valign="top" style="white-space: nowrap;">
                <img src="{logo_url}" width="150px" />
            </td>
            <td align="center" valign="middle">
                <h3><b>WORK ORDER ESTIMATE</b><br /></h3>
            </td>
        </tr>
    </table>
    '''
    
    html_content = re.sub(r'<h3>.*?</h3>', '', html_content, flags=re.DOTALL)
    html_content = re.sub(r'<img[^>]*src="[^"]*"[^>]*>', '', html_content)
    html_content = header + html_content
    
    return html_content

def main():
    url = wmill.get_variable("f/SUPABASE/URL")
    key = wmill.get_variable("f/SUPABASE/ANON_KEY")
    supabase = create_client(url, key)

    # Find all WOs needing PDF backfill:
    # - estimates row exists with no PDF URL and status = active
    # - est_emails row has the HTML body we need
    # Use a raw query approach: get estimates missing PDFs, then fetch their HTML
    missing = supabase.table("estimates").select(
        "wo_number"
    ).is_("estimate_pdf_url", "null").eq("status", "active").execute()

    if not missing.data:
        print("No estimates missing PDF URLs. Nothing to backfill.")
        return {"processed": 0, "skipped": 0, "failed": 0}

    wo_numbers = [row["wo_number"] for row in missing.data]
    print(f"Found {len(wo_numbers)} estimates missing PDF URLs")

    processed = 0
    skipped = 0
    failed = 0
    failed_details = []

    for wo_number in wo_numbers:
        # Get the HTML body from est_emails
        email_rows = supabase.table("est_emails").select(
            "wo_number, body_html, subject"
        ).eq("wo_number", wo_number).like(
            "subject", "%Estimate #%"
        ).not_.is_("body_html", "null").limit(1).execute()

        if not email_rows.data:
            print(f"  SKIP WO #{wo_number}: no HTML body in est_emails")
            skipped += 1
            continue

        email = email_rows.data[0]
        html_content = email["body_html"]

        try:
            # Generate PDF
            html = clean_html_for_pdf(html_content)
            pdf_bytes = HTML(string=html).write_pdf()

            # Upload to storage
            file_path = f"{wo_number}.pdf"
            supabase.storage.from_("estimates").upload(
                file_path,
                pdf_bytes,
                file_options={"content-type": "application/pdf", "upsert": "true"}
            )

            pdf_url = supabase.storage.from_("estimates").get_public_url(file_path)

            # Update estimates table
            result = supabase.table("estimates").update({
                "estimate_pdf_url": pdf_url
            }).eq("wo_number", wo_number).execute()

            if not result.data:
                print(f"  WARN WO #{wo_number}: estimates update returned no rows")
                failed += 1
                failed_details.append({"wo_number": wo_number, "error": "estimates update returned no rows"})
                continue

            # Mark est_emails as processed
            supabase.table("est_emails").update({
                "pdf_processed": True
            }).eq("wo_number", wo_number).execute()

            print(f"  OK   WO #{wo_number}: {pdf_url}")
            processed += 1

        except Exception as e:
            print(f"  FAIL WO #{wo_number}: {e}")
            failed += 1
            failed_details.append({"wo_number": wo_number, "error": str(e)})

    summary = {
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
        "failed_details": failed_details,
        "total_candidates": len(wo_numbers)
    }
    print(f"\nBackfill complete: {processed} processed, {skipped} skipped, {failed} failed out of {len(wo_numbers)} candidates")
    return summary
