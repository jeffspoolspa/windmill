#requirements:
#pandas==2.1.4
#psycopg2-binary==2.9.9
#sqlalchemy==2.0.43


import pandas as pd
from sqlalchemy import create_engine

def main(result: dict, supabase_connection: dict):

    df = pd.DataFrame(result['data'], columns=result['columns'])
    
    # Create connection string from Windmill resource
    # Supabase uses PostgreSQL under the hood
    connection_string = f"postgresql://{supabase_connection['user']}:{supabase_connection['password']}@{supabase_connection['host']}:{supabase_connection['port']}/{supabase_connection['dbname']}"
    
    engine = create_engine(connection_string)
    
    # Upload dataframe to table
    df.to_sql(
        name='consumables_data',  # Your Supabase table name
        con=engine,
        if_exists='append',  # or 'replace' to overwrite
        index=False,
        method='multi'  # Faster bulk insert
    )
    
    return {"rows_inserted": len(df)}