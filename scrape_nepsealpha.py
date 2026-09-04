"""
scrape_nepsealpha.py
====================
Scrapes adjusted OHLC price data from https://nepsealpha.com/nepse-data
for all symbols in the database, then upserts the results into daily_price.

HOW IT WORKS
------------
nepsealpha.com is behind Cloudflare and requires login.
The CSRF token (_token) is only present for authenticated sessions.

Strategy (3 steps):
  1. Open system Chrome via Playwright.
     - Cloudflare is solved automatically (real browser fingerprint).
     - The page auto-POSTs to /nepse-data on load — we intercept that
       request to capture the live _token + all cookies.
     - If not logged in, the browser waits for the user to log in.
     - Browser closes after ~5 seconds (only needed for credentials).

  2. Use the captured _token + cookies in a lightweight requests.Session
     for all subsequent symbol fetches — much faster than a browser.

  3. For each symbol POST to /nepse-data with adjusted price_type.
     Parse JSON, upsert adj_open/adj_high/adj_low/adj_close into daily_price.

Usage
-----
    python scrape_nepsealpha.py --start 2021-08-01 --end 2026-08-01
    python scrape_nepsealpha.py --start 2021-08-01 --end 2026-08-01 --symbol ADBL
    python scrape_nepsealpha.py --start 2021-08-01 --end 2026-08-01 --no-skip
    python scrape_nepsealpha.py --type adjusted --start 2012-01-01 --end 2026-08-14
    python scrape_nepsealpha.py --type both

Prerequisites
-------------
    pip install playwright psycopg2-binary python-dotenv requests
    playwright install chromium
"""

import re
import sys
import json
import time
import random
import datetime
import logging
import argparse
from urllib.parse import parse_qs

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

from db_config import DB_CONFIG

load_dotenv()

BASE_URL         = "https://nepsealpha.com/nepse-data"
USER_AGENT       = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
DELAY_MIN        = 1
DELAY_MAX        = 4
MAX_RETRIES      = 3
RETRY_DELAY      = 8
LOGIN_TIMEOUT_S  = 180   # wait up to 3 minutes for manual login

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step 1: Playwright — capture live _token + cookies from the page auto-POST
# ---------------------------------------------------------------------------

