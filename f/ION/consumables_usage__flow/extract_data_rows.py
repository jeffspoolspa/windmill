#requirements:
#pandas==2.1.4
#psycopg2-binary==2.9.9
#sqlalchemy==2.0.43

from pandas.core.api import DataFrame
import re
import pandas as pd
from sqlalchemy import create_engine
from urllib.parse import quote_plus

def main(raw_data: map, supabase_connection: dict):
    df = pd.DataFrame(raw_data)
    date_row = df.iloc[1]
    date = re.findall(r'(.+?) -', date_row[0])
    df.columns = df.iloc[4]
    df = df.iloc[5:]
    df["Date"] = date[0]
    print(df['Technician'])
    
    # Connect to Supabase
    encoded_password = quote_plus(supabase_connection['password'])
    connection_string = f"postgresql://{supabase_connection['user']}:{encoded_password}@{supabase_connection['host']}:{supabase_connection['port']}/{supabase_connection['dbname']}?sslmode=require"
    engine = create_engine(connection_string)
    
    # Read only specific columns from items table
    items_df = pd.read_sql('SELECT item_name, zoho_item_id, cost, price FROM items', engine)
    
    # Merge on Item Name
    df = df.merge(
        items_df, 
        left_on='Item Name',
        right_on='item_name',
        how='left'
    )
    
    # Drop the duplicate item_name column from items table (since you already have Item Name)
    df = df.drop(columns=['item_name'])
    
    return {
        "data": df.to_dict('records'),
        "columns": list(df.columns)
    }