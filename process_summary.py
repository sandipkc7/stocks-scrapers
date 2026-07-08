from db_config import DB_CONFIG
import psycopg2
import logging
from typing import List, Tuple

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Database connection configuration


def populate_daily_summary(target_date: str):
    """
    Processes the raw floorsheet data for a given date and populates the daily_broker_summary table.
    """
    logging.info(f"Starting daily summary population for {target_date}")
    
    fetch_query = """
        WITH broker_stats AS (
            SELECT 
                date,
                stock_symbol,
                buyer AS broker_id,
                quantity AS buy_qty,
                amount AS buy_amount,
                0 AS sell_qty,
                0::numeric AS sell_amount,
                CASE WHEN buyer = seller THEN quantity ELSE 0 END AS match_qty,
                CASE WHEN buyer = seller THEN amount ELSE 0 END AS match_amount
            FROM floorsheet
            WHERE date = %s
            
            UNION ALL
            
            SELECT 
                date,
                stock_symbol,
                seller AS broker_id,
                0 AS buy_qty,
                0::numeric AS buy_amount,
                quantity AS sell_qty,
                amount AS sell_amount,
                0 AS match_qty, -- Avoid double counting matching volume
                0::numeric AS match_amount
            FROM floorsheet
            WHERE date = %s
        )
        SELECT 
            date,
            stock_symbol,
            broker_id,
            SUM(buy_qty) AS total_buy_qty,
            SUM(buy_amount) AS total_buy_amount,
            SUM(sell_qty) AS total_sell_qty,
            SUM(sell_amount) AS total_sell_amount,
            SUM(match_qty) AS matching_qty,
            SUM(match_amount) AS matching_amount
        FROM broker_stats
        GROUP BY date, stock_symbol, broker_id
    """

    insert_query = """
        INSERT INTO daily_broker_summary (
            date, stock_symbol, broker_id, 
            total_buy_qty, total_buy_amount, 
            total_sell_qty, total_sell_amount, 
            matching_qty, matching_amount
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
    """
    
    delete_query = "DELETE FROM daily_broker_summary WHERE date = %s"
    
    try:
        with psycopg2.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cur:
                # 1. Fetch aggregated data
                logging.info("Executing aggregation query on floorsheet...")
                cur.execute(fetch_query, (target_date, target_date))
                records = cur.fetchall()
                
                if not records:
                    logging.warning(f"No records found for date {target_date}. Exiting.")
                    return
                    
                logging.info(f"Successfully aggregated {len(records)} summary records.")
                
                # 2. Upsert into daily_broker_summary
                logging.info("Clearing old summary records and inserting new ones...")
                cur.execute(delete_query, (target_date,))
                cur.executemany(insert_query, records)
                
                # 3. Perform real-time verification (Raw Qty vs Aggregated Qty)
                logging.info("Performing autonomous summary verification...")
                cur.execute("SELECT COALESCE(SUM(quantity), 0) FROM floorsheet WHERE date = %s", (target_date,))
                raw_qty = cur.fetchone()[0]
                
                cur.execute("SELECT COALESCE(SUM(total_buy_qty), 0) FROM daily_broker_summary WHERE date = %s", (target_date,))
                summary_qty = cur.fetchone()[0]
                
                is_verified = (raw_qty == summary_qty)
                
                if is_verified:
                    logging.info(f"Verification [SUCCESS] - Raw: {raw_qty} | Agg: {summary_qty}")
                else:
                    logging.warning(f"Verification [MISMATCH] - Raw: {raw_qty} | Agg: {summary_qty}")
                
                # 4. Update calendar table
                logging.info("Updating tracking and verification columns in calendar...")
                cur.execute(
                    """UPDATE calendar 
                       SET floorsheet_summary = TRUE,
                           summary_floorsheet_qty = %s,
                           summary_broker_qty = %s,
                           summary_verified = %s
                       WHERE date = %s""",
                    (raw_qty, summary_qty, is_verified, target_date)
                )
                
                # Commit the transaction
                conn.commit()
                logging.info(f"Successfully populated daily summary and updated calendar for {target_date}.")
                
    except Exception as e:
        logging.error(f"An error occurred while processing data for {target_date}: {e}")

def get_pending_dates() -> List[str]:
    """
    Query the calendar table for dates that have complete floorsheet data
    but haven't been summarized yet.
    """
    try:
        with psycopg2.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cur:
                query = """
                    SELECT date FROM calendar 
                    WHERE floorsheet_verified = TRUE 
                    AND (floorsheet_summary IS NULL OR floorsheet_summary = FALSE OR summary_verified IS NULL OR summary_verified = FALSE)
                    ORDER BY date ASC
                """
                cur.execute(query)
                return [str(row[0]) for row in cur.fetchall()]
    except Exception as e:
        logging.error(f"Failed to fetch pending dates: {e}")
        return []

if __name__ == "__main__":
    logging.info("Starting batch summarization process...")
    pending_dates = get_pending_dates()
    
    if not pending_dates:
        logging.info("No pending dates found to summarize. Exiting.")
    else:
        logging.info(f"Found {len(pending_dates)} pending dates to process.")
        for date_str in pending_dates:
            populate_daily_summary(date_str)
        logging.info("Batch summarization process completed.")
