import os
import sys
import time
import random
import logging
import json
import psycopg2
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    HAS_CURL_CFFI = False

from db_config import DB_CONFIG

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
DELAY_MIN = 1.0
DELAY_MAX = 3.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

def get_credentials_via_browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright not installed.")
        sys.exit(1)

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(
                channel="chrome",
                headless=False,
                args=["--disable-blink-features=AutomationControlled"]
            )
        except Exception:
            browser = pw.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            )
            
        ctx = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="en-US"
        )
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()
        
        log.info("Navigating to https://nepsealpha.com/ to capture cookies...")
        page.goto("https://nepsealpha.com/")
        
        for _ in range(30):
            if "just a moment" not in page.title().lower():
                log.info("Cloudflare cleared. Title: %s", page.title())
                break
            time.sleep(1)
            
        time.sleep(3)
        
        browser_cookies = ctx.cookies()
        cookie_dict = {c["name"]: c["value"] for c in browser_cookies}
        important = [k for k in cookie_dict if k in ("cf_clearance", "nepsealpha_session")]
        log.info("Cookies captured: %s", important)
        
        browser.close()
        return cookie_dict

def build_session(cookie_dict):
    if HAS_CURL_CFFI:
        log.info("Using curl_cffi for fast HTTP.")
        session = cffi_requests.Session(impersonate="chrome")
        for name, value in cookie_dict.items():
            session.cookies.set(name, value, domain="nepsealpha.com")
    else:
        log.warning("curl_cffi not installed, using requests.")
        session = requests.Session()
        adapter = HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1))
        session.mount("https://", adapter)
        for name, value in cookie_dict.items():
            session.cookies.set(name, value, domain="nepsealpha.com")
    return session

def get_all_symbols():
    for attempt in range(5):
        try:
            conn = psycopg2.connect(**DB_CONFIG)
            cur = conn.cursor()
            cur.execute("SELECT symbol FROM companies ORDER BY symbol;")
            symbols = [row[0] for row in cur.fetchall()]
            cur.close()
            conn.close()
            return symbols
        except Exception as e:
            log.warning("get_all_symbols DB connect failed (attempt %d): %s", attempt+1, e)
            time.sleep(5)
    return []

def get_latest_quarter_value(quartes_growths, particular_key, offset=0):
    """Filter quartesGrowths array by 'particulars' key and find the most recent one (highest date)."""
    items = [item for item in quartes_growths if item.get("particulars") == particular_key]
    if not items or len(items) <= offset:
        return None
    # Sort by financial_date descending
    items.sort(key=lambda x: x.get("financial_date", ""), reverse=True)
    val = items[offset].get("value")
    
    # Sometimes it comes as string with Cr. (crores) in other places, but usually in quartesGrowths it's a raw float or string
    if isinstance(val, str):
        val_clean = val.replace(",", "").replace("%", "").strip()
        if "Cr." in val_clean:
            val_clean = val_clean.replace("Cr.", "").strip()
            try:
                return float(val_clean) * 10000000
            except:
                return None
        try:
            return float(val_clean)
        except:
            return None
    return val

