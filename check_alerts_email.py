"""
Check Alerts & Queue Email Notifications
=========================================
Runs after compute_indicators.py in the daily pipeline.

1. Queries all active alerts from price_alerts table
2. Compares each alert's condition/threshold against latest daily_prices data
3. For triggered alerts:
   - Checks user_email_preferences.receive_alerts
   - Looks up the user's email from users table
   - Renders the "Price Alert" template with personalised data
   - Inserts into email_queue with source = 'alert'
   - Marks the alert as triggered (is_active = FALSE for one-shot)
4. Optionally calls process_queue.php via CLI to flush the queue

Schedule: Configurable via email_config.alert_schedule (default: 8PM NPT on trading days)
"""

import sys
import os
import subprocess
from datetime import datetime

# Force UTF-8 output
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from db_config import DB_CONFIG
import psycopg2
import psycopg2.extras


def log(msg):
    timestamp = datetime.now().strftime('%H:%M:%S')
    print(f"[{timestamp}] {msg}", flush=True)


def get_db_conn():
    return psycopg2.connect(**DB_CONFIG)


def ensure_tables(conn):
    """Ensure required tables exist."""
    cur = conn.cursor()
    
    # price_alerts may already exist from alerts_api.php
    cur.execute("""
        CREATE TABLE IF NOT EXISTS price_alerts (
            id SERIAL PRIMARY KEY,
            user_id INT NOT NULL,
            symbol VARCHAR(50) NOT NULL,
            condition VARCHAR(30) NOT NULL,
            threshold NUMERIC NOT NULL,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # email_queue must exist
    cur.execute("""
        CREATE TABLE IF NOT EXISTS email_queue (
            id SERIAL PRIMARY KEY,
            user_id INT,
            to_email VARCHAR(255) NOT NULL,
            subject VARCHAR(255) NOT NULL,
            body_html TEXT NOT NULL,
            status VARCHAR(20) DEFAULT 'pending',
            error_message TEXT,
            template_id INT,
            source VARCHAR(50) DEFAULT 'manual',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP,
            attempts INT DEFAULT 0
        )
    """)
    
    # user_email_preferences
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_email_preferences (
            id SERIAL PRIMARY KEY,
            user_id INT NOT NULL UNIQUE,
            receive_alerts BOOLEAN DEFAULT TRUE,
            receive_marketing BOOLEAN DEFAULT TRUE,
            receive_system BOOLEAN DEFAULT TRUE,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    conn.commit()
    cur.close()


def get_active_alerts(conn):
    """Fetch all active price alerts with user info."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("""
        SELECT 
            pa.id, pa.user_id, pa.symbol, pa.condition, pa.threshold,
            u.username, u.email
        FROM price_alerts pa
        JOIN users u ON u.id = pa.user_id
        WHERE pa.is_active = TRUE
          AND u.email IS NOT NULL 
          AND u.email != ''
        ORDER BY pa.user_id, pa.symbol
    """)
    alerts = cur.fetchall()
    cur.close()
    return alerts


def get_latest_price(conn, symbol):
    """Get the latest closing price for a symbol from daily_prices."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("""
        SELECT close_price, date 
        FROM daily_prices 
        WHERE symbol = %s 
        ORDER BY date DESC 
        LIMIT 1
    """, (symbol,))
    row = cur.fetchone()
    cur.close()
    return row


def check_alert_condition(condition, threshold, current_price):
    """
    Evaluate whether an alert condition is triggered.
    
    Conditions:
        above     - price > threshold
        below     - price < threshold
        rsi_above - (handled separately, not price-based)
        rsi_below - (handled separately, not price-based)
    """
    threshold = float(threshold)
    current_price = float(current_price)
    
    if condition == 'above':
        return current_price > threshold
    elif condition == 'below':
        return current_price < threshold
    
    return False


def check_rsi_condition(conn, symbol, condition, threshold):
    """Check RSI-based alert conditions from computed_indicators."""
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("""
            SELECT rsi_14 
            FROM computed_indicators 
            WHERE symbol = %s 
            ORDER BY date DESC 
            LIMIT 1
        """, (symbol,))
        row = cur.fetchone()
        cur.close()
        
        if row and row['rsi_14'] is not None:
            rsi = float(row['rsi_14'])
            threshold = float(threshold)
            if condition == 'rsi_above':
                return rsi > threshold, rsi
            elif condition == 'rsi_below':
                return rsi < threshold, rsi
    except Exception:
        pass
    
    return False, None


def get_condition_text(condition):
    """Human-readable condition descriptions."""
    texts = {
        'above': 'risen above',
        'below': 'dropped below',
        'rsi_above': 'RSI has risen above',
        'rsi_below': 'RSI has dropped below',
    }
    return texts.get(condition, condition)


def check_user_email_preference(conn, user_id):
    """Check if user has opted in to alert emails."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("""
        SELECT receive_alerts 
        FROM user_email_preferences 
        WHERE user_id = %s
    """, (user_id,))
    row = cur.fetchone()
    cur.close()
    
    # Default is TRUE (opted in) if no preference record exists
    if row is None:
        return True
    return bool(row['receive_alerts'])


def get_alert_template(conn):
    """Fetch the default alert email template."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("""
        SELECT id, subject, body_html 
        FROM email_templates 
        WHERE type = 'alert' AND is_default = TRUE
        ORDER BY id ASC 
        LIMIT 1
    """)
    row = cur.fetchone()
    cur.close()
    
    if row:
        return dict(row)
    
    # Fallback template if none in DB
    return {
        'id': None,
        'subject': '🔔 Alert: {{symbol}} has {{condition_text}} {{threshold}}',
        'body_html': '''<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
            <h2>🔔 Price Alert Triggered</h2>
            <p>Hello {{username}},</p>
            <p>Your alert for <strong>{{symbol}}</strong> has been triggered:</p>
            <ul>
                <li>Condition: {{condition_text}}</li>
                <li>Threshold: Rs. {{threshold}}</li>
                <li>Current Price: Rs. {{current_price}}</li>
            </ul>
            <p>Triggered on {{date}} at {{time}}</p>
            <hr><p style="color:#888;font-size:12px;">NexTrade — Stock Analysis Platform</p>
        </div>'''
    }


def render_template(template_str, data):
    """Replace {{placeholders}} with actual values."""
    result = template_str
    for key, value in data.items():
        result = result.replace('{{' + key + '}}', str(value))
    return result


def queue_alert_email(conn, user_id, to_email, subject, body_html, template_id=None):
    """Insert a rendered email into the email_queue table."""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO email_queue (user_id, to_email, subject, body_html, source, template_id, status)
        VALUES (%s, %s, %s, %s, 'alert', %s, 'pending')
    """, (user_id, to_email, subject, body_html, template_id))
    conn.commit()
    cur.close()


