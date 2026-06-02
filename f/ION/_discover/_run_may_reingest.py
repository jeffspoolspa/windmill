# requirements:
# wmill
# requests
# beautifulsoup4
# psycopg2-binary

"""Throwaway no-arg runner: re-ingest MAY 2026 at the new per-log grain via the
deployed fetch->parse->normalize->upsert pipeline. runScriptByPath passes no args,
so the window + probe_only=False are baked here. Delete after the May validation."""

import f.ION._discover.backfill_visits as bf


def main():
    return bf.main(start_month="2026-05", end_month="2026-05",
                   probe_only=False, write_unmapped=False)
