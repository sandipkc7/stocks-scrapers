import sys
import os
import requests
import psycopg2
import time
import warnings
from urllib3.exceptions import InsecureRequestWarning
from contextlib import contextmanager

# Add parent directory to path so db_config can be imported if it's there
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    from db_config import DB_CONFIG
except ImportError:
    print("CRITICAL ERROR: db_config.py not found in python directory.")
    sys.exit(1)

warnings.filterwarnings('ignore', category=InsecureRequestWarning)

@contextmanager
def get_db_connection():
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        yield conn
    finally:
        conn.close()

def create_session():
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
    })
    session.get('https://chukul.com/', verify=False)
    return session

def fetch_api_count(session, date, retry_count=0):
    if retry_count > 3:
        print(f"Max retries reached for {date}. Skipping.")
        return None
        
    # First request: get total pages
    url_page_1 = f"https://chukul.com/api/data/v2/floorsheet/bydate/?date={date}&page=1&size=500"
    try:
        response = session.get(url_page_1, verify=False, timeout=15)
        
        if response.status_code == 429:
            sleep_time = 10 * (retry_count + 1)
            print(f"HTTP 429 Rate Limit hit. Backing off for {sleep_time}s...")
            time.sleep(sleep_time)
            return fetch_api_count(session, date, retry_count + 1)
            
        if response.status_code != 200:
            print(f"Error fetching page 1 for {date}: HTTP {response.status_code}")
            return None
        
        data = response.json()
        if not data.get('data'):
            return 0  # No data on this date
            
        last_page = int(data.get('last_page', 1))
        
        # If there's only 1 page, total is just the length of items
        if last_page == 1:
            return len(data.get('data', []))
            
        time.sleep(2) # Prevent hammering the API between page 1 and last page
            
        # Second request: get last page items
        url_last_page = f"https://chukul.com/api/data/v2/floorsheet/bydate/?date={date}&page={last_page}&size=500"
        res_last = session.get(url_last_page, verify=False, timeout=15)
        
        if res_last.status_code == 429:
            sleep_time = 10 * (retry_count + 1)
            print(f"HTTP 429 Rate Limit hit on last page. Backing off for {sleep_time}s...")
            time.sleep(sleep_time)
            return fetch_api_count(session, date, retry_count + 1)
            
        if res_last.status_code != 200:
            print(f"Error fetching last page {last_page} for {date}: HTTP {res_last.status_code}")
            return None
            
        last_data = res_last.json()
        rows_on_last_page = len(last_data.get('data', []))
        
        total_api_rows = ((last_page - 1) * 500) + rows_on_last_page
        return total_api_rows
        
    except Exception as e:
        print(f"Exception fetching API count for {date}: {str(e)}")
        return None

def main():
    print("Starting Floorsheet Verification Script...")
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Select dates >= 2026-01-01 where floorsheet is marked complete, not a holiday, but not yet verified
            query = """
                SELECT date 
                FROM calendar 
                WHERE date >= '2026-01-01' 
                  AND floorsheet_complete = True 
                  AND (holiday IS NULL OR holiday = False)
                  AND (floorsheet_verified IS NULL OR floorsheet_verified = False)
                ORDER BY date ASC
            """
            cur.execute(query)
            dates = [row[0] for row in cur.fetchall()]
            
            if not dates:
                print("No pending dates found for verification.")
                return

            print(f"Found {len(dates)} dates to verify: {', '.join(str(d) for d in dates)}")
            
            session = create_session()
            
            for date_val in dates:
                date_str = date_val.strftime("%Y-%m-%d") if hasattr(date_val, 'strftime') else str(date_val)
                print(f"---")
                print(f"Verifying date: {date_str}")
                
                # Fetch API count
                api_count = fetch_api_count(session, date_str)
                if api_count is None:
                    print(f"Skipping {date_str} due to API error.")
                    time.sleep(2)
                    continue
                
                # Fetch Local DB count
                cur.execute("SELECT COUNT(*) FROM floorsheet WHERE date = %s", (date_str,))
                db_count = cur.fetchone()[0]
                
                difference = abs(api_count - db_count)
                is_verified = (api_count == db_count)
                
                print(f"API Expected: {api_count} | Local DB: {db_count}")
                if is_verified:
                    print(f"[SUCCESS] Counts match! Marking verified.")
                else:
                    print(f"[MISMATCH] Difference of {difference} records.")
                
                # Update calendar table
                update_query = """
                    UPDATE calendar 
                    SET floorsheet_api_transactions = %s,
                        floorsheet_our_transactions = %s,
                        floorsheet_verified = %s
                    WHERE date = %s
                """
                cur.execute(update_query, (api_count, db_count, is_verified, date_str))
                conn.commit()
                
                time.sleep(1) # gentle delay between dates

    print("Verification complete.")

if __name__ == "__main__":
    main()
