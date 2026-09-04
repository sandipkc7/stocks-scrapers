import psycopg2
import psycopg2.extras
from datetime import datetime
import pandas as pd
import numpy as np

def get_db_conn():
    # Read config from ini
    import configparser
    config = configparser.ConfigParser()
    config.read('/var/www/html/stocks/config.ini')
    db_config = config['database']
    return psycopg2.connect(
        dbname=db_config['dbname'],
        user=db_config['user'],
        password=db_config['password'],
        host=db_config['host'],
        port="5432"
    )

def calculate_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def process_symbol(symbol, df):
    # Sort chronologically
    df = df.sort_values('date').reset_index(drop=True)
    if len(df) < 50:
        return None

    # Calculate EMAs
    df['ema9'] = calculate_ema(df['close'], 9)
    df['ema18'] = calculate_ema(df['close'], 18)
    df['avg_val'] = (df['high'] + df['low']) / 2.0

    emaPathPoints = []
    currentRegionType = None
    lastCrossIndex = 0
    lastPushedExtremeIndex = -1

    for i in range(1, len(df)):
        ema9_prev = df.loc[i - 1, 'ema9']
        ema18_prev = df.loc[i - 1, 'ema18']
        ema9_curr = df.loc[i, 'ema9']
        ema18_curr = df.loc[i, 'ema18']
        
        if pd.isna(ema9_prev) or pd.isna(ema18_prev) or pd.isna(ema9_curr) or pd.isna(ema18_curr):
            continue

        isUp = ema9_curr > ema18_curr
        wasUp = ema9_prev > ema18_prev

        if currentRegionType is None:
            currentRegionType = 'up' if isUp else 'down'
            lastCrossIndex = i

        if not wasUp and isUp:  # Upcross
            if currentRegionType == 'down':
                # Process its Ultimate Low
                hasDistinctLineExtreme = abs(df.loc[i, 'avg_val'] - df.loc[lastCrossIndex, 'avg_val']) > 0.5
                use_lows = (i - lastCrossIndex <= 1) and not hasDistinctLineExtreme
                
                bestIndex = -1
                bestVal = float('inf')
                
                for k in range(lastCrossIndex, i + 1):
                    val = df.loc[k, 'low'] if use_lows else df.loc[k, 'avg_val']
                    if not pd.isna(val) and val < bestVal:
                        bestVal = val
                        bestIndex = k
                
                if bestIndex != -1:
                    lookbackStart = max(lastPushedExtremeIndex + 1, bestIndex - 10)
                    for j in range(lookbackStart, bestIndex):
                        val = df.loc[j, 'low'] if use_lows else df.loc[j, 'avg_val']
                        if not pd.isna(val) and val < bestVal:
                            bestVal = val
                            bestIndex = j
                            
                    emaPathPoints.append({
                        'value': bestVal,
                        'type': 'Low',
                        'dataIdx': bestIndex
                    })
                    lastPushedExtremeIndex = bestIndex
                    
            currentRegionType = 'up'
            lastCrossIndex = i

        elif wasUp and not isUp:  # Downcross
            if currentRegionType == 'up':
                # Process its Ultimate High
                hasDistinctLineExtreme = abs(df.loc[i, 'avg_val'] - df.loc[lastCrossIndex, 'avg_val']) > 0.5
                use_highs = (i - lastCrossIndex <= 1) and not hasDistinctLineExtreme
                
                bestIndex = -1
                bestVal = float('-inf')
                
                for k in range(lastCrossIndex, i + 1):
                    val = df.loc[k, 'high'] if use_highs else df.loc[k, 'avg_val']
                    if not pd.isna(val) and val > bestVal:
                        bestVal = val
                        bestIndex = k
                        
                if bestIndex != -1:
                    lookbackStart = max(lastPushedExtremeIndex + 1, bestIndex - 10)
                    for j in range(lookbackStart, bestIndex):
                        val = df.loc[j, 'high'] if use_highs else df.loc[j, 'avg_val']
                        if not pd.isna(val) and val > bestVal:
                            bestVal = val
                            bestIndex = j
                            
                    emaPathPoints.append({
                        'value': bestVal,
                        'type': 'High',
                        'dataIdx': bestIndex
                    })
                    lastPushedExtremeIndex = bestIndex
                    
            currentRegionType = 'down'
            lastCrossIndex = i

    if len(emaPathPoints) < 2:
        return None

    # TB/TS Identification Logic
    currentTrend = 'UP' if emaPathPoints[1]['type'] == 'High' else 'DOWN'
    
    activeTB_idx = 1
    activeTS_idx = 0
    
    for i in range(2, len(emaPathPoints)):
        pt = emaPathPoints[i]
        tbPt = emaPathPoints[activeTB_idx]
        tsPt = emaPathPoints[activeTS_idx]
        
        if currentTrend == 'UP':
            if pt['type'] == 'High' and pt['value'] > tbPt['value']:
                activeTB_idx = i
                activeTS_idx = i - 1
            elif pt['type'] == 'Low' and pt['value'] < tsPt['value']:
                currentTrend = 'DOWN'
                activeTB_idx = i
                activeTS_idx = i - 1
        elif currentTrend == 'DOWN':
            if pt['type'] == 'Low' and pt['value'] < tbPt['value']:
                activeTB_idx = i
                activeTS_idx = i - 1
            elif pt['type'] == 'High' and pt['value'] > tsPt['value']:
                currentTrend = 'UP'
                activeTB_idx = i
                activeTS_idx = i - 1

    return {
        'symbol': symbol,
        'current_trend': currentTrend,
        'tb_value': float(emaPathPoints[activeTB_idx]['value']),
        'ts_value': float(emaPathPoints[activeTS_idx]['value'])
    }

