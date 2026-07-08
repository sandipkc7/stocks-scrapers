from db_config import DB_CONFIG
import os
import asyncio
import httpx
import time
import psycopg2
from psycopg2.extras import execute_values
from datetime import date
import traceback
from nepse import AsyncNepse

def setup_db():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    
    # Ensure daily_price table exists
    cur.execute('''
        CREATE TABLE IF NOT EXISTS daily_price (
            id SERIAL PRIMARY KEY,
            date DATE,
            symbol VARCHAR(50),
            open NUMERIC,
            high NUMERIC,
            low NUMERIC,
            close NUMERIC,
            previous_close NUMERIC,
            traded_shares NUMERIC,
            turnover NUMERIC,
            UNIQUE(date, symbol)
        )
    ''')
    # Ensure calendar table exists
    cur.execute('''
        CREATE TABLE IF NOT EXISTS calendar (
            id SERIAL PRIMARY KEY,
            date DATE UNIQUE,
            holiday BOOLEAN,
            floorsheet_complete BOOLEAN,
            floorsheet_page INTEGER,
            daily_price BOOLEAN DEFAULT FALSE
        )
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

async def fetch_and_save_daily_share():
    print("Initializing Nepse API client...")
    nepse = AsyncNepse()
    nepse.setTLSVerification(False)
    
    try:
        # Force authentication
        print("Retrieving authorization token...")
        await nepse.getMarketStatus()
        
        today_date = date.today().isoformat()
        print(f"Fetching daily price summary for: {today_date}...")
        
        # Get daily price history for today
        resp_json = await nepse.getPriceVolumeHistory(today_date)
        data = resp_json.get("content", [])
        print(f"Retrieved {len(data)} scrip price records.")
        
        if not data:
            print("No daily price records returned by NEPSE API.")
            return False
            
        records = []
        unique_dates = set()
        
        for item in data:
            date_val = item.get('businessDate')
            symbol_val = item.get('symbol')
            if not date_val or not symbol_val:
                continue
                
            open_price = safe_float(item.get('openPrice'))
            high_price = safe_float(item.get('highPrice'))
            low_price = safe_float(item.get('lowPrice'))
            
            close_price = safe_float(item.get('closePrice'))
            if close_price == 0.0:
                close_price = safe_float(item.get('lastUpdatedPrice'))
                
            prev_close = safe_float(item.get('previousDayClosePrice'))
            vol = safe_float(item.get('totalTradedQuantity'))
            turnover = safe_float(item.get('totalTradedValue'))
            
            records.append((
                date_val,
                symbol_val,
                open_price,
                high_price,
                low_price,
                close_price,
                prev_close,
                vol,
                turnover
            ))
            unique_dates.add(date_val)
            
        print(f"Connecting to database to save {len(records)} records...")
        conn, cur = setup_db()
        
        # Insert into daily_price
        insert_query = '''
            INSERT INTO daily_price (date, symbol, open, high, low, close, previous_close, traded_shares, turnover)
            VALUES %s
            ON CONFLICT (date, symbol) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                previous_close = EXCLUDED.previous_close,
                traded_shares = EXCLUDED.traded_shares,
                turnover = EXCLUDED.turnover;
        '''
        execute_values(cur, insert_query, records)
        conn.commit()
        print(f"Successfully saved {len(records)} records to daily_price.")
        
        # Update calendar
        for dt in unique_dates:
            cur.execute('INSERT INTO calendar (date) VALUES (%s) ON CONFLICT DO NOTHING', (dt,))
            cur.execute('UPDATE calendar SET daily_price = TRUE WHERE date = %s', (dt,))
        conn.commit()
        print(f"Updated calendar table for dates: {list(unique_dates)}")
        
        cur.close()
        conn.close()
        return True
        
    except Exception as e:
        print(f"An error occurred: {e}")
        traceback.print_exc()
        return False
    finally:
        await nepse.client.aclose()

if __name__ == "__main__":
    asyncio.run(fetch_and_save_daily_share())
