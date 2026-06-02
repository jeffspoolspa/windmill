# requirements:
# wmill
# requests
# beautifulsoup4
# psycopg2-binary

"""
f/ION/_discover/_run_backfill

Throwaway no-arg runner so the async MCP runner (runScriptByPath, which passes no
args) can execute a REAL historical visit backfill with the window baked in.
Edit WINDOW + PROBE_ONLY, redeploy, run. Deletable after the backfill is done.
"""

import f.ION._discover.backfill_visits as bf

START_MONTH = "2026-05"
END_MONTH = "2026-05"
PROBE_ONLY = False  # real upsert


def main():
    return bf.main(None, start_month=START_MONTH, end_month=END_MONTH,
                   probe_only=PROBE_ONLY, write_unmapped=False)
