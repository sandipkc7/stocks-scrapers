from db_config import DB_CONFIG
import psycopg2
from psycopg2.extras import execute_values
import pandas as pd
import numpy as np
import warnings

# Suppress pandas warnings
warnings.filterwarnings('ignore')



def setup_db():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS computed_indicators (
            id SERIAL PRIMARY KEY,
            date DATE,
            symbol VARCHAR(50),
            sma_5 NUMERIC, sma_10 NUMERIC, sma_20 NUMERIC, sma_50 NUMERIC, sma_100 NUMERIC, sma_200 NUMERIC,
            ema_12 NUMERIC, ema_26 NUMERIC,
            macd_line NUMERIC, macd_signal NUMERIC, macd_histogram NUMERIC,
            rsi_14 NUMERIC,
            bb_upper NUMERIC, bb_middle NUMERIC, bb_lower NUMERIC,
            atr_14 NUMERIC,
            stoch_k NUMERIC, stoch_d NUMERIC,
            volume_sma_20 NUMERIC,
            UNIQUE(date, symbol)
        )
    ''')
    conn.commit()
    return conn, cur

def compute_rsi(data, window=14):
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    
    # Exponential Moving Average using Wilder's method
    avg_gain = gain.ewm(com=window-1, min_periods=window).mean()
    avg_loss = loss.ewm(com=window-1, min_periods=window).mean()
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def compute_stoch(high, low, close, k_window=14, d_window=3):
    lowest_low = low.rolling(window=k_window).min()
    highest_high = high.rolling(window=k_window).max()
    
    k = 100 * ((close - lowest_low) / (highest_high - lowest_low))
    d = k.rolling(window=d_window).mean()
    return k, d

def compute_atr(high, low, close_prev, window=14):
    tr1 = high - low
    tr2 = (high - close_prev).abs()
    tr3 = (low - close_prev).abs()
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    # Wilder's smoothing
    atr = tr.ewm(com=window-1, min_periods=window).mean()
    return atr

def main():
    print("Connecting to DB...")
    conn, cur = setup_db()
    
    # Read all daily_price into Pandas
    print("Fetching daily_price data...")
    query = "SELECT date, symbol, open, high, low, close, previous_close, traded_shares FROM daily_price ORDER BY symbol, date"
    df = pd.read_sql_query(query, conn)
    
    if df.empty:
        print("No daily_price data found.")
        return
        
    print(f"Loaded {len(df)} rows. Processing indicators by symbol...")
    
    records_to_insert = []
    
    # Process each symbol
    grouped = df.groupby('symbol')
    for symbol, group in grouped:
        group = group.sort_values('date').copy()
        
        # We need at least some data to make it worthwhile
        if len(group) < 20:
            continue
            
        close = group['close']
        high = group['high']
        low = group['low']
        volume = group['traded_shares']
        prev_close = group['previous_close']
        
        # SMAs
        sma_5 = close.rolling(window=5).mean()
        sma_10 = close.rolling(window=10).mean()
        sma_20 = close.rolling(window=20).mean()
        sma_50 = close.rolling(window=50).mean()
        sma_100 = close.rolling(window=100).mean()
        sma_200 = close.rolling(window=200).mean()
        
        # EMAs
        ema_12 = close.ewm(span=12, adjust=False).mean()
        ema_26 = close.ewm(span=26, adjust=False).mean()
        
        # MACD
        macd_line = ema_12 - ema_26
        macd_signal = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = macd_line - macd_signal
        
        # RSI
        rsi_14 = compute_rsi(close, 14)
        
        # Bollinger Bands
        bb_middle = sma_20
        bb_std = close.rolling(window=20).std(ddof=0)
        bb_upper = bb_middle + (2 * bb_std)
        bb_lower = bb_middle - (2 * bb_std)
        
        # ATR
        atr_14 = compute_atr(high, low, prev_close, 14)
        
        # Stochastic
        stoch_k, stoch_d = compute_stoch(high, low, close, 14, 3)
        
        # Volume SMA
        volume_sma_20 = volume.rolling(window=20).mean()
        
        # Assign to group
        group['sma_5'] = sma_5
        group['sma_10'] = sma_10
        group['sma_20'] = sma_20
        group['sma_50'] = sma_50
        group['sma_100'] = sma_100
        group['sma_200'] = sma_200
        group['ema_12'] = ema_12
        group['ema_26'] = ema_26
        group['macd_line'] = macd_line
        group['macd_signal'] = macd_signal
        group['macd_hist'] = macd_hist
        group['rsi_14'] = rsi_14
        group['bb_upper'] = bb_upper
        group['bb_middle'] = bb_middle
        group['bb_lower'] = bb_lower
        group['atr_14'] = atr_14
        group['stoch_k'] = stoch_k
        group['stoch_d'] = stoch_d
        group['volume_sma_20'] = volume_sma_20
        
        # Replace NaNs with None for DB insert
        group = group.where(pd.notnull(group), None)
        
        # We only want to insert/upsert the latest X days? Or all history?
        # Let's insert all history to make historical screeners possible.
        for _, row in group.iterrows():
            records_to_insert.append((
                row['date'], row['symbol'],
                row['sma_5'], row['sma_10'], row['sma_20'], row['sma_50'], row['sma_100'], row['sma_200'],
                row['ema_12'], row['ema_26'],
                row['macd_line'], row['macd_signal'], row['macd_hist'],
                row['rsi_14'],
                row['bb_upper'], row['bb_middle'], row['bb_lower'],
                row['atr_14'],
                row['stoch_k'], row['stoch_d'],
                row['volume_sma_20']
            ))

    print(f"Preparing to upsert {len(records_to_insert)} records into computed_indicators...")
    
    insert_query = '''
        INSERT INTO computed_indicators (
            date, symbol, 
            sma_5, sma_10, sma_20, sma_50, sma_100, sma_200, 
            ema_12, ema_26, 
            macd_line, macd_signal, macd_histogram, 
            rsi_14, 
            bb_upper, bb_middle, bb_lower, 
            atr_14, 
            stoch_k, stoch_d, 
            volume_sma_20
        ) VALUES %s
        ON CONFLICT (date, symbol) DO UPDATE SET
            sma_5 = EXCLUDED.sma_5, sma_10 = EXCLUDED.sma_10, sma_20 = EXCLUDED.sma_20, sma_50 = EXCLUDED.sma_50, sma_100 = EXCLUDED.sma_100, sma_200 = EXCLUDED.sma_200,
            ema_12 = EXCLUDED.ema_12, ema_26 = EXCLUDED.ema_26,
            macd_line = EXCLUDED.macd_line, macd_signal = EXCLUDED.macd_signal, macd_histogram = EXCLUDED.macd_histogram,
            rsi_14 = EXCLUDED.rsi_14,
            bb_upper = EXCLUDED.bb_upper, bb_middle = EXCLUDED.bb_middle, bb_lower = EXCLUDED.bb_lower,
            atr_14 = EXCLUDED.atr_14,
            stoch_k = EXCLUDED.stoch_k, stoch_d = EXCLUDED.stoch_d,
            volume_sma_20 = EXCLUDED.volume_sma_20
    '''
    
    # Chunk inserts
    chunk_size = 5000
    for i in range(0, len(records_to_insert), chunk_size):
        chunk = records_to_insert[i:i+chunk_size]
        execute_values(cur, insert_query, chunk)
        conn.commit()
        print(f"Inserted chunk {i//chunk_size + 1}")
        
    print("Done computing and saving indicators!")
    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
