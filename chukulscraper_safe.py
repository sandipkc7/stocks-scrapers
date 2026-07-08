from db_config import DB_CONFIG
import requests
import psycopg2
from datetime import datetime, timedelta
import random
import time
import warnings
from urllib3.exceptions import InsecureRequestWarning
from contextlib import contextmanager
from chukul_floorsheet_verification import fetch_api_count

warnings.filterwarnings('ignore', category=InsecureRequestWarning)



@contextmanager
def get_db_connection():
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        yield conn
    finally:
        conn.close()

def create_session_with_delay():
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
    })
    session.get('https://chukul.com/', verify=False)
    return session

def add_random_delay(min_delay=5, max_delay=10):
    delay = random.uniform(min_delay, max_delay)
    print(f"Sleeping for {delay:.2f} seconds before next request...")
    time.sleep(delay)

def scrape_floorsheet_page(session, date, page=1, size=500, retry_count=0):
    if retry_count > 3:
        print(f"Max retries reached for page {page} on {date}.")
        return [], 0
        
    url = f"https://chukul.com/api/data/v2/floorsheet/bydate/?date={date}&page={page}&size={size}"
    
    try:
        response = session.get(url, verify=False, timeout=15)
        
        if response.status_code == 429:
            sleep_time = 10 * (retry_count + 1)
            print(f"HTTP 429 Rate Limit hit. Backing off for {sleep_time}s...")
            time.sleep(sleep_time)
            return scrape_floorsheet_page(session, date, page, size, retry_count + 1)
            
        if response.status_code == 200:
            data = response.json()
            floorsheet_data = []
            items = data.get('data', [])
            print(f"Scraped {len(items)} records for date {date}, page {page}")
            for item in items:
                floorsheet_data.append({
                    'stock_symbol': str(item.get('symbol', '')),
                    'contract_no': int(item.get('transaction', 0)) if item.get('transaction') else None,
                    'buyer': int(item.get('buyer', 0)) if item.get('buyer') else 0,
                    'seller': int(item.get('seller', 0)) if item.get('seller') else 0,
                    'quantity': int(float(item.get('quantity', 0))),
                    'rate': float(item.get('rate', 0.0)) if item.get('rate') else 0.0,
                    'amount': float(item.get('amount', 0.0)) if item.get('amount') else 0.0,
                    'date': date
                })
            return floorsheet_data, data.get('last_page', 1)
        else:
            print(f"Warning: HTTP {response.status_code} for date {date}")
    except Exception as e:
        print(f"Error scraping floorsheet: {e}")
    
    return [], 0

def update_calendar(conn, date, complete, page):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE calendar SET floorsheet_complete = %s, floorsheet_page = %s WHERE date = %s",
            (complete, page, date)
        )
        conn.commit()

def verify_and_update_calendar(conn, session, date):
    print(f"Starting real-time verification for {date}...")
    api_count = fetch_api_count(session, date)
    if api_count is None:
        print(f"Failed to fetch API count for {date}. Verification skipped.")
        return False
        
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM floorsheet WHERE date = %s", (date,))
        db_count = cur.fetchone()[0]
        
        is_verified = (api_count == db_count)
        
        if is_verified:
            print(f"[SUCCESS] {date} fully verified! API: {api_count} | DB: {db_count}")
        else:
            print(f"[MISMATCH] {date} dropped data! API: {api_count} | DB: {db_count}")
            
        cur.execute("""
            UPDATE calendar 
            SET floorsheet_api_transactions = %s,
                floorsheet_our_transactions = %s,
                floorsheet_verified = %s
            WHERE date = %s
        """, (api_count, db_count, is_verified, date))
        conn.commit()
    return is_verified

def is_market_closed(conn, date):
    dt = datetime.strptime(date, "%Y-%m-%d")
    if dt.weekday() >= 5:
        return True
    with conn.cursor() as cur:
        cur.execute("SELECT holiday FROM calendar WHERE date = %s", (date,))
        row = cur.fetchone()
        if row and row[0]:
            return True
    return False

def scrape_date(session, date, force_rescrap=False):
    print(f"\nProcessing date: {date}{' (FORCE RESCRAP)' if force_rescrap else ''}")
    with get_db_connection() as conn:
        if is_market_closed(conn, date):
            print(f"Market closed on {date}, skipping.")
            return False
            
        with conn.cursor() as cur:
            cur.execute("SELECT floorsheet_complete, floorsheet_page FROM calendar WHERE date = %s", (date,))
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "INSERT INTO calendar (date, holiday, floorsheet_complete, floorsheet_page) VALUES (%s, false, false, 1)",
                    (date,)
                )
                conn.commit()
                complete, page = False, 1
            else:
                complete, page = row[0] if row[0] is not None else False, row[1] if row[1] is not None else 1
                
        if force_rescrap:
            complete = False
            page = 1
            update_calendar(conn, date, False, 1)
            
        if complete:
            print(f"Already completed for {date}, skipping.")
            return True
            
        while True:
            data, total_pages = scrape_floorsheet_page(session, date, page=page, size=500)
            
            if not data or total_pages == 0:
                print(f"No data retrieved for date {date} on page {page}. Retrying this page...")
                add_random_delay(min_delay=5, max_delay=10)
                continue # Skip incrementing the page, just loop again!
                
            store_floorsheet(data)
            print(f"Saved {len(data)} records for date {date} (page {page} of {total_pages})")
                
            if page >= total_pages:
                update_calendar(conn, date, True, 1)
                # Verify exactly as it finishes!
                verify_and_update_calendar(conn, session, date)
                break
            else:
                page += 1
                update_calendar(conn, date, False, page)
                
            add_random_delay(min_delay=2, max_delay=5)
            
    return True