def fetch_and_parse_symbol(session, symbol):
    url = f"https://nepsealpha.com/stocks/{symbol}/info"
    try:
        resp = session.get(url, timeout=20)
        if resp.status_code != 200:
            log.warning("Status %d for %s", resp.status_code, symbol)
            return None
    except Exception as e:
        log.warning("Error fetching %s: %s", symbol, e)
        return None
        
    soup = BeautifulSoup(resp.text, 'html.parser')
    div = soup.find('div', {'data-page': True})
    
    if not div:
        log.info("No data-page payload found for %s", symbol)
        return None
        
    try:
        parsed = json.loads(div['data-page'])
        props = parsed.get("props", {})
    except json.JSONDecodeError:
        log.warning("Failed to parse JSON for %s", symbol)
        return None
        
    data = {}
    
    # 1. funda_table
    funda = props.get("funda_table", {})
    if funda:
        data['pe_ratio'] = funda.get("pe_ratio")
        data['pb_ratio'] = funda.get("pb_ratio")
        data['roe_ttm'] = funda.get("roe")
        data['roa_ttm'] = funda.get("roa")
        
    # 2. quartesGrowths
    qg = props.get("quartesGrowths", [])
    if qg:
        data['eps_ttm'] = get_latest_quarter_value(qg, "eps_ttm")
        data['net_profit_ttm'] = get_latest_quarter_value(qg, "net_profit_ttm")
        data['net_margin_ttm'] = get_latest_quarter_value(qg, "net_margin_ttm")
        data['asset_turnover_ttm'] = get_latest_quarter_value(qg, "asset_turnover_ttm")
        data['bvps'] = get_latest_quarter_value(qg, "bvps")
        data['net_profit_till_qtr'] = get_latest_quarter_value(qg, "net_profit_till_qtr")
        data['revenue_till_qtr'] = get_latest_quarter_value(qg, "revenue_till_qtr")
        data['revenue_ttm'] = get_latest_quarter_value(qg, "revenue_ttm")
        
        # Extract previous quarter metrics for trend comparison
        data['eps_ttm_prev'] = get_latest_quarter_value(qg, "eps_ttm", offset=1)
        data['roe_ttm_prev'] = get_latest_quarter_value(qg, "roe_ttm", offset=1)
        data['net_profit_till_qtr_prev'] = get_latest_quarter_value(qg, "net_profit_till_qtr", offset=1)
        
        # If funda_table didn't have ROE or ROA, fallback to quartesGrowths
        if data.get('roe_ttm') is None:
            data['roe_ttm'] = get_latest_quarter_value(qg, "roe_ttm")
        if data.get('roa_ttm') is None:
            data['roa_ttm'] = get_latest_quarter_value(qg, "roa_ttm")
            
    # 3. pe_pb_avg (overall average provided by NA)
    pe_pb_avg = props.get("pe_pb_avg", {})
    if pe_pb_avg:
        data['pb_average_available'] = pe_pb_avg.get("pb_avg")
        data['pb_average_5yr'] = pe_pb_avg.get("pb_avg") # NA average is usually long term
        
    # 4. otherQuartGrowths (historical PE/PB)
    oqg = props.get("otherQuartGrowths", [])
    if oqg and not data.get("ps_ratio"):
        # Most recent ps_ratio
        sorted_oqg = sorted(oqg, key=lambda x: x.get("date", ""), reverse=True)
        if sorted_oqg:
            data['ps_ratio'] = sorted_oqg[0].get("ps")
            
        # Calculate our own 2-yr avg just in case
        pb_history = [item.get("pb_ratio") for item in sorted_oqg if item.get("pb_ratio") is not None]
        if pb_history:
            data['pb_average_3yr'] = sum(pb_history) / len(pb_history) # Actually 2yr since it's 8 quarters
            
    # Normalize percentages - NA provides ROE as 0.1135 (11.35%). We leave it as numeric.
    # The DB columns are NUMERIC.
    
    # Calculate fundamental sound status
    score = 0
    pb_avg = data.get('pb_average_5yr') or data.get('pb_average_3yr') or 0
    pb_ratio = data.get('pb_ratio') or 0
    if pb_avg > 0 and pb_ratio > 0 and pb_ratio < pb_avg:
        score += 1

    roe = data.get('roe_ttm')
    roe_prev = data.get('roe_ttm_prev')
    if roe is not None and roe_prev is not None and roe > roe_prev:
        score += 1

    eps = data.get('eps_ttm')
    eps_prev = data.get('eps_ttm_prev')
    if eps is not None and eps_prev is not None and eps > eps_prev:
        score += 1
        
    np = data.get('net_profit_till_qtr')
    np_prev = data.get('net_profit_till_qtr_prev')
    if np is not None and np_prev is not None and np > np_prev:
        score += 1
        
    data['fundamental_sound_status'] = 'Sound' if score >= 3 else 'Weak'
    
    return data

