import sys
import os
import psycopg2

# Add parent directory to path so db_config can be imported if it's there
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    from db_config import DB_CONFIG
except ImportError:
    print("CRITICAL ERROR: db_config.py not found in python directory.")
    sys.exit(1)

def get_unverified_dates(cur):
    query = """
        SELECT date FROM calendar 
        WHERE floorsheet_summary = TRUE 
        AND (summary_verified IS NULL OR summary_verified = FALSE)
        ORDER BY date ASC
    """
    cur.execute(query)
    return [row[0] for row in cur.fetchall()]

def verify_date(cur, date):
    # Get raw floorsheet quantities
    cur.execute("SELECT COALESCE(SUM(quantity), 0) FROM floorsheet WHERE date = %s", (date,))
    raw_qty = cur.fetchone()[0]

    # Get aggregated broker summary quantities
    # Since every transaction has one buyer, the sum of all broker buy quantities should equal the raw quantity.
    cur.execute("SELECT COALESCE(SUM(total_buy_qty), 0) FROM daily_broker_summary WHERE date = %s", (date,))
    summary_qty = cur.fetchone()[0]

    is_verified = (raw_qty == summary_qty)
    
    # Update calendar table
    cur.execute("""
        UPDATE calendar 
        SET summary_floorsheet_qty = %s,
            summary_broker_qty = %s,
            summary_verified = %s
        WHERE date = %s
    """, (raw_qty, summary_qty, is_verified, date))
    
    return raw_qty, summary_qty, is_verified

def main():
    print("Starting Summary Verification...")
    try:
        with psycopg2.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cur:
                dates = get_unverified_dates(cur)
                if not dates:
                    print("No pending dates found for summary verification.")
                    return
                
                print(f"Found {len(dates)} dates to verify.")
                
                for date in dates:
                    date_str = date.strftime('%Y-%m-%d') if hasattr(date, 'strftime') else str(date)
                    print(f"---")
                    print(f"Verifying summary for date: {date_str}")
                    
                    raw_qty, summary_qty, is_verified = verify_date(cur, date_str)
                    
                    print(f"Raw Floorsheet Qty: {raw_qty} | Aggregated Broker Qty: {summary_qty}")
                    if is_verified:
                        print("[SUCCESS] Quantities perfectly match! Marking verified.")
                    else:
                        print(f"[MISMATCH] Difference of {abs(raw_qty - summary_qty)} units.")
                
                conn.commit()
                print("Verification complete.")
                
    except Exception as e:
        print(f"Database error during verification: {e}")

if __name__ == "__main__":
    main()
