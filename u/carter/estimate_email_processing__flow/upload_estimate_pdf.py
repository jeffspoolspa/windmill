import wmill
import re
from weasyprint import HTML
from supabase import create_client

def clean_html_for_pdf(html_content: str):
    """Move logo to top right and clean up HTML"""
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

def main(html_content: str, wo_number: str, subject: str):
    """Convert HTML to PDF, upload to storage, and update database.
    No try/except — let failures propagate so skip_failure on the branch
    makes them visible in Windmill job logs instead of silently returning False.
    """
    url = wmill.get_variable("f/SUPABASE/URL")
    key = wmill.get_variable("f/SUPABASE/ANON_KEY")
    supabase = create_client(url, key)
    
    # Generate PDF
    html = clean_html_for_pdf(html_content)
    pdf_bytes = HTML(string=html).write_pdf()
    
    # Upload to storage (upsert overwrites if exists)
    file_path = f"{wo_number}.pdf"
    supabase.storage.from_("estimates").upload(
        file_path,
        pdf_bytes,
        file_options={"content-type": "application/pdf", "upsert": "true"}
    )
    
    pdf_url = supabase.storage.from_("estimates").get_public_url(file_path)
    
    # Update estimates table with the PDF URL
    result = supabase.table("estimates").update({
        "estimate_pdf_url": pdf_url
    }).eq("wo_number", wo_number).execute()
    
    if not result.data:
        raise ValueError(f"No estimate row found for wo_number {wo_number} — cannot set PDF URL")
    
    # Only mark pdf_processed AFTER we've confirmed the URL is set
    supabase.table("est_emails").update({
        "pdf_processed": True
    }).eq("wo_number", wo_number).execute()
    
    print(f"Processed estimate PDF for WO #{wo_number}: {pdf_url}")
    return {"success": True, "wo_number": wo_number, "pdf_url": pdf_url}