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

def fetch_from_sharesansar_fallback():
    print("Attempting fallback: Scraping daily prices from ShareSansar...")
    import requests
    from bs4 import BeautifulSoup
    
    URL = "https://www.sharesansar.com/today-share-price"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    try:
        response = requests.get(URL, headers=headers, timeout=20)
        response.raise_for_status()
        html = response.text
        
        bs = BeautifulSoup(html, "html.parser")
        try:
            today_text = bs.find("span", {"class": "text-org"}).text.strip()
        except AttributeError:
            today_text = date.today().isoformat()
            
        table = bs.find("table")
        if not table:
            print("No tables found on ShareSansar page.")
            return []
            
        headers_list = [th.text.strip() for th in table.find_all("th")]
        
        try:
            idx_symbol = headers_list.index("Symbol")
            idx_open = headers_list.index("Open")
            idx_high = headers_list.index("High")
            idx_low = headers_list.index("Low")
            idx_close = headers_list.index("Close")
            idx_prev_close = headers_list.index("Prev. Close")
            idx_vol = headers_list.index("Vol")
            idx_turnover = headers_list.index("Turnover")
        except ValueError as e:
            print(f"Could not find required columns in ShareSansar headers: {headers_list}")
            return []
            
        records = []
        tbody = table.find("tbody")
        rows = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]
        
        for row in rows:
            cols = row.find_all("td")
            if len(cols) < len(headers_list):
                continue
                
            symbol = cols[idx_symbol].text.strip()
            if not symbol:
                continue
                
            records.append((
                today_text,
                symbol,
                safe_float(cols[idx_open].text),
                safe_float(cols[idx_high].text),
                safe_float(cols[idx_low].text),
                safe_float(cols[idx_close].text),
                safe_float(cols[idx_prev_close].text),
                safe_float(cols[idx_vol].text),
                safe_float(cols[idx_turnover].text),
            ))
            
        print(f"Fallback retrieved {len(records)} scrip price records from ShareSansar for date {today_text}.")
        return records
    except Exception as fallback_err:
        print(f"Fallback scraping from ShareSansar failed: {fallback_err}")
        traceback.print_exc()
        return []

async def fetch_and_save_daily_share(force=False):
    print("Initializing Nepse API client...")
    nepse = AsyncNepse()
    nepse.setTLSVerification(False)
    
    # 1. Resolve last update date from NEPSE
    status = None
    target_date = date.today().isoformat()
    try:
        # Force authentication and retrieve market status
        print("Retrieving authorization token & market status to check date...")
        status = await nepse.getMarketStatus()
        asOf = status.get('asOf')
        if asOf:
            target_date = asOf.split('T')[0]
    except Exception as e:
        print(f"Failed to retrieve initial market status: {e}")
        
    # 2. Check if daily price has already been successfully scraped for this date
    if not force:
        try:
            conn, cur = setup_db()
            cur.execute("SELECT daily_price FROM calendar WHERE date = %s", (target_date,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row and row[0] is True:
                print(f"Daily price data for {target_date} has already been scraped (daily_price = True in calendar). Skipping execution.")
                await nepse.client.aclose()
                return True
        except Exception as db_err:
            print(f"Database calendar check failed: {db_err}. Proceeding with fetch.")
    else:
        print(f"Bypassing already-scraped check (force = True).")
        
    # 3. Retrieve daily prices
    data = None
    retries = 3
    for attempt in range(1, retries + 1):
        try:
            if not status and attempt > 1:
                status = await nepse.getMarketStatus()
                asOf = status.get('asOf')
                if asOf:
                    target_date = asOf.split('T')[0]
                    
            print(f"Fetching daily price summary for: {target_date} (attempt {attempt}/{retries})...")
            
            # Get daily price history for the target date
            resp_json = await nepse.getPriceVolumeHistory(target_date)
            data = resp_json.get("content", [])
            if data:
                print(f"Retrieved {len(data)} scrip price records from NEPSE API.")
                break
            else:
                print(f"Attempt {attempt} returned empty daily price records from NEPSE API.")
        except Exception as e:
            print(f"Attempt {attempt} failed with NEPSE API error: {e}")
            if attempt < retries:
                await asyncio.sleep(2)
            else:
                print("All NEPSE API attempts failed. Fallback to ShareSansar will be attempted.")
        
    records = []
    unique_dates = set()
    
    if data:
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
    else:
        # Trigger fallback
        fallback_records = fetch_from_sharesansar_fallback()
        if fallback_records:
            records = fallback_records
            for rec in records:
                unique_dates.add(rec[0])
        else:
            print("No daily price records returned by NEPSE API or fallback scraper.")
            return False
            
    try:
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
        print(f"An error occurred during database operations: {e}")
        traceback.print_exc()
        return False
    finally:
        await nepse.client.aclose()

if __name__ == "__main__":
    import sys
    force_pull = "--force" in sys.argv or "--force-pull" in sys.argv
    asyncio.run(fetch_and_save_daily_share(force=force_pull))