def main():
    print("Starting Trend Computation (TB/TS)...")
    conn = get_db_conn()
    
    # Get all distinct symbols
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT symbol FROM daily_price WHERE adj_close IS NOT NULL")
    symbols = [row[0] for row in cur.fetchall()]
    print(f"Found {len(symbols)} symbols to process.")
    
    # Get existing trends
    cur.execute("SELECT symbol, current_trend FROM company_trends")
    existing_trends = {row[0]: row[1] for row in cur.fetchall()}

    results = []
    notifications = []

    for symbol in symbols:
        try:
            # We fetch up to 300 days for sufficient EMA warmup and path points
            query = f"SELECT date, adj_open as open, adj_high as high, adj_low as low, adj_close as close FROM daily_price WHERE symbol = '{symbol}' AND adj_close IS NOT NULL ORDER BY date DESC"
            df = pd.read_sql(query, conn)
            
            res = process_symbol(symbol, df)
            if res:
                results.append(res)
                
                new_trend = res['current_trend']
                old_trend = existing_trends.get(symbol)
                
                # If trend changed, prepare a notification
                if old_trend and old_trend != new_trend:
                    if old_trend == 'UP' and new_trend == 'DOWN':
                        msg = f"{symbol} trend has shifted from UPTREND to DOWNTREND based on TS break."
                        title = f"Trend Shift: {symbol} (DOWN)"
                        type_ = "warning"
                    elif old_trend == 'DOWN' and new_trend == 'UP':
                        msg = f"{symbol} trend has shifted from DOWNTREND to UPTREND based on TB break."
                        title = f"Trend Shift: {symbol} (UP)"
                        type_ = "success"
                        
                    notifications.append((title, msg, type_))
                    
        except Exception as e:
            print(f"Error processing {symbol}: {e}")
            
    if results:
        insert_query = """
        INSERT INTO company_trends (symbol, current_trend, tb_value, ts_value, updated_at)
        VALUES (%(symbol)s, %(current_trend)s, %(tb_value)s, %(ts_value)s, CURRENT_TIMESTAMP)
        ON CONFLICT (symbol) DO UPDATE SET 
            current_trend = EXCLUDED.current_trend,
            tb_value = EXCLUDED.tb_value,
            ts_value = EXCLUDED.ts_value,
            updated_at = CURRENT_TIMESTAMP
        """
        psycopg2.extras.execute_batch(cur, insert_query, results)
        
        # Insert notifications if any
        if notifications:
            notif_query = """
            INSERT INTO system_notifications (title, message, type, created_at, is_read)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP, FALSE)
            """
            cur.executemany(notif_query, notifications)
            
        conn.commit()
        print(f"Successfully computed and stored trend for {len(results)} symbols.")
        if notifications:
            print(f"Generated {len(notifications)} trend shift notifications.")
    
    cur.close()
    conn.close()
    print("Done!")

if __name__ == "__main__":
    main()
