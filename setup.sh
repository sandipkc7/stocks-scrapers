#!/usr/bin/env bash
# =============================================================================
# setup.sh — One-time setup for the NEPSE Python standalone server
# =============================================================================
# Run this ONCE after cloning the repo on your Python-only server:
#
#   git clone https://github.com/YOUR_USER/YOUR_REPO.git .
#   cd pythonserver
#   chmod +x setup.sh
#   ./setup.sh
#
# =============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "===================================================="
echo "  NEPSE Python Server — One-Time Setup"
echo "===================================================="
echo ""

# 1. Check Python version
PYTHON=$(which python3 || which python)
PY_VERSION=$($PYTHON --version 2>&1)
echo "[1/5] Python found: $PY_VERSION"

# 2. Create virtual environment
if [ ! -d ".venv" ]; then
    echo "[2/5] Creating virtual environment (.venv)..."
    $PYTHON -m venv .venv
else
    echo "[2/5] Virtual environment already exists. Skipping."
fi

# 3. Install dependencies
echo "[3/5] Installing Python dependencies from requirements.txt..."
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt

echo "[4/5] Installing nepse library from GitHub..."
.venv/bin/pip install "nepse @ git+https://github.com/basic-bgnr/NepseUnofficialApi.git@dev"

# 4. Create config.ini from example if not present
if [ ! -f "config.ini" ]; then
    echo ""
    echo "[5/5] config.ini not found. Creating from template..."
    cp config.ini.example config.ini
    echo ""
    echo "  ACTION REQUIRED: Edit config.ini with your database credentials:"
    echo "  ------------------------------------------------------------------"
    echo "    nano config.ini"
    echo "  ------------------------------------------------------------------"
    echo ""
else
    echo "[5/5] config.ini already exists. Skipping."
fi

# 5. Install cron job
echo ""
echo "  To install the daily cron job (3:30 PM Nepal time every day):"
echo "  ---------------------------------------------------------------"
echo "  (crontab -l 2>/dev/null; echo '45 9 * * * cd $SCRIPT_DIR && ./.venv/bin/python daily_pipeline.py >> pipeline.log 2>&1') | crontab -"
echo ""
echo "  Or for a system-wide cron (runs as current user):"
echo "  sudo bash -c \"echo '45 9 * * * $(whoami) cd $SCRIPT_DIR && ./.venv/bin/python daily_pipeline.py 2>&1 | tee -a pipeline.log > current_scrape.log' > /etc/cron.d/nepse_pipeline\""
echo "  sudo chmod 644 /etc/cron.d/nepse_pipeline"
echo ""
echo "===================================================="
echo "  Setup complete! Next steps:"
echo "    1. Edit config.ini with your DB credentials"
echo "    2. Test: ./.venv/bin/python daily_pipeline.py"
echo "    3. Install cron job (see above)"
echo "===================================================="
