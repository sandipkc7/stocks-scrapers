from db_config import DB_CONFIG
import os
import asyncio
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime, date
import traceback
import random
from nepse import AsyncNepse

def setup_db(conn):
    cur = conn.cursor()
    # Ensure calendar table exists with date as primary key
    cur.execute('''
        CREATE TABLE IF NOT EXISTS calendar (
            date DATE NOT NULL,
            holiday BOOLEAN DEFAULT FALSE,
            "Holiday_Description" VARCHAR(255)
        );
    ''')
    # Add UNIQUE constraint on date if not already present (required for ON CONFLICT)
    cur.execute('''
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = 'calendar'::regclass
                AND contype IN ('p', 'u')
                AND conkey = ARRAY(
                    SELECT attnum FROM pg_attribute
                    WHERE attrelid = 'calendar'::regclass AND attname = 'date'
                )
            ) THEN
                ALTER TABLE calendar ADD CONSTRAINT calendar_date_unique UNIQUE (date);
            END IF;
        END $$;
    ''')
    # Add Holiday_Description column if it doesn't exist
    cur.execute('''
        ALTER TABLE calendar 
        ADD COLUMN IF NOT EXISTS "Holiday_Description" VARCHAR(255);
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
    conn.commit()
    cur.close()


def send_notification(conn, title, message='', type='info', source='nepse_holiday.py'):
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

def parse_holiday(item):
    date_val = item.get('date') or item.get('holidayDate') or item.get('holiday_date')
    desc_val = item.get('description') or item.get('name') or item.get('title') or item.get('holidayName') or item.get('holidayDescription')
    
    if date_val:
        date_val = str(date_val).split('T')[0].split(' ')[0]
    return date_val, desc_val

async def scrape_holidays():
    print("Connecting to database...")
    conn = psycopg2.connect(**DB_CONFIG)
    setup_db(conn)
    
    send_notification(conn, "Holiday Scraper Started", "Starting to scrape holiday data from NEPSE API (Selenium-free).", "scraper")
    
    nepse = AsyncNepse()
    nepse.setTLSVerification(False)
    
    try:
        print("Authenticating with NEPSE...")
        await nepse.getMarketStatus()
        print("Successfully authenticated.")
        
        current_year = datetime.now().year
        years = [current_year - 1, current_year, current_year + 1]
        
        all_holidays = []
        
        for year in years:
            print(f"Fetching holiday list for year {year}...")
            try:
                # Direct API GET request utilizing AsyncNepse's authenticated client
                data = await nepse.requestGETAPI(f'/api/nots/holiday/list?year={year}')
                
                items = []
                if isinstance(data, dict):
                    items = data.get('content') or data.get('data') or data.get('payload') or []
                elif isinstance(data, list):
                    items = data
                    
                year_count = 0
                for item in items:
                    if isinstance(item, dict):
                        h_date, h_desc = parse_holiday(item)
                        if h_date:
                            all_holidays.append((h_date, h_desc))
                            year_count += 1
                print(f"Found {year_count} holiday records for year {year}.")
            except Exception as e:
                print(f"Error fetching holidays for year {year}: {e}")
                
            if year != years[-1]:
                delay = random.uniform(5.0, 10.0)
                print(f"  Sleeping for {delay:.2f} seconds to protect API rate limit...")
                await asyncio.sleep(delay)
                
        if all_holidays:
            print(f"Updating database with {len(all_holidays)} total holidays...")
            cur = conn.cursor()
            
            for h_date, h_desc in all_holidays:
                cur.execute("""
                    INSERT INTO calendar (date, holiday, "Holiday_Description")
                    VALUES (%s, TRUE, %s)
                    ON CONFLICT (date) DO UPDATE 
                    SET holiday = EXCLUDED.holiday,
                        "Holiday_Description" = EXCLUDED."Holiday_Description";
                """, (h_date, h_desc))
                
            conn.commit()
            cur.close()
            
            success_msg = f"Successfully fetched and upserted {len(all_holidays)} holiday records for years {years}."
            send_notification(conn, "Holiday Scraper Success", success_msg, "success")
            print(success_msg)
        else:
            warning_msg = "Scraper finished but no holidays were fetched. NEPSE API might have returned empty data."
            send_notification(conn, "Holiday Scraper Warning", warning_msg, "warning")
            print(warning_msg)
            
    except Exception as e:
        error_msg = f"Fatal error in holiday scraper: {str(e)}"
        print(error_msg)
        traceback.print_exc()
        send_notification(conn, "Holiday Scraper Error", error_msg, "error")
    finally:
        await nepse.client.aclose()
        conn.close()

if __name__ == "__main__":
    asyncio.run(scrape_holidays())
