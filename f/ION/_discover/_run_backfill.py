# requirements:
# wmill
# requests
# beautifulsoup4
# psycopg2-binary

"""f/ION/_discover/_run_backfill — throwaway no-arg backfill runner."""

import f.ION._discover.backfill_visits as bf

START_MONTH = "2025-01"
END_MONTH = "2025-06"
PROBE_ONLY = False


def main():
    return bf.main(None, start_month=START_MONTH, end_month=END_MONTH,
                   probe_only=PROBE_ONLY, write_unmapped=False)