def store_floorsheet(data_list):
    if not data_list:
        return
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    
    cur.execute('SELECT COUNT(*) FROM floorsheet')
    before_count = cur.fetchone()[0]
    
    inserted = 0
    failed = 0
    for d in data_list:
        try:
            cur.execute('''
                INSERT INTO floorsheet (stock_symbol, contract_no, buyer, seller, quantity, rate, amount, date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (contract_no) DO NOTHING
            ''', (d['stock_symbol'], d['contract_no'], d['buyer'], d['seller'], d['quantity'], d['rate'], d['amount'], d['date']))
            if cur.rowcount > 0:
                inserted += 1
        except Exception as e:
            print(f"Insert error for contract {d['contract_no']}: {e}")
            failed += 1
    
    conn.commit()
    
    cur.execute('SELECT COUNT(*) FROM floorsheet')
    after_count = cur.fetchone()[0]
    print(f"Inserted {inserted} new records (total: {after_count}, skipped/failed: {len(data_list) - inserted})")
    cur.close()
    conn.close()

def get_completed_dates():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT date FROM calendar WHERE floorsheet_complete = true")
                return set(str(row[0]) for row in cur.fetchall())
    except Exception as e:
        print(f"Error checking completed dates: {e}")
        return set()

def get_unverified_dates():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT date FROM calendar 
                    WHERE date >= '2026-01-01' 
                      AND floorsheet_complete = True 
                      AND (holiday IS NULL OR holiday = False)
                      AND (floorsheet_verified IS NULL OR floorsheet_verified = False)
                    ORDER BY date ASC
                """)
                return [str(row[0]) for row in cur.fetchall()]
    except Exception as e:
        print(f"Error fetching unverified dates: {e}")
        return []

def ensure_db_constraints():
    print("Verifying database constraints...")
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Check if the unique constraint floorsheet_contract_no exists
                cur.execute("""
                    SELECT 1 FROM pg_constraint 
                    WHERE conrelid = 'floorsheet'::regclass 
                      AND conname = 'floorsheet_contract_no'
                """)
                exists = cur.fetchone()
                
                if not exists:
                    print("Constraint 'floorsheet_contract_no' not found. Checking for duplicates...")
                    
                    # Check if there are duplicates
                    cur.execute("""
                        SELECT COUNT(*) FROM (
                            SELECT contract_no FROM floorsheet GROUP BY contract_no HAVING COUNT(*) > 1
                        ) t
                    """)
                    dup_groups = cur.fetchone()[0]
                    
                    if dup_groups > 0:
                        print(f"Found {dup_groups} groups of duplicate contract numbers. Cleaning duplicates...")
                        
                        # Create temporary index to speed up cleanup
                        cur.execute("CREATE INDEX IF NOT EXISTS temp_floorsheet_contract_no_idx ON floorsheet (contract_no)")
                        conn.commit()
                        
                        # Delete duplicates, keeping the lowest id
                        cur.execute("""
                            DELETE FROM floorsheet a
                            USING floorsheet b
                            WHERE a.contract_no = b.contract_no
                              AND a.id > b.id
                        """)
                        deleted_count = cur.rowcount
                        print(f"Deleted {deleted_count} duplicate records.")
                        
                        # Drop temporary index
                        cur.execute("DROP INDEX IF EXISTS temp_floorsheet_contract_no_idx")
                        conn.commit()
                    else:
                        print("No duplicate contract numbers found.")
                    
                    # Create UNIQUE constraint
                    print("Creating UNIQUE constraint 'floorsheet_contract_no'...")
                    cur.execute("ALTER TABLE floorsheet ADD CONSTRAINT floorsheet_contract_no UNIQUE (contract_no)")
                    conn.commit()
                    print("Constraint 'floorsheet_contract_no' created successfully.")
                else:
                    print("Constraint 'floorsheet_contract_no' verified.")
    except Exception as e:
        print(f"Warning/Error verifying database constraints: {e}")

def main():
    ensure_db_constraints()
    end_date = datetime.now()
    start_date = datetime(2026, 5, 1)
    
    # 1. Fetch unverified dates (Mismatches and Nulls)
    unverified_dates = get_unverified_dates()
    
    # 2. Fetch new dates
    new_dates_to_scrape = []
    completed_dates = get_completed_dates()
    current_date = start_date
    while current_date <= end_date:
        date_str = current_date.strftime('%Y-%m-%d')
        if current_date.weekday() < 5 and date_str not in completed_dates and date_str not in unverified_dates:
            new_dates_to_scrape.append(date_str)
        current_date += timedelta(days=1)
    
    print(f"Found {len(unverified_dates)} unverified dates to rescue.")
    print(f"Found {len(new_dates_to_scrape)} new dates to scrape.")
    
    # Process Unverified Dates First (with force_rescrap)
    for date in unverified_dates:
        session = create_session_with_delay()
        success = scrape_date(session, date, force_rescrap=True)
        add_random_delay(min_delay=3, max_delay=8)
        
    # Process New Dates
    for date in new_dates_to_scrape:
        session = create_session_with_delay()
        success = scrape_date(session, date, force_rescrap=False)
        add_random_delay(min_delay=3, max_delay=8)
        
    print("Scraping completed!")

if __name__ == '__main__':
    main()