def get_credentials_via_browser():
    """
    Opens Chrome (temporary profile, no conflict with running Chrome),
    navigates to nepsealpha.com/nepse-data and intercepts a POST to /nepse-data
    to capture the live _token + cookies.

    Two paths:
      A) If logged in: the page auto-POSTs on mount → token captured in ~3s
      B) If not logged in: actively fills the form + clicks Search to trigger POST

    Browser closes as soon as token is captured.
    Returns: (csrf_token: str, cookies: dict)
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright not installed. Run: pip install playwright && playwright install chromium")
        sys.exit(1)

    with sync_playwright() as pw:
        # Use system Chrome with a fresh TEMPORARY profile.
        # This avoids the ProcessSingleton conflict (Chrome can be running).
        # Cloudflare trusts Chrome's fingerprint even with a temp profile.
        try:
            browser = pw.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            log.info("Using system Chrome (temp profile, no conflict with running Chrome).")
        except Exception as e:
            log.warning("Chrome not found (%s). Using bundled Chromium.", e)
            browser = pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )

        ctx = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()

        # --- Intercept every POST to /nepse-data to capture _token ---
        captured = {"token": None}

        def on_request(req):
            if req.method == "POST" and "nepse-data" in req.url and "cdn-cgi" not in req.url:
                post_data = req.post_data or ""
                if "_token=" in post_data:
                    parsed = parse_qs(post_data)
                    token = parsed.get("_token", [None])[0]
                    if token and not captured["token"]:
                        captured["token"] = token
                        log.info("Token captured: %s...", token[:12])

        page.on("request", on_request)

        # Navigate
        log.info("Navigating to %s ...", BASE_URL)
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)

        # Wait for Cloudflare
        for _ in range(30):
            if "just a moment" not in page.title().lower():
                log.info("Cloudflare cleared. Title: %r", page.title())
                break
            time.sleep(1)

        # Wait up to 10s for the auto-POST (fires if already logged in)
        for i in range(10):
            if captured["token"]:
                break
            time.sleep(1)

        # Path B: auto-POST didn't fire — actively fill the form and click Search
        if not captured["token"]:
            log.info("No auto-POST detected. Filling the form to trigger one...")
            log.info("(If not logged in, please log in in the Chrome window)")
            try:
                # Wait for Select2 to be ready
                page.wait_for_selector(".select2-container", timeout=10_000)

                # Click the Select2 symbol container
                s2 = page.locator(".select2-container").first
                s2.click()
                page.wait_for_timeout(500)

                # Type in the search box that appears
                search_field = page.locator(".select2-search__field")
                search_field.wait_for(state="visible", timeout=5_000)
                search_field.fill("NEPSE")
                page.wait_for_timeout(800)

                # Click the first option
                opts = page.locator(".select2-results__option")
                opts.first.wait_for(state="visible", timeout=5_000)
                opts.first.click()
                page.wait_for_timeout(300)

                # Click Search button
                log.info("Clicking Search button...")
                page.locator("#searchBtn").click()

                # Wait for the POST to fire
                for i in range(LOGIN_TIMEOUT_S):
                    if captured["token"]:
                        break
                    if i == 20:
                        log.info(
                            "=" * 55 + "\n"
                            "ACTION REQUIRED: Please log in to nepsealpha.com\n"
                            "in the Chrome window. The script will continue\n"
                            "automatically after login.\n"
                            + "=" * 55
                        )
                    if i % 15 == 0 and i > 0:
                        log.info("  Waiting... (%ds)", i)
                    time.sleep(1)

            except Exception as e:
                log.warning("Form-fill fallback error: %s", e)
                # Last resort: wait for user login
                for i in range(LOGIN_TIMEOUT_S):
                    if captured["token"]:
                        break
                    time.sleep(1)

        if not captured["token"]:
            log.error("Could not capture CSRF token after %ds.", LOGIN_TIMEOUT_S)
            browser.close()
            sys.exit(1)

        # Grab cookies
        browser_cookies = ctx.cookies()
        cookie_dict = {c["name"]: c["value"] for c in browser_cookies}
        important = [k for k in cookie_dict if k in ("cf_clearance", "nepsealpha_session", "XSRF-TOKEN")]
        log.info("Cookies captured: %s (+%d others)", important, max(0, len(cookie_dict) - len(important)))

        browser.close()
        log.info("Browser closed. Switching to fast HTTP mode.")
        return captured["token"], cookie_dict



# ---------------------------------------------------------------------------
# Step 2: requests.Session with browser-sourced credentials
# ---------------------------------------------------------------------------

def build_session(cookie_dict):
    """
    Build an HTTP session that impersonates Chrome's TLS fingerprint.
    Uses curl_cffi (preferred) because Cloudflare validates TLS fingerprints.
    Plain requests gets 403 even with valid cf_clearance cookies.
    Falls back to requests if curl_cffi is not installed.
    """
    if HAS_CURL_CFFI:
        log.info("Using curl_cffi (Chrome TLS impersonation).")
        session = cffi_requests.Session(impersonate="chrome")
        for name, value in cookie_dict.items():
            session.cookies.set(name, value, domain="nepsealpha.com")
    else:
        log.warning(
            "curl_cffi not installed — falling back to requests.\n"
            "Install it for better Cloudflare compatibility: pip install curl_cffi"
        )
        session = requests.Session()
        adapter = HTTPAdapter(max_retries=Retry(
            total=3, backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        ))
        session.mount("https://", adapter)
        for name, value in cookie_dict.items():
            session.cookies.set(name, value, domain="nepsealpha.com")
    return session


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)


def fetch_all_symbols():
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("SELECT symbol FROM companies")
    symbols = {row[0] for row in cur.fetchall()}
    cur.execute("SELECT symbol FROM market_indices")
    symbols.update(row[0] for row in cur.fetchall())
    cur.close(); conn.close()
    return sorted(symbols)


def get_already_scraped_symbols(start_date, end_date, p_type="adjusted"):
    conn = get_db_connection()
    cur  = conn.cursor()
    col = "adj_close" if p_type == "adjusted" else "close"
    cur.execute(
        f"SELECT DISTINCT symbol FROM daily_price "
        f"WHERE date >= %s AND date <= %s AND {col} IS NOT NULL",
        (start_date, end_date),
    )
    scraped = {row[0] for row in cur.fetchall()}
    cur.close(); conn.close()
    return scraped


def upsert_to_db(symbol, records, price_type):
    """Upsert adjusted or unadjusted OHLC columns."""
    if not records:
        return 0
        
    conn = get_db_connection()
    cur  = conn.cursor()
    
    if price_type == "adjusted":
        tuples = [
            (r["date"], symbol, r["adj_open"], r["adj_high"], r["adj_low"], r["adj_close"])
            for r in records
        ]
        sql = """
            INSERT INTO daily_price (date, symbol, adj_open, adj_high, adj_low, adj_close)
            VALUES %s
            ON CONFLICT (date, symbol) DO UPDATE SET
                adj_open  = EXCLUDED.adj_open,
                adj_high  = EXCLUDED.adj_high,
                adj_low   = EXCLUDED.adj_low,
                adj_close = EXCLUDED.adj_close
        """
    else:
        tuples = [
            (r["date"], symbol, r["open"], r["high"], r["low"], r["close"], r.get("traded_shares"), r.get("turnover"), r.get("previous_close"))
            for r in records
        ]
        sql = """
            INSERT INTO daily_price (date, symbol, open, high, low, close, traded_shares, turnover, previous_close)
            VALUES %s
            ON CONFLICT (date, symbol) DO UPDATE SET
                open           = EXCLUDED.open,
                high           = EXCLUDED.high,
                low            = EXCLUDED.low,
                close          = EXCLUDED.close,
                traded_shares  = EXCLUDED.traded_shares,
                turnover       = EXCLUDED.turnover,
                previous_close = EXCLUDED.previous_close
        """

    execute_values(cur, sql, tuples)
    conn.commit()
    cur.close(); conn.close()
    return len(tuples)


# ---------------------------------------------------------------------------
# Data fetching & parsing
# ---------------------------------------------------------------------------

def parse_record(raw, price_type):
    """Normalise one API row. Handles both f_date and date key names."""
    try:
        row  = {k.lower(): v for k, v in raw.items()}
        date = row.get("f_date") or row.get("date")
        if not date:
            return None
            
        parsed = {"date": date}
        if price_type == "adjusted":
            parsed["adj_open"]  = float(row["open"])  if row.get("open")  is not None else None
            parsed["adj_high"]  = float(row["high"])  if row.get("high")  is not None else None
            parsed["adj_low"]   = float(row["low"])   if row.get("low")   is not None else None
            parsed["adj_close"] = float(row["close"]) if row.get("close") is not None else None
        else:
            parsed["open"]  = float(row["open"])  if row.get("open")  is not None else None
            parsed["high"]  = float(row["high"])  if row.get("high")  is not None else None
            parsed["low"]   = float(row["low"])   if row.get("low")   is not None else None
            parsed["close"] = float(row["close"]) if row.get("close") is not None else None
            parsed["traded_shares"] = int(row["volume"]) if row.get("volume") is not None else None
            parsed["turnover"] = float(row["turnover"]) if row.get("turnover") is not None else None
            
            # Calculate previous_close if percent_change exists
            pc = row.get("percent_change")
            close_val = parsed["close"]
            if pc is not None and close_val is not None:
                pc = float(pc)
                if pc != -100: # to avoid division by zero
                    parsed["previous_close"] = round(close_val / (1 + (pc / 100.0)), 2)
                else:
                    parsed["previous_close"] = None
            else:
                parsed["previous_close"] = None
                
        return parsed
    except Exception as e:
        log.debug("Parse error %s: %s", raw, e)
        return None


def fetch_symbol(session, token, symbol, start_date, end_date, price_type="adjusted"):
    """POST to /nepse-data for one symbol. Returns (records, status)."""
    headers = {
        "Accept":           "application/json, text/javascript, */*; q=0.01",
        "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin":           "https://nepsealpha.com",
        "Referer":          BASE_URL,
        "User-Agent":       USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
    }
    payload = {
        "symbol":        symbol,
        "specific_date": end_date,
        "start_date":    start_date,
        "end_date":      end_date,
        "filter_type":   "date-range",
        "price_type":    price_type,
        "time_frame":    "daily",
        "_token":        token,
    }
    try:
        resp = session.post(BASE_URL, data=payload, headers=headers, timeout=30)
    except requests.RequestException as e:
        log.warning("  Network error: %s", e)
        return None, "error"

    if resp.status_code in (419, 403):
        log.warning("  Auth error HTTP %d for %s", resp.status_code, symbol)
        return None, "auth_expired"
    if resp.status_code != 200:
        return None, f"http_{resp.status_code}"

    try:
        body = resp.json()
    except ValueError:
        return None, "json_error"

    raw_list = body["data"] if isinstance(body, dict) and "data" in body else (body if isinstance(body, list) else [])
    return ([], "empty") if not raw_list else (raw_list, "ok")


def scrape_symbol(session, token, symbol, start_date, end_date, price_types):
    """Fetch + upsert one symbol for requested price_types. Returns row count."""
    total_upserted = 0
    for p_type in price_types:
        for attempt in range(1, MAX_RETRIES + 1):
            raw_list, status = fetch_symbol(session, token, symbol, start_date, end_date, price_type=p_type)

            if status == "ok":
                parsed = [p for r in raw_list if (p := parse_record(r, p_type)) is not None]
                if not parsed:
                    log.warning("  [%s][%s] Data returned but unparseable.", symbol, p_type)
                    break
                n = upsert_to_db(symbol, parsed, p_type)
                log.info("  [%s][%s] Upserted %d rows.", symbol, p_type, n)
                total_upserted += n
                break

            if status == "empty":
                log.info("  [%s][%s] No data in %s -> %s.", symbol, p_type, start_date, end_date)
                break

            if status == "auth_expired":
                log.error("  [%s][%s] Auth expired (403/419). Re-run to refresh credentials.", symbol, p_type)
                break

            if attempt < MAX_RETRIES:
                log.info("  Retry %s [%s] in %ds...", symbol, p_type, RETRY_DELAY)
                time.sleep(RETRY_DELAY)
            else:
                log.error("  [%s][%s] Failed after %d attempts (%s).", symbol, p_type, MAX_RETRIES, status)
                
    return total_upserted


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="NepseAlpha Adjusted & Unadjusted OHLC Scraper")
    from zoneinfo import ZoneInfo
    today  = datetime.datetime.now(ZoneInfo("Asia/Kathmandu")).strftime("%Y-%m-%d")
    parser.add_argument("--start",   default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--end",     default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--symbol",  default=None, help="Single symbol only")
    parser.add_argument("--no-skip", action="store_true", help="Re-scrape already-done symbols")
    parser.add_argument("--type", choices=["adjusted", "unadjusted", "both"], default="adjusted", help="Price type to scrape")
    args = parser.parse_args()

    print("=" * 60)
    print("   NepseAlpha OHLC Scraper")
    print("=" * 60)

    import sys
    if sys.stdin.isatty():
        start_date = args.start or input(f"Start date (YYYY-MM-DD) [default {today}]: ").strip() or today
        end_date   = args.end   or input(f"End date   (YYYY-MM-DD) [default {today}]: ").strip() or today
    else:
        start_date = args.start or today
        end_date   = args.end   or today
    log.info("Date range: %s -> %s", start_date, end_date)
    
    if args.type == "both":
        price_types = ["unadjusted", "adjusted"]
    else:
        price_types = [args.type]

    # Step 1: Browser captures live token + cookies (fast, ~5 seconds)
    csrf_token, cookie_dict = get_credentials_via_browser()

    # Step 2: Build fast HTTP session
    session = build_session(cookie_dict)

    # Symbols
    if args.symbol:
        symbols = [args.symbol.upper()]
    else:
        symbols = fetch_all_symbols()
        log.info("Found %d symbols.", len(symbols))

    if not args.no_skip and not args.symbol:
        if args.type == "both":
            done_adj = get_already_scraped_symbols(start_date, end_date, "adjusted")
            done_unadj = get_already_scraped_symbols(start_date, end_date, "unadjusted")
            done = done_adj.intersection(done_unadj)
        else:
            done = get_already_scraped_symbols(start_date, end_date, args.type)
        pending = [s for s in symbols if s not in done]
        log.info("Skipping %d already scraped. %d remaining.", len(symbols) - len(pending), len(pending))
        symbols = pending

    if not symbols:
        log.info("Nothing to scrape.")
        return

    # Step 3: Fetch all symbols via HTTP
    total = 0
    for idx, symbol in enumerate(symbols, 1):
        log.info("[%d/%d] %s", idx, len(symbols), symbol)
        total += scrape_symbol(session, csrf_token, symbol, start_date, end_date, price_types)
        if idx < len(symbols):
            delay = random.uniform(DELAY_MIN, DELAY_MAX)
            log.info("  Sleeping %.1fs...", delay)
            time.sleep(delay)

    print("\n" + "=" * 60)
    print(f"   Done. {len(symbols)} symbols, {total} rows upserted.")
    print("=" * 60)


if __name__ == "__main__":
    main()
