"""
NEPSE Daily Pipeline — Standalone Python Server
=================================================
Designed to run on a server that has ONLY Python installed.
Connects directly to a remote PostgreSQL database via config.ini.

Pipeline order:
  1. nepse_holiday.py      - refresh holiday calendar
  2. Holiday/Saturday check - abort if non-trading day
  3. nepse_companies.py    - update company list
  4. chukulscraper_safe.py - fetch floorsheet data
  5. nepse_daily_share.py  - fetch daily share prices
  6. nepse_live_index.py   - fetch live/current index
  7. process_summary.py    - aggregate broker summaries
  8. compute_indicators.py - calculate technical indicators

Scheduling (Linux cron):
  45 9 * * *  user  cd /path/to/pythonserver && ./.venv/bin/python daily_pipeline.py >> pipeline.log 2>&1

Deployment (GitHub):
  git pull && echo "Updated."
"""
import sys
import os
import subprocess
import psycopg2
from datetime import date, datetime
import traceback

# Force UTF-8 output to avoid encoding errors on non-UTF-8 terminals
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

PIPELINE_SCRIPTS = [
    'nepse_companies.py',
    'chukulscraper_safe.py',
    'nepse_daily_share.py',
    'nepse_live_index.py',
    'process_summary.py',
    'compute_indicators.py',
    'check_alerts_email.py',      # Step 9: Evaluate alerts & queue notification emails
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_python_bin():
    is_windows = sys.platform.startswith('win')
    venv = os.path.join(SCRIPT_DIR, '.venv',
                        'Scripts' if is_windows else 'bin',
                        'python.exe' if is_windows else 'python')
    return venv if os.path.exists(venv) else ('python' if is_windows else 'python3')

def get_db_conn():
    sys.path.insert(0, SCRIPT_DIR)
    from db_config import DB_CONFIG
    return psycopg2.connect(**DB_CONFIG)

def log(msg):
    timestamp = datetime.now().strftime('%H:%M:%S')
    print(f"[{timestamp}] {msg}", flush=True)

# ---------------------------------------------------------------------------
# DB Tracking
# ---------------------------------------------------------------------------

def ensure_pipeline_columns(conn):
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
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO calendar (date) VALUES (%s) ON CONFLICT (date) DO NOTHING;", (run_date,))
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
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS system_notifications (
                id SERIAL PRIMARY KEY, type VARCHAR(30) NOT NULL DEFAULT 'info',
                title VARCHAR(255) NOT NULL, message TEXT, source VARCHAR(100),
                is_read BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    today = date.today()

    # Saturday is always non-trading in Nepal
    if today.weekday() == 5:
        return True, "Saturday is a non-trading day in Nepal."

    try:
        cur = conn.cursor()
        cur.execute(
            'SELECT holiday, "Holiday_Description" FROM calendar WHERE date = %s',
            (today,)
        )
        row = cur.fetchone()
        cur.close()
        if row and row[0] is True:
            desc = row[1] or 'Public Holiday'
            return True, f"Marked as holiday: {desc}"
    except Exception as e:
        log(f"[WARN] Could not query calendar for holiday: {e}. Assuming trading day.")

    return False, ''

def get_holiday_data_freshness(conn):
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT MAX(date) FROM calendar
            WHERE holiday = TRUE AND date >= CURRENT_DATE - INTERVAL '365 days'
        """)
        row = cur.fetchone()
        cur.close()
        return row[0] if row else None
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Script Execution
# ---------------------------------------------------------------------------

def run_script(python_bin, script_name, args=None):
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
# Main
# ---------------------------------------------------------------------------

def main():
    today = date.today()
    log("=" * 60)
    log("NEPSE DAILY PIPELINE (Standalone Python Server)")
    log(f"Date: {today.strftime('%A, %d %B %Y')}")
    log("=" * 60)

    python_bin = get_python_bin()
    log(f"Python: {python_bin}")

    # STEP 1: Refresh holidays
    log("")
    log("STEP 1/8: Refreshing holiday calendar...")
    run_script(python_bin, 'nepse_holiday.py')

    # STEP 2: DB connect + holiday freshness check
    log("")
    log("STEP 2/8: Checking trading day status...")
    try:
        conn = get_db_conn()
    except Exception as e:
        log(f"[FATAL] Cannot connect to database: {e}")
        log(f"[HINT]  Check your config.ini credentials and ensure the DB host is reachable from this server.")
        sys.exit(1)

    ensure_pipeline_columns(conn)

    last_holiday = get_holiday_data_freshness(conn)
    if last_holiday:
        days_since = (today - last_holiday).days
        if days_since > 30:
            log(f"[WARN] Holiday data may be stale — last holiday: {last_holiday} ({days_since} days ago).")
        else:
            log(f"[INFO] Holiday data is current. Last holiday on record: {last_holiday}.")

    # STEP 3: Holiday / non-trading day check
    is_holiday, reason = is_today_non_trading(conn)
    if is_holiday:
        msg = f"Today ({today}) is a non-trading day. Reason: {reason}"
        log(msg)
        send_notification(conn, "Daily Pipeline — Non-Trading Day", msg, "info")
        record_pipeline_status(conn, today, 'holiday')
        conn.close()
        log("PIPELINE COMPLETE: Stopped — non-trading day.")
        sys.exit(0)

    log(f"[OK] Today ({today}) is a trading day. Proceeding.")
    send_notification(conn, "Daily Pipeline Started",
                      f"Trading day confirmed for {today}. Running {len(PIPELINE_SCRIPTS)} scraper steps.",
                      "scraper")
    conn.close()

    # STEPS 4-9: Run scrapers
    results = {}
    for script_name in PIPELINE_SCRIPTS:
        args = []
        if script_name == 'nepse_daily_share.py':
            args = ['--force']
        success = run_script(python_bin, script_name, args=args)
        results[script_name] = success
        try:
            conn = get_db_conn()
            send_notification(conn,
                f"Pipeline Step {'OK' if success else 'FAILED'}: {script_name}",
                f"{script_name} {'completed successfully' if success else 'FAILED — check pipeline.log'} for {today}.",
                'success' if success else 'error')
            conn.close()
        except Exception as e:
            log(f"[WARN] Could not record notification for {script_name}: {e}")

    # Final summary
    passed = [s for s, ok in results.items() if ok]
    failed = [s for s, ok in results.items() if not ok]

    log("")
    log("=" * 60)
    log("DAILY PIPELINE SUMMARY")
    log("=" * 60)
    for script, ok in results.items():
        log(f"  {'[OK]  SUCCESS' if ok else '[FAIL] FAILED '}  {script}")
    log(f"\n  {len(passed)}/{len(PIPELINE_SCRIPTS)} scripts completed successfully.")

    try:
        conn = get_db_conn()
        if not failed:
            status, notif_type = 'success', 'success'
            title = "Daily Pipeline Completed Successfully"
            msg = f"All {len(PIPELINE_SCRIPTS)} steps completed for {today}."
        elif not passed:
            status, notif_type = 'failed', 'error'
            title = "Daily Pipeline Failed"
            msg = f"All steps failed for {today}. Check pipeline.log."
        else:
            status, notif_type = 'partial', 'warning'
            title = "Daily Pipeline Completed With Errors"
            msg = f"{len(passed)}/{len(PIPELINE_SCRIPTS)} steps succeeded. Failed: {', '.join(failed)}."

        record_pipeline_status(conn, today, status, failed or None)
        send_notification(conn, title, msg, notif_type)
        conn.close()
    except Exception as e:
        log(f"[WARN] Could not write final DB status: {e}")

    log("\nPIPELINE FINISHED.")

if __name__ == "__main__":
    main()
