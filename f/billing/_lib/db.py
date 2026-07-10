# requirements:
# psycopg2-binary
# wmill

"""
f/billing/_lib/db — the one Supabase connection helper.

ADR 009 (tier-1 primitive): one implementation per external operation. This
replaces the get_db_conn() boilerplate copy-pasted into 24 billing /
service_billing scripts, each identical, each pointing at the same resource.
Extracted VERBATIM so behavior is unchanged.

Import as:  from f.billing._lib.db import get_db_conn

Shared across billing AND service_billing (cross-area import works — see
f/billing/_lib/qbo). Port 6543 is the Supabase transaction pooler.
"""

import psycopg2
import wmill

SUPABASE_RESOURCE = "u/carter/supabase"


def get_db_conn():
    sb = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(
        host=sb["host"], port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"), user=sb["user"],
        password=sb["password"], sslmode=sb.get("sslmode", "require"),
    )