def deactivate_alert(conn, alert_id):
    """Mark an alert as inactive after it triggers."""
    cur = conn.cursor()
    cur.execute("UPDATE price_alerts SET is_active = FALSE WHERE id = %s", (alert_id,))
    conn.commit()
    cur.close()


def send_notification(conn, title, message='', notif_type='info', source='check_alerts_email.py'):
    """Send a system notification (same as daily_pipeline.py)."""
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO system_notifications (type, title, message, source, is_read)
            VALUES (%s, %s, %s, %s, FALSE)
        """, (notif_type, title, message, source))
        conn.commit()
        cur.close()
    except Exception as e:
        log(f"[WARN] Could not save notification: {e}")


def process_queue_via_php():
    """Call the PHP queue processor to flush pending emails."""
    try:
        # Find PHP binary
        php_paths = [
            r'C:\xampp\php\php.exe',         # Windows XAMPP
            '/usr/bin/php',                    # Linux
            '/usr/local/bin/php',              # macOS
        ]
        
        php_bin = None
        for p in php_paths:
            if os.path.exists(p):
                php_bin = p
                break
        
        if not php_bin:
            # Try system PATH
            php_bin = 'php'
        
        # Path to process_queue.php (relative to this script's location)
        # This script is in pythonserver/, process_queue.php is in public/admin/mail/
        queue_script = os.path.join(SCRIPT_DIR, '..', 'public', 'admin', 'mail', 'process_queue.php')
        queue_script = os.path.normpath(queue_script)
        
        if os.path.exists(queue_script):
            log(f"Calling PHP queue processor: {queue_script}")
            result = subprocess.run(
                [php_bin, queue_script, '--limit=100'],
                capture_output=True, text=True, timeout=120
            )
            if result.stdout:
                for line in result.stdout.strip().split('\n'):
                    log(f"  [PHP] {line}")
            if result.returncode != 0 and result.stderr:
                log(f"  [PHP ERROR] {result.stderr[:200]}")
        else:
            log(f"[WARN] Queue processor not found at: {queue_script}")
            
    except Exception as e:
        log(f"[WARN] Could not invoke PHP queue processor: {e}")


def main():
    log("=" * 60)
    log("CHECK ALERTS & QUEUE EMAIL NOTIFICATIONS")
    log("=" * 60)
    
    try:
        conn = get_db_conn()
    except Exception as e:
        log(f"[FATAL] Cannot connect to database: {e}")
        sys.exit(1)
    
    ensure_tables(conn)
    
    # Fetch active alerts
    alerts = get_active_alerts(conn)
    log(f"Found {len(alerts)} active alert(s) to check")
    
    if not alerts:
        log("No active alerts. Exiting.")
        conn.close()
        return
    
    # Get the email template
    template = get_alert_template(conn)
    
    triggered_count = 0
    skipped_count = 0
    queued_count = 0
    
    for alert in alerts:
        symbol = alert['symbol']
        condition = alert['condition']
        threshold = alert['threshold']
        user_id = alert['user_id']
        username = alert['username']
        email = alert['email']
        alert_id = alert['id']
        
        # Check condition
        is_triggered = False
        current_value = None
        
        if condition in ('above', 'below'):
            price_data = get_latest_price(conn, symbol)
            if price_data and price_data['close_price'] is not None:
                current_value = float(price_data['close_price'])
                is_triggered = check_alert_condition(condition, threshold, current_value)
        elif condition in ('rsi_above', 'rsi_below'):
            is_triggered, current_value = check_rsi_condition(conn, symbol, condition, threshold)
        
        if not is_triggered:
            continue
        
        triggered_count += 1
        log(f"  ⚡ TRIGGERED: {symbol} — {get_condition_text(condition)} {threshold} (current: {current_value}) for user {username}")
        
        # Check user email preference
        if not check_user_email_preference(conn, user_id):
            skipped_count += 1
            log(f"    ⏭ Skipped: User {username} opted out of alert emails")
            continue
        
        # Render template
        now = datetime.now()
        data = {
            'username': username,
            'email': email,
            'symbol': symbol,
            'condition_text': get_condition_text(condition),
            'threshold': f"{float(threshold):,.2f}",
            'current_price': f"{current_value:,.2f}" if current_value else 'N/A',
            'date': now.strftime('%Y-%m-%d'),
            'time': now.strftime('%H:%M:%S'),
        }
        
        rendered_subject = render_template(template['subject'], data)
        rendered_body = render_template(template['body_html'], data)
        
        # Queue the email
        queue_alert_email(conn, user_id, email, rendered_subject, rendered_body, template.get('id'))
        queued_count += 1
        log(f"    ✉ Queued alert email to {email}")
        
        # Deactivate the alert (one-shot)
        deactivate_alert(conn, alert_id)
        log(f"    🔕 Alert #{alert_id} deactivated")
    
    # Summary
    log("")
    log("=" * 60)
    log("ALERT CHECK SUMMARY")
    log(f"  Checked:   {len(alerts)} alert(s)")
    log(f"  Triggered: {triggered_count}")
    log(f"  Skipped:   {skipped_count} (opted out)")
    log(f"  Queued:    {queued_count} email(s)")
    log("=" * 60)
    
    # Send system notification
    if triggered_count > 0:
        send_notification(
            conn,
            f"Alert Check: {triggered_count} alert(s) triggered",
            f"{queued_count} email(s) queued for delivery, {skipped_count} skipped (opted out).",
            'info' if queued_count > 0 else 'warning'
        )
    
    conn.close()
    
    # Process the queue
    if queued_count > 0:
        log("\nProcessing email queue...")
        process_queue_via_php()
    
    log("\nALERT CHECK COMPLETE.")


if __name__ == '__main__':
    main()
