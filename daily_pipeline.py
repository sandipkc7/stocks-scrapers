"""
Daily Pipeline Orchestrator for NEPSE Data Scraping
====================================================
Runs all daily scrapers in order:
  1. nepse_holiday.py     - refresh holiday calendar
  2. (holiday check)      - if today is a holiday/Saturday, stop
  3. nepse_companies.py   - update company list
  4. chukulscraper_safe.py - fetch floorsheet data
  5. nepse_daily_share.py  - fetch daily share prices
  6. nepse_live_index.py   - fetch live/current index
  7. process_summary.py   - aggregate broker summaries
  8. compute_indicators.py - calculate technical indicators

Scheduling:
  - Linux (Production): runs every day at 3:30 PM NPT via cron
  - Windows (Local): runs via Task Scheduler at 3:30 PM
  - Can also be triggered from Admin Panel (scraper_control.php)
"""
import sys
import os
import subprocess
import psycopg2
from datetime import date, datetime, timedelta
import traceback

# Force UTF-8 output on Windows to avoid cp1252 UnicodeEncodeError
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Resolve paths relative to this script's directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, 'current_scrape.log')

# Pipeline scripts to run after holiday check (in order)
PIPELINE_SCRIPTS = [
    'nepse_companies.py',
    'chukulscraper_safe.py',
    'nepse_daily_share.py',
    'calculate_adjusted_preopen.py',
    'nepse_live_index.py',
    'process_summary.py',
    'scrape_nepsealpha.py',
    'scrape_nepsealpha_bydate.py',
    'compute_indicators.py',
    'scrape_nepsealpha_fundamentals.py',
    'compute_trend_tb_ts.py',
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_python_bin():
    """Resolve the correct Python binary (.venv or system fallback)."""
    is_windows = sys.platform.startswith('win')
    venv_python = os.path.join(
        SCRIPT_DIR, '.venv',
        'Scripts' if is_windows else 'bin',
        'python.exe' if is_windows else 'python'
    )
    if os.path.exists(venv_python):
        return venv_python
    return 'python' if is_windows else 'python3'

def get_db_conn():
    """Open a PostgreSQL connection using db_config.py."""
    sys.path.insert(0, SCRIPT_DIR)
    from db_config import DB_CONFIG
    return psycopg2.connect(**DB_CONFIG)

def log(msg):
    """Print with timestamp, flushed immediately (captured by shell redirection)."""
    timestamp = datetime.now().strftime('%H:%M:%S')
    print(f"[{timestamp}] {msg}", flush=True)

# ---------------------------------------------------------------------------
# Database tracking helpers
# ---------------------------------------------------------------------------

def ensure_pipeline_columns(conn):
    """
    Add pipeline tracking columns to the calendar table if they don't exist.
    This is safe to run every time (IF NOT EXISTS).
    """
    cur = conn.cursor()
    cur.execute("""
        ALTER TABLE calendar
            ADD COLUMN IF NOT EXISTS pipeline_run BOOLEAN DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS pipeline_status VARCHAR(20) DEFAULT NULL,
            ADD COLUMN IF NOT EXISTS pipeline_ran_at TIMESTAMP DEFAULT NULL,
            ADD COLUMN IF NOT EXISTS pipeline_failed_steps TEXT DEFAULT NULL;
    """)
    conn.commit()
    cur.close()

def record_pipeline_status(conn, run_date, status, failed_steps=None):
    """
    Upsert today's pipeline run status into the calendar table.
    status: 'holiday' | 'success' | 'partial' | 'failed'
    """
    try:
        cur = conn.cursor()
        # Ensure the date row exists in calendar first
        cur.execute("""
            INSERT INTO calendar (date) VALUES (%s)
            ON CONFLICT (date) DO NOTHING;
        """, (run_date,))
        failed_str = ', '.join(failed_steps) if failed_steps else None
        cur.execute("""
            UPDATE calendar
            SET pipeline_run = TRUE,
                pipeline_status = %s,
                pipeline_ran_at = CURRENT_TIMESTAMP,
                pipeline_failed_steps = %s
            WHERE date = %s;
        """, (status, failed_str, run_date))
        conn.commit()
        cur.close()
    except Exception as e:
        log(f"[WARN] Could not update calendar pipeline status: {e}")

def send_notification(conn, title, message='', notif_type='info', source='daily_pipeline.py'):
    """Insert a row into system_notifications following the existing protocol."""
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS system_notifications (
                id SERIAL PRIMARY KEY,
                type VARCHAR(30) NOT NULL DEFAULT 'info',
                title VARCHAR(255) NOT NULL,
                message TEXT,
                source VARCHAR(100),
                is_read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            INSERT INTO system_notifications (type, title, message, source, is_read)
            VALUES (%s, %s, %s, %s, FALSE)
        """, (notif_type, title, message, source))
        conn.commit()
        cur.close()
        log(f"Notification Sent: [{notif_type.upper()}] {title}")
    except Exception as e:
        log(f"[WARN] Could not save notification: {e}")

# ---------------------------------------------------------------------------
# Holiday Detection
# ---------------------------------------------------------------------------

def is_today_non_trading(conn):
    """
    Returns (True, reason) if today is a non-trading day, else (False, '').

    Logic (in order):
    1. Saturday in Nepal (weekday 5 in Python) is always non-trading.
    2. Check if today is marked holiday=TRUE in the calendar table.
    3. Fallback: if no calendar row exists for today at all, allow trading
       (the holiday scraper just finished; absence of a holiday row means trading day).
    """
    today = date.today()

    # 1. Saturday is always non-trading in Nepal
    if today.weekday() == 5:
        return True, "Saturday is a non-trading day in Nepal."

    # 2. DB holiday check
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT holiday, \"Holiday_Description\" FROM calendar WHERE date = %s",
            (today,)
        )
        row = cur.fetchone()
        cur.close()
        if row and row[0] is True:
            desc = row[1] or 'Public Holiday'
            return True, f"Today is marked as a holiday in the calendar: {desc}"
    except Exception as e:
        log(f"[WARN] Could not query calendar for holiday status: {e}. Assuming trading day.")

    return False, ''

def get_holiday_api_last_update(conn):
    """
    Returns the most recent date for which holiday data exists in the calendar,
    so we can warn if holiday data is stale (older than 30 days).
    """
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT MAX(date) FROM calendar
            WHERE holiday = TRUE AND date >= CURRENT_DATE - INTERVAL '365 days'
        """)
        row = cur.fetchone()
        cur.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return None

# ---------------------------------------------------------------------------
# Script Execution
# ---------------------------------------------------------------------------

def run_script(python_bin, script_name, args=None):
    """
    Launch a script as a subprocess, streaming its stdout/stderr live.
    Returns True on exit code 0, False otherwise.
    """
    script_path = os.path.join(SCRIPT_DIR, script_name)
    log("")
    log("=" * 60)
    log(f"RUNNING: {script_name}")
    log("=" * 60)

    cmd = [python_bin, script_path]
    if args:
        cmd.extend(args)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=SCRIPT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1
        )
        for line in proc.stdout:
            print(line, end='', flush=True)
        proc.wait()

        if proc.returncode == 0:
            log(f"[OK] {script_name} completed successfully.")
            return True
        else:
            log(f"[FAIL] {script_name} exited with code {proc.returncode}.")
            return False

    except Exception as e:
        log(f"[FAIL] Failed to launch {script_name}: {e}")
        traceback.print_exc()
        return False

# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------

def main():
    today = date.today()
    log("=" * 60)
    log("NEPSE DAILY PIPELINE STARTED")
    log(f"Date: {today.strftime('%A, %d %B %Y')}")
    log("=" * 60)

    python_bin = get_python_bin()
    log(f"Python binary: {python_bin}")

    # ------------------------------------------------------------------
    # STEP 1: Refresh Holiday Calendar
    # ------------------------------------------------------------------
    log("")
    log("STEP 1/8: Refreshing holiday calendar (nepse_holiday.py)...")
    holiday_refresh_ok = run_script(python_bin, 'nepse_holiday.py')
    if not holiday_refresh_ok:
        log("[WARN] Holiday scraper failed — will rely on existing DB calendar data.")

    # ------------------------------------------------------------------
    # STEP 2: Connect to DB and verify holiday data freshness
    # ------------------------------------------------------------------
    log("")
    log("STEP 2/8: Connecting to database and checking trading day status...")
    try:
        conn = get_db_conn()
    except Exception as e:
        log(f"[FATAL] Cannot connect to database: {e}")
        sys.exit(1)

    ensure_pipeline_columns(conn)

    # Warn if holiday API data seems stale (last holiday > 30 days ago)
    last_holiday_date = get_holiday_api_last_update(conn)
    if last_holiday_date:
        days_since = (today - last_holiday_date).days
        if days_since > 30:
            log(f"[WARN] Holiday data may be stale — last holiday recorded was {last_holiday_date} ({days_since} days ago).")
        else:
            log(f"[INFO] Holiday data is current. Most recent holiday on record: {last_holiday_date}.")
    else:
        log("[WARN] No holiday records found in calendar. Proceeding with caution.")

    # ------------------------------------------------------------------
    # STEP 3: Holiday / Non-Trading Day Check
    # ------------------------------------------------------------------
    is_holiday, holiday_reason = is_today_non_trading(conn)

    if is_holiday:
        msg = f"Today ({today}) is a non-trading day. Reason: {holiday_reason} Pipeline will not run data scrapers."
        log(msg)
        send_notification(conn, "Daily Pipeline — Non-Trading Day", msg, "info")
        record_pipeline_status(conn, today, 'holiday')
        conn.close()
        log("")
        log("PIPELINE COMPLETE: Stopped — non-trading day.")
        sys.exit(0)

    log(f"[OK] Today ({today}) is a trading day. Proceeding with data pipeline.")
    send_notification(
        conn,
        "Daily Pipeline Started",
        f"Trading day confirmed for {today}. Running {len(PIPELINE_SCRIPTS)} scraper steps.",
        "scraper"
    )
    conn.close()

    # ------------------------------------------------------------------
    # STEPS 4–9: Run all scraper scripts in order
    # ------------------------------------------------------------------
    results = {}
    for script_name in PIPELINE_SCRIPTS:
        args = []
        if script_name == 'nepse_daily_share.py':
            args = ['--force']
        success = run_script(python_bin, script_name, args=args)
        results[script_name] = success

        # Record per-step outcome to DB immediately after each script
        try:
            conn = get_db_conn()
            step_status = 'success' if success else 'error'
            step_title = f"Pipeline Step {'OK' if success else 'FAILED'}: {script_name}"
            step_msg = (
                f"{script_name} completed successfully for {today}."
                if success else
                f"{script_name} failed during the daily pipeline run for {today}. Check current_scrape.log for details."
            )
            send_notification(conn, step_title, step_msg, step_status)
            conn.close()
        except Exception as e:
            log(f"[WARN] Could not record per-step notification for {script_name}: {e}")

    # ------------------------------------------------------------------
    # Final Summary
    # ------------------------------------------------------------------
    log("")
    log("=" * 60)
    log("DAILY PIPELINE SUMMARY")
    log("=" * 60)

    passed = [s for s, ok in results.items() if ok]
    failed = [s for s, ok in results.items() if not ok]

    for script, ok in results.items():
        status = "[OK]  SUCCESS" if ok else "[FAIL] FAILED "
        log(f"  {status}  {script}")

    log("")
    log(f"  {len(passed)}/{len(PIPELINE_SCRIPTS)} scripts completed successfully.")

    # Write final status to calendar + system_notifications
    try:
        conn = get_db_conn()
        if not results:
            final_status = 'failed'
        elif not failed:
            final_status = 'success'
        elif not passed:
            final_status = 'failed'
        else:
            final_status = 'partial'

        record_pipeline_status(conn, today, final_status, failed if failed else None)

        if final_status == 'success':
            send_notification(
                conn,
                "Daily Pipeline Completed Successfully",
                f"All {len(PIPELINE_SCRIPTS)} scraper steps completed successfully for {today}.",
                "success"
            )
        elif final_status == 'partial':
            send_notification(
                conn,
                "Daily Pipeline Completed With Errors",
                f"{len(passed)}/{len(PIPELINE_SCRIPTS)} steps succeeded. Failed: {', '.join(failed)}. Check current_scrape.log.",
                "warning"
            )
        else:
            send_notification(
                conn,
                "Daily Pipeline Failed",
                f"All scraper steps failed for {today}. Check current_scrape.log immediately.",
                "error"
            )
        conn.close()
    except Exception as e:
        log(f"[WARN] Could not write final status to database: {e}")

    log("")
    log("PIPELINE FINISHED.")


if __name__ == "__main__":
    main()
