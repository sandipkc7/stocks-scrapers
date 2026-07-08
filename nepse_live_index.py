from db_config import DB_CONFIG
import os
import asyncio
import httpx
import time
import psycopg2
from psycopg2.extras import execute_values
from datetime import date
import requests
import traceback
from nepse import AsyncNepse

NOTIF_API = 'http://localhost/stocks/public/admin/notifications_api.php'

# Standardized mapping of NEPSE Index IDs to expected database names
INDEX_NAME_MAP = {
    "58": "NEPSE Index",
    "57": "Sensitive Index",
    "62": "Float Index",
    "63": "Sensitive Float Index",
    "51": "Banking SubIndex",
    "55": "Development Bank Index",
    "60": "Finance Index",
    "52": "Hotels And Tourism Index",
    "54": "HydroPower Index",
    "67": "Investment Index",
    "65": "Life Insurance",
    "56": "Manufacturing And Processing",
    "64": "Microfinance Index",
    "66": "Mutual Fund",
    "59": "Non Life Insurance",
    "53": "Others Index",
    "61": "Trading Index"
}

def send_notification(title, message='', type='info', source='nepse_live_index.py'):
    try:
        requests.post(NOTIF_API, data={
            'action': 'create',
            'type': type,
            'title': title,
            'message': message,
            'source': source
        }, timeout=5)
    except Exception:
        pass

def setup_db():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    # Create tables if not exist (without current_value)
    cur.execute('''
        CREATE TABLE IF NOT EXISTS nepse_index (
            id SERIAL PRIMARY KEY,
            date DATE,
            index_name VARCHAR(100),
            points_change NUMERIC,
            percent_change NUMERIC,
            UNIQUE (date, index_name)
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS sub_index (
            id SERIAL PRIMARY KEY,
            date DATE,
            index_name VARCHAR(100),
            points_change NUMERIC,
            percent_change NUMERIC,
            UNIQUE (date, index_name)
        )
    ''')
    
    # Drop current_value if exists, add new columns
    for table in ["nepse_index", "sub_index"]:
        cur.execute(f'''
            ALTER TABLE {table} DROP COLUMN IF EXISTS current_value;
            ALTER TABLE {table}
            ADD COLUMN IF NOT EXISTS open NUMERIC,
            ADD COLUMN IF NOT EXISTS high NUMERIC,
            ADD COLUMN IF NOT EXISTS low NUMERIC,
            ADD COLUMN IF NOT EXISTS closing NUMERIC,
            ADD COLUMN IF NOT EXISTS turnover_values NUMERIC,
            ADD COLUMN IF NOT EXISTS turnover_volume NUMERIC,
            ADD COLUMN IF NOT EXISTS total_transaction NUMERIC;
        ''')
    conn.commit()
    return conn, cur

def safe_float(val):
    if val is None or val == '-':
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val).replace(',', '').replace('%', ''))
    except ValueError:
        return 0.0

async def fetch_save_live_indices():
    send_notification("Live Index Scraper Started", "Scraping today's live index snapshots.", "scraper", "nepse_live_index.py")
    conn, cur = setup_db()
    
    print("Initializing Nepse API client...")
    nepse = AsyncNepse()
    nepse.setTLSVerification(False)
    
    try:
        # Force authentication
        print("Retrieving authorization token...")
        await nepse.getMarketStatus()
        
        print("Fetching today's live index snapshots...")
        main_indices_data = await nepse.getNepseIndex()
        sub_indices_data = await nepse.getNepseSubIndices()
        
        today_str = date.today().isoformat()
        nepse_values = []
        sub_values = []
        
        # Parse main live indices
        for item in main_indices_data:
            idx_id = item.get("id")
            if not idx_id:
                continue
            
            index_name = INDEX_NAME_MAP.get(str(idx_id)) or item.get("index")
            if not index_name:
                continue
            
            closing = safe_float(item.get("currentValue") or item.get("close"))
            points_change = safe_float(item.get("change"))
            percent_change = safe_float(item.get("perChange"))
            open_val = safe_float(item.get("open") or item.get("previousClose"))
            high = safe_float(item.get("high"))
            low = safe_float(item.get("low"))
            turnover = 0.0
            volume = 0.0
            transactions = 0.0
            
            val_tuple = (
                today_str, index_name, points_change, percent_change,
                open_val, high, low, closing, turnover, volume, transactions
            )
            nepse_values.append(val_tuple)
            
        # Parse sub live indices
        for item in sub_indices_data:
            idx_id = item.get("id")
            if not idx_id:
                continue
                
            index_name = INDEX_NAME_MAP.get(str(idx_id)) or item.get("index")
            if not index_name:
                continue
            
            closing = safe_float(item.get("currentValue"))
            points_change = safe_float(item.get("change"))
            percent_change = safe_float(item.get("perChange"))
            open_val = 0.0
            high = 0.0
            low = 0.0
            turnover = 0.0
            volume = 0.0
            transactions = 0.0
            
            val_tuple = (
                today_str, index_name, points_change, percent_change,
                open_val, high, low, closing, turnover, volume, transactions
            )
            sub_values.append(val_tuple)
            
        # Upsert query keeping existing non-zero fields
        insert_query = """
            INSERT INTO {} (
                date, index_name, points_change, percent_change,
                open, high, low, closing, turnover_values, turnover_volume, total_transaction
            ) VALUES %s
            ON CONFLICT (date, index_name) DO UPDATE SET
                points_change = EXCLUDED.points_change,
                percent_change = EXCLUDED.percent_change,
                open = CASE WHEN EXCLUDED.open != 0 THEN EXCLUDED.open ELSE {}.open END,
                high = CASE WHEN EXCLUDED.high != 0 THEN EXCLUDED.high ELSE {}.high END,
                low = CASE WHEN EXCLUDED.low != 0 THEN EXCLUDED.low ELSE {}.low END,
                closing = EXCLUDED.closing
        """
        
        inserted = 0
        if nepse_values:
            execute_values(cur, insert_query.format("nepse_index", "nepse_index", "nepse_index", "nepse_index"), nepse_values)
            inserted += len(nepse_values)
        if sub_values:
            execute_values(cur, insert_query.format("sub_index", "sub_index", "sub_index", "sub_index"), sub_values)
            inserted += len(sub_values)
            
        conn.commit()
        print(f"Successfully saved {inserted} live index values to DB for date: {today_str}.")
        send_notification("Live Index Scraper Completed", f"Successfully scraped and stored {inserted} live index values for today.", "success", "nepse_live_index.py")
        
    except Exception as e:
        err = traceback.format_exc()
        print("An error occurred:")
        print(err)
        send_notification("Live Index Scraper Error", str(e), "error", "nepse_live_index.py")
    finally:
        cur.close()
        conn.close()
        await nepse.client.aclose()

if __name__ == "__main__":
    asyncio.run(fetch_save_live_indices())
