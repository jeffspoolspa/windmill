
#extra_requirements:
#psycopg2-binary

import wmill
import psycopg2

SEASON_MAP = {
    12: 'winter', 1: 'winter', 2: 'winter',
    3: 'shoulder', 4: 'shoulder', 5: 'shoulder',
    6: 'summer', 7: 'summer', 8: 'summer',
    9: 'shoulder', 10: 'shoulder', 11: 'shoulder',
}


def get_db_conn():
    supabase = wmill.get_resource("u/carter/supabase")
    return psycopg2.connect(
        host=supabase.get("host"), port=supabase.get("port", 6543),
        dbname=supabase.get("dbname", "postgres"), user=supabase.get("user"),
        password=supabase.get("password"), sslmode=supabase.get("sslmode", "require"),
    )


def main():
    conn = get_db_conn()
    cur = conn.cursor()

    # Step 1: Aggregate chemical + total cost percentiles by calendar month + frequency
    cur.execute("""
        SELECT
            service_frequency,
            EXTRACT(MONTH FROM billing_month)::int AS cal_month,
            COUNT(*) AS sample_size,
            ARRAY_AGG(DISTINCT to_char(billing_month, 'YYYY-MM') ORDER BY to_char(billing_month, 'YYYY-MM')) AS data_months,
            ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY chemical_total)::numeric, 2) AS chem_p25,
            ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY chemical_total)::numeric, 2) AS chem_median,
            ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY chemical_total)::numeric, 2) AS chem_p75,
            ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY invoice_total)::numeric, 2) AS total_p25,
            ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY invoice_total)::numeric, 2) AS total_median,
            ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY invoice_total)::numeric, 2) AS total_p75
        FROM billing_audit.maintenance_invoices
        WHERE service_frequency IN ('weekly', 'biweekly')
          AND chemical_total IS NOT NULL
          AND chemical_total > 0
          AND billing_month >= (CURRENT_DATE - INTERVAL '24 months')
        GROUP BY service_frequency, EXTRACT(MONTH FROM billing_month)
        ORDER BY service_frequency, cal_month
    """)
    rows = cur.fetchall()

    if not rows:
        cur.close()
        conn.close()
        return {"status": "no_data", "message": "No qualifying invoices found"}

    # Step 2: Full refresh — delete existing rows
    cur.execute("DELETE FROM billing_audit.chemical_cost_estimates")
    deleted = cur.rowcount

    # Step 3: Insert computed rows
    inserted = 0
    for row in rows:
        freq, cal_month, sample, months, cp25, cmed, cp75, tp25, tmed, tp75 = row
        season = SEASON_MAP.get(cal_month, 'unknown')

        cur.execute("""
            INSERT INTO billing_audit.chemical_cost_estimates
                (service_frequency, calendar_month, season,
                 chem_p25, chem_median, chem_p75,
                 total_p25, total_median, total_p75,
                 sample_size, data_months_included, computed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """, (freq, cal_month, season, cp25, cmed, cp75, tp25, tmed, tp75, sample, months))
        inserted += 1

    conn.commit()

    # Step 4: Read back summary for verification
    cur.execute("""
        SELECT service_frequency, COUNT(*), 
               ROUND(AVG(chem_median)::numeric, 2) AS avg_chem_median,
               ROUND(MIN(chem_p25)::numeric, 2) AS min_chem,
               ROUND(MAX(chem_p75)::numeric, 2) AS max_chem,
               SUM(sample_size) AS total_invoices
        FROM billing_audit.chemical_cost_estimates
        GROUP BY service_frequency
    """)
    summary = {}
    for row in cur.fetchall():
        summary[row[0]] = {
            "months_covered": row[1],
            "avg_chem_median": float(row[2]),
            "chem_range": f"${float(row[3])} - ${float(row[4])}",
            "total_invoices": row[5],
        }

    cur.close()
    conn.close()

    return {
        "status": "success",
        "deleted_old": deleted,
        "rows_inserted": inserted,
        "frequency_summary": summary,
    }
