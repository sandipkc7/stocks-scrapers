#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Pull latest code from GitHub and restart cron
# =============================================================================
# Run this any time you push new code to GitHub:
#
#   ./deploy.sh
#
# =============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "[deploy] Pulling latest code from GitHub..."
git pull origin main

echo "[deploy] Re-installing dependencies (in case requirements.txt changed)..."
.venv/bin/pip install -r requirements.txt -q

echo ""
echo "[deploy] Done. Running a quick connection test..."
.venv/bin/python -c "
import sys, os
sys.path.insert(0, '.')
try:
    from db_config import DB_CONFIG
    import psycopg2
    conn = psycopg2.connect(**DB_CONFIG)
    conn.close()
    print('[OK] Database connection successful.')
except Exception as e:
    print(f'[FAIL] Database connection failed: {e}')
    sys.exit(1)
"

echo ""
echo "[deploy] Deployment complete."
