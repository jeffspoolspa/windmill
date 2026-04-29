#requirements:
#wmill
#supabase
#beautifulsoup4

"""
ONE-OFF: Backfill acceptance_link (and additional_contacts) from est_emails HTML
for estimates that lost them during the April 16 RLS regression.

Re-uses the exact parser logic from flow module 'e' (get_estimate_details) so
the values match what the live flow would produce. Only updates fields that
are currently NULL — never overwrites existing values.

Safe to delete after running.
"""

import wmill
import re
from bs4 import BeautifulSoup
from supabase import create_client


def get_acceptance_link(html_content: str) -> str | None:
    """Same logic as flow module e."""
    soup = BeautifulSoup(html_content, 'html.parser')
    link = soup.find('a', href=lambda x: 'woAccept.cfm' in x if x else False)
    if not link:
        return None
    return link.get('href')


def main():
    url = wmill.get_variable("f/SUPABASE/URL")
    key = wmill.get_variable("f/SUPABASE/ANON_KEY")
    supabase = create_client(url, key)

    # Find estimates with NULL acceptance_link
    missing = supabase.table("estimates").select(
        "wo_number"
    ).is_("acceptance_link", "null").execute()

    if not missing.data:
        return {"processed": 0, "skipped": 0, "failed": 0}

    wo_numbers = [row["wo_number"] for row in missing.data]
    print(f"Found {len(wo_numbers)} estimates missing acceptance_link")

    processed = 0
    skipped_no_html = 0
    skipped_no_link_in_html = 0
    failed = 0
    failed_details = []

    for wo_number in wo_numbers:
        # Pull the HTML body for this WO
        email_rows = supabase.table("est_emails").select(
            "wo_number, body_html"
        ).eq("wo_number", wo_number).like(
            "subject", "%Estimate #%"
        ).not_.is_("body_html", "null").limit(1).execute()

        if not email_rows.data:
            skipped_no_html += 1
            continue

        html = email_rows.data[0]["body_html"]

        try:
            link = get_acceptance_link(html)
        except Exception as e:
            print(f"  PARSE FAIL WO #{wo_number}: {e}")
            failed += 1
            failed_details.append({"wo_number": wo_number, "error": f"parse: {e}"})
            continue

        if not link:
            skipped_no_link_in_html += 1
            continue

        try:
            result = supabase.table("estimates").update({
                "acceptance_link": link
            }).eq("wo_number", wo_number).execute()

            if not result.data:
                failed += 1
                failed_details.append({"wo_number": wo_number, "error": "update returned no rows"})
                continue

            print(f"  OK   WO #{wo_number}")
            processed += 1

        except Exception as e:
            print(f"  UPDATE FAIL WO #{wo_number}: {e}")
            failed += 1
            failed_details.append({"wo_number": wo_number, "error": str(e)})

    summary = {
        "processed": processed,
        "skipped_no_html": skipped_no_html,
        "skipped_no_link_in_html": skipped_no_link_in_html,
        "failed": failed,
        "failed_details": failed_details,
        "total_candidates": len(wo_numbers),
    }
    print(f"\nDone: {processed} processed, "
          f"{skipped_no_html} skipped (no HTML), "
          f"{skipped_no_link_in_html} skipped (no link in HTML), "
          f"{failed} failed")
    return summary