def upsert_fundamentals(conn, symbol, data):
    cur = conn.cursor()
    
    # Fetch old status
    cur.execute("SELECT fundamental_sound_status FROM nepsealpha_fundamentals WHERE symbol = %s", (symbol,))
    old_row = cur.fetchone()
    old_status = old_row[0] if old_row else None
    new_status = data.get('fundamental_sound_status')
    
    # Notify if changed
    if old_status and new_status and old_status != new_status:
        cur.execute("SELECT company_name FROM companies WHERE symbol = %s", (symbol,))
        name_row = cur.fetchone()
        cname = name_row[0] if name_row else symbol
        
        title = "Fundamental Status Changed!"
        msg = f"{cname} ({symbol}) fundamental status has changed from {old_status} to {new_status} based on the latest quarterly update."
        cur.execute("INSERT INTO system_notifications (title, message, type) VALUES (%s, %s, %s)", 
                   (title, msg, "fundamental_change"))
    
    # Filter out None values to let DB defaults apply, or just set NULL
    cols = ['symbol'] + list(data.keys())
    vals = [symbol] + list(data.values())
    
    placeholders = ", ".join(["%s"] * len(cols))
    updates = ", ".join([f"{k} = EXCLUDED.{k}" for k in data.keys()])
    
    sql = f"""
        INSERT INTO nepsealpha_fundamentals ({", ".join(cols)})
        VALUES ({placeholders})
        ON CONFLICT (symbol) DO UPDATE SET
        {updates},
        last_updated = CURRENT_TIMESTAMP;
    """
    
    cur.execute(sql, vals)
    conn.commit()
    cur.close()

def main():
    print("="*50)
    print("Starting NepseAlpha Fundamentals Scraper (JSON Payload parsing)")
    print("="*50)
    
    cookies = get_credentials_via_browser()
    session = build_session(cookies)
    symbols = get_all_symbols()
    
    conn = None
    for attempt in range(5):
        try:
            conn = psycopg2.connect(**DB_CONFIG)
            break
        except Exception as e:
            log.warning("main loop DB connect failed (attempt %d): %s", attempt+1, e)
            time.sleep(5)
            
    if not conn:
        log.error("Could not connect to database after multiple attempts.")
        return
    
    # Ensure schema is updated
    try:
        cur = conn.cursor()
        cur.execute("ALTER TABLE nepsealpha_fundamentals ADD COLUMN IF NOT EXISTS fundamental_sound_status VARCHAR(50);")
        conn.commit()
        cur.close()
    except Exception as e:
        log.warning("Could not alter table: %s", e)
        conn.rollback()
    
    log.info("Found %d equity symbols to scrape.", len(symbols))
    
    success_count = 0
    for idx, symbol in enumerate(symbols, 1):
        log.info("[%d/%d] Scraping %s...", idx, len(symbols), symbol)
        data = fetch_and_parse_symbol(session, symbol)
        
        if data and any(v is not None for v in data.values()):
            try:
                upsert_fundamentals(conn, symbol, data)
                success_count += 1
                log.info("  -> Success")
            except Exception as e:
                log.error("  -> DB Error: %s", e)
                # Try reconnecting once
                try:
                    conn = psycopg2.connect(**DB_CONFIG)
                    upsert_fundamentals(conn, symbol, data)
                    success_count += 1
                    log.info("  -> Success (reconnected)")
                except Exception as reconnect_e:
                    log.error("  -> Reconnect Failed: %s", reconnect_e)
        else:
            log.info("  -> No fundamental data found")
            
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
        
    conn.close()
    print(f"\nCompleted! Successfully scraped {success_count} companies.")

if __name__ == "__main__":
    main()
