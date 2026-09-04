import csv
import psycopg2
from psycopg2.extras import execute_values
from db_config import DB_CONFIG
import sys
import os
import shutil
import glob

def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)

def safe_float(val):
    if not val:
        return None
    try:
        # Remove commas and percentage signs
        clean_val = str(val).replace(',', '').replace('%', '').strip()
        return float(clean_val)
    except ValueError:
        return None

def import_csv(filepath):
    if not os.path.exists(filepath):
        print(f"Error: File '{filepath}' not found.")
        return False

    print(f"\nReading data from {filepath}...")
    
    records = []
    
    with open(filepath, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            symbol = row.get('Symbol', '').strip()
            date = row.get('Date', '').strip()
            
            if not symbol or not date:
                continue
                
            adj_open = safe_float(row.get('Open'))
            adj_high = safe_float(row.get('High'))
            adj_low = safe_float(row.get('Low'))
            adj_close = safe_float(row.get('Close'))
            
            records.append((
                date, symbol,
                adj_open, adj_high, adj_low, adj_close
            ))
            
    if not records:
        print(" -> No valid records found in the CSV.")
        return False
        
    print(f" -> Found {len(records)} records. Upserting to database...")
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    # We only update the adjusted columns. 
    # Unadjusted columns (open, high, low, close, volume) remain untouched!
    sql = """
        INSERT INTO daily_price (
            date, symbol, 
            adj_open, adj_high, adj_low, adj_close
        ) VALUES %s
        ON CONFLICT (date, symbol) DO UPDATE SET
            adj_open = EXCLUDED.adj_open,
            adj_high = EXCLUDED.adj_high,
            adj_low = EXCLUDED.adj_low,
            adj_close = EXCLUDED.adj_close
    """
    
    execute_values(cur, sql, records)
    conn.commit()
    cur.close()
    conn.close()
    
    print(" -> Database updated successfully!")
    return True

def process_data_directory():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, 'data')
    completed_dir = os.path.join(data_dir, 'completed')
    
    # Ensure directories exist
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(completed_dir, exist_ok=True)
    
    # Find all CSV files in the data directory
    csv_files = glob.glob(os.path.join(data_dir, '*.csv'))
    
    if not csv_files:
        print(f"No CSV files found in '{data_dir}'.")
        print(f"Please place your downloaded NepseAlpha CSVs in the 'data' folder and run this script again.")
        return
        
    print(f"Found {len(csv_files)} CSV file(s) in '{data_dir}'. Starting batch process...\n")
    
    success_count = 0
    for filepath in csv_files:
        filename = os.path.basename(filepath)
        
        if "adjusted" not in filename.lower():
            print(f" -> Skipping '{filename}' (Filename must contain 'adjusted' to prevent wrong data import).")
            continue
            
        try:
            success = import_csv(filepath)
            
            if success:
                # Move to completed folder
                dest_path = os.path.join(completed_dir, filename)
                # If file already exists in completed, overwrite it
                if os.path.exists(dest_path):
                    os.remove(dest_path)
                    
                shutil.move(filepath, dest_path)
                print(f" -> Moved '{filename}' to completed folder.")
                success_count += 1
            else:
                print(f" -> Failed to process '{filename}'. Left in data folder.")
                
        except Exception as e:
            print(f" -> Error processing '{filename}': {e}")
            
    print(f"\nBatch process complete! Successfully imported {success_count} out of {len(csv_files)} files.")

if __name__ == '__main__':
    print("=== NepseAlpha CSV Batch Importer ===")
    process_data_directory()
