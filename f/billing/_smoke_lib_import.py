# requirements:
# psycopg2-binary
# wmill

from f.billing._lib.db import get_db_conn

def main():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("select 1")
    r = cur.fetchone()[0]
    cur.close(); conn.close()
    return {"import_ok": True, "select_1": r}
