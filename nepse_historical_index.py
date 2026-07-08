from db_config import DB_CONFIG
import os
import asyncio
import httpx
import time
import psycopg2
from psycopg2.extras import execute_values
from datetime import date
import traceback
import random
from nepse import AsyncNepse

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

def setup_db(conn):
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
    
    # Ensure system_notifications table exists
    cur.execute('''
        CREATE TABLE IF NOT EXISTS system_notifications (
            id SERIAL PRIMARY KEY,
            type VARCHAR(30) NOT NULL DEFAULT 'info',
            title VARCHAR(255) NOT NULL,
            message TEXT,
            source VARCHAR(100),
            is_read BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
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
    cur.close()

def send_notification(conn, title, message='', type='info', source='nepse_historical_index.py'):
    try:
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO system_notifications (type, title, message, source, is_read)
            VALUES (%s, %s, %s, %s, FALSE)
        ''', (type, title, message, source))
        conn.commit()
        cur.close()
        print(f"Notification Sent: [{type.upper()}] {title} - {message}")
    except Exception as e:
        print(f"Failed to save system notification: {e}")

def safe_float(val):
    if val is None or val == '-':
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val).replace(',', '').replace('%', ''))
    except ValueError:
        return 0.0

async def get_index_options(nepse):
    options = []
    try:
        main_indices = await nepse.getNepseIndex()
        for item in main_indices:
            idx_id = item.get("id")
            if idx_id:
                name = INDEX_NAME_MAP.get(str(idx_id)) or item.get("index")
                if name:
                    options.append((str(idx_id), name.strip()))
                
        sub_indices = await nepse.getNepseSubIndices()
        for item in sub_indices:
            idx_id = item.get("id")
            if idx_id:
                name = INDEX_NAME_MAP.get(str(idx_id)) or item.get("index")
                if name:
                    options.append((str(idx_id), name.strip()))
    except Exception as e:
        print(f"Error fetching dynamic index options: {e}")
        options = [(k, v) for k, v in INDEX_NAME_MAP.items()]
    return options

async def fetch_save_all():
    print("Connecting to database...")
    conn = psycopg2.connect(**DB_CONFIG)
    setup_db(conn)
    
    send_notification(conn, "Historical Index Scraper Started", "Extracting active session token and scraping historical index data.", "scraper", "nepse_historical_index.py")
    
    print("Initializing Nepse API client...")
    nepse = AsyncNepse()
    nepse.setTLSVerification(False)
    
    try:
        # Force authentication and retrieve active token
        print("Retrieving authorization token...")
        await nepse.getMarketStatus()
        token = await nepse.token_manager.getAccessToken()
        print("Token retrieved successfully.")
        
        # Get dynamic index list for historical backfill
        options = await get_index_options(nepse)
        print(f"Found {len(options)} indices to scrape for history.")
        
        main_indices = ["NEPSE Index", "Sensitive Index", "Float Index", "Sensitive Float Index"]
        api_headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Referer": "https://www.nepalstock.com/indices",
            "Authorization": f"Salter {token}"
        }
        
        total_saved = 0
        
        # Fetch index history sequentially with a randomized delay to prevent rate-limiting/IP flagging
        async with httpx.AsyncClient(verify=False, timeout=20.0) as client:
            for idx, (val, index_name) in enumerate(options):
                url = f"https://www.nepalstock.com/api/nots/index/history/{val}?&size=500"
                print(f"[{idx+1}/{len(options)}] Fetching history for {index_name} (ID: {val})...")
                
                try:
                    r = await client.get(url, headers=api_headers)
                    if r.status_code == 200:
                        resp_json = r.json()
                        data = resp_json.get("content", [])
                        print(f"  Retrieved {len(data)} history records.")
                        
                        nepse_values = []
                        sub_values = []
                        
                        for item in data:
                            date_val = item.get('businessDate')
                            open_val = safe_float(item.get('openIndex'))
                            high = safe_float(item.get('highIndex'))
                            low = safe_float(item.get('lowIndex'))
                            closing = safe_float(item.get('closingIndex'))
                            abs_change = safe_float(item.get('absChange'))
                            pct_change = safe_float(item.get('percentageChange'))
                            turnover_val = safe_float(item.get('turnoverValue'))
                            turnover_vol = safe_float(item.get('turnoverVolume'))
                            total_trans = safe_float(item.get('totalTransaction'))
                            
                            val_tuple = (
                                date_val, index_name, abs_change, pct_change,
                                open_val, high, low, closing, turnover_val, turnover_vol, total_trans
                            )
                            
                            if index_name in main_indices:
                                nepse_values.append(val_tuple)
                            else:
                                sub_values.append(val_tuple)
                                
                        # Upsert query for history
                        insert_query = """
                            INSERT INTO {} (
                                date, index_name, points_change, percent_change,
                                open, high, low, closing, turnover_values, turnover_volume, total_transaction
                            ) VALUES %s
                            ON CONFLICT (date, index_name) DO UPDATE SET
                                points_change = EXCLUDED.points_change,
                                percent_change = EXCLUDED.percent_change,
                                open = EXCLUDED.open,
                                high = EXCLUDED.high,
                                low = EXCLUDED.low,
                                closing = EXCLUDED.closing,
                                turnover_values = EXCLUDED.turnover_values,
                                turnover_volume = EXCLUDED.turnover_volume,
                                total_transaction = EXCLUDED.total_transaction
                        """
                        
                        inserted_count = 0
                        cur = conn.cursor()
                        if nepse_values:
                            execute_values(cur, insert_query.format("nepse_index"), nepse_values)
                            inserted_count += len(nepse_values)
                        if sub_values:
                            execute_values(cur, insert_query.format("sub_index"), sub_values)
                            inserted_count += len(sub_values)
                            
                        conn.commit()
                        cur.close()
                        print(f"  Saved {inserted_count} records to DB.")
                        total_saved += inserted_count
                    else:
                        print(f"  Failed to fetch history for {index_name}: HTTP {r.status_code}")
                except Exception as e:
                    print(f"  Error fetching history for {index_name}: {e}")
                
                # Introduce a randomized delay of 5 to 10 seconds between requests (except for the last one)
                if idx < len(options) - 1:
                    delay = random.uniform(5.0, 10.0)
                    print(f"  Sleeping for {delay:.2f} seconds to protect API rate limit...")
                    await asyncio.sleep(delay)
                    
        send_notification(conn, "Historical Index Scraper Completed", f"Successfully scraped and stored {total_saved} historical data points for {len(options)} indices.", "success", "nepse_historical_index.py")
        
    except Exception as e:
        err = traceback.format_exc()
        print("An error occurred:")
        print(err)
        send_notification(conn, "Historical Index Scraper Error", str(e), "error", "nepse_historical_index.py")
    finally:
        conn.close()
        await nepse.client.aclose()

if __name__ == "__main__":
    asyncio.run(fetch_save_all())
