import psycopg2
import sys
import os
import argparse
from datetime import date
from psycopg2.extras import execute_values

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from db_config import DB_CONFIG

def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)

def get_symbols(conn, symbol=None):
    cur = conn.cursor()
    if symbol:
        return [symbol.upper()]
    cur.execute("SELECT DISTINCT symbol FROM daily_price")
    symbols = [row[0] for row in cur.fetchall()]
    cur.close()
    return symbols

def get_corporate_actions(conn, symbol):
    cur = conn.cursor()
    # Ensure event_type matches what your scraper inserts, e.g., 'Bonus Dividend', 'Right Share'
    cur.execute("""
        SELECT ex_date, event_type, ratio, subscription_price
        FROM corporate_actions
        WHERE symbol = %s AND ex_date IS NOT NULL
        ORDER BY ex_date ASC
    """, (symbol,))
    events = []
    for row in cur.fetchall():
        events.append({
            'ex_date': row[0],
            'type': row[1].lower(),
            'ratio': float(row[2]) if row[2] else 0.0,
            'subscription_price': float(row[3]) if len(row) > 3 and row[3] else 100.0
        })
    cur.close()
    return events

def calculate_adjusted_data_for_symbol(conn, symbol, events):
    cur = conn.cursor()
    cur.execute("""
        SELECT date, open, high, low, close
        FROM daily_price
        WHERE symbol = %s AND close IS NOT NULL
        ORDER BY date ASC
    """, (symbol,))
    
    rows = cur.fetchall()
    
    adjusted_records = []
    
    for row in rows:
        obs_date = row[0]
        unadj_open = float(row[1]) if row[1] else None
        unadj_high = float(row[2]) if row[2] else None
        unadj_low = float(row[3]) if row[3] else None
        unadj_close = float(row[4]) if row[4] else None
        
        # Filter events that occurred AFTER the observation date
        # (Prices on or after the ex-date are already "adjusted" for that event in real life)
        future_events = [e for e in events if e['ex_date'] > obs_date]
        
        A = 1.0
        C = 0.0
        
        for event in future_events:
            # Process chronologically
            ratio = event['ratio']
            if 'bonus' in event['type']:
                # Bonus rate b is decimal form (e.g. 0.20 for 20%)
                b = ratio
                A = A / (1 + b)
                C = C / (1 + b)
            elif 'right' in event['type']:
                # Right share ratio r, e.g., 0.50 for 50% (1:2)
                r = ratio
                S = event['subscription_price']
                A = A / (1 + r)
                C = (C + r * S) / (1 + r)
        
        # Calculate adjusted values
        adj_open = (A * unadj_open + C) if unadj_open is not None else None
        adj_high = (A * unadj_high + C) if unadj_high is not None else None
        adj_low = (A * unadj_low + C) if unadj_low is not None else None
        adj_close = (A * unadj_close + C) if unadj_close is not None else None
        
        adjusted_records.append((obs_date, symbol, adj_open, adj_high, adj_low, adj_close))
        
    cur.close()
    return adjusted_records

def upsert_adjusted_data(conn, records):
    if not records:
        return 0
    cur = conn.cursor()
    sql = """
        INSERT INTO daily_price_math_adj (date, symbol, open, high, low, close)
        VALUES %s
        ON CONFLICT (date, symbol) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close
    """
    execute_values(cur, sql, records)
    conn.commit()
    cur.close()
    return len(records)

def main():
    parser = argparse.ArgumentParser(description="Calculate Adjusted Pre-Open Data")
    parser.add_argument("--symbol", default=None, help="Process a single symbol")
    args = parser.parse_args()

    conn = get_db_connection()
    symbols = get_symbols(conn, args.symbol)
    
    total_processed = 0
    for idx, symbol in enumerate(symbols):
        print(f"[{idx+1}/{len(symbols)}] Processing {symbol}...")
        events = get_corporate_actions(conn, symbol)
        
        records = calculate_adjusted_data_for_symbol(conn, symbol, events)
        if records:
            upserted = upsert_adjusted_data(conn, records)
            total_processed += upserted
            print(f"  Upserted {upserted} adjusted records.")
        else:
            print(f"  No unadjusted records found.")
            
    print(f"\nDone. Processed {total_processed} rows in total.")
    conn.close()

if __name__ == "__main__":
    main()
