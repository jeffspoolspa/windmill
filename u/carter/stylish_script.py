import psycopg2
from weasyprint import HTML

def main(db: dict):
    conn = psycopg2.connect(
        host=db["host"],
        port=db["port"],
        database=db["dbname"],
        user=db["user"],
        password=db["password"]
    )
    cur = conn.cursor()
    
    # Get unprocessed emails
    cur.execute("""
        SELECT id, body_html, wo_number 
        FROM est_emails 
        WHERE body_html IS NOT NULL 
          AND (pdf_processed = FALSE OR pdf_processed IS NULL)
    """)
    
    rows = cur.fetchall()
    
    for row_id, html_content, work_order in rows:
        try:
            # Convert HTML to PDF
            pdf_bytes = HTML(string=html_content).write_pdf()
            
            # Store in estimates table
            cur.execute("""
                UPDATE estimates 
                SET estimate_pdf = %s 
                WHERE wo_number = %s
                    AND estimate_pdf IS NULL
            """, (psycopg2.Binary(pdf_bytes), work_order))
            
            # Mark as processed
            cur.execute("""
                UPDATE est_emails 
                SET pdf_processed = TRUE 
                WHERE id = %s
            """, (row_id,))
            
            conn.commit()
            print(f"Processed estimate {work_order}")
            
        except Exception as e:
            print(f"Error processing {work_order}: {e}")
            conn.rollback()
    
    cur.close()
    conn.close()
    
    return {"processed": len(rows)}