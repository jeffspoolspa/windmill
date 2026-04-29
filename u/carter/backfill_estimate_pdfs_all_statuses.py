#requirements:
#wmill
#supabase
#weasyprint

"""
ONE-OFF: Backfill PDFs for estimates of ANY status (not just active).
The original backfill_estimate_pdfs filters status = 'active', which excluded
the regression-era victims (April 13 - 28) that had since been promoted to
'converted', 'declined', or 'expired'. This script catches those.

Safe to delete after running once. Does NOT modify status or approval_status.
"""

import wmill
import re
from weasyprint import HTML
from supabase import create_client


def clean_html_for_pdf(html_content: str):
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
    return header + html_content


def main():
    url = wmill.get_variable("f/SUPABASE/URL")
    key = wmill.get_variable("f/SUPABASE/ANON_KEY")
    supabase = create_client(url, key)

    # NO status filter — we want converted/declined/expired estimates too
    missing = supabase.table("estimates").select(
        "wo_number, status"
    ).is_("estimate_pdf_url", "null").execute()

    if not missing.data:
        return {"processed": 0, "skipped": 0, "failed": 0}

    wo_numbers = [(row["wo_number"], row["status"]) for row in missing.data]
    print(f"Found {len(wo_numbers)} estimates missing PDF URLs (all statuses)")

    processed = 0
    skipped = 0
    failed = 0
    failed_details = []
    by_status = {}

    for wo_number, status in wo_numbers:
        email_rows = supabase.table("est_emails").select(
            "wo_number, body_html, subject"
        ).eq("wo_number", wo_number).like(
            "subject", "%Estimate #%"
        ).not_.is_("body_html", "null").limit(1).execute()

        if not email_rows.data:
            skipped += 1
            continue

        email = email_rows.data[0]
        html_content = email["body_html"]

        try:
            html = clean_html_for_pdf(html_content)
            pdf_bytes = HTML(string=html).write_pdf()

            file_path = f"{wo_number}.pdf"
            supabase.storage.from_("estimates").upload(
                file_path,
                pdf_bytes,
                file_options={"content-type": "application/pdf", "upsert": "true"}
            )

            pdf_url = supabase.storage.from_("estimates").get_public_url(file_path)

            result = supabase.table("estimates").update({
                "estimate_pdf_url": pdf_url
            }).eq("wo_number", wo_number).execute()

            if not result.data:
                failed += 1
                failed_details.append({"wo_number": wo_number, "error": "estimates update returned no rows"})
                continue

            # Mark email pdf_processed only if it isn't already (don't disturb other state)
            supabase.table("est_emails").update({
                "pdf_processed": True
            }).eq("wo_number", wo_number).execute()

            print(f"  OK   WO #{wo_number} ({status}): {pdf_url}")
            by_status[status] = by_status.get(status, 0) + 1
            processed += 1

        except Exception as e:
            print(f"  FAIL WO #{wo_number} ({status}): {e}")
            failed += 1
            failed_details.append({"wo_number": wo_number, "status": status, "error": str(e)})

    summary = {
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
        "by_status": by_status,
        "failed_details": failed_details,
        "total_candidates": len(wo_numbers),
    }
    print(f"\nDone: {processed} processed, {skipped} skipped, {failed} failed")
    print(f"By status: {by_status}")
    return summary
