# NEPSE Python Server

Standalone Python scraping server for NEPSE data.  
No PHP or local PostgreSQL required — connects directly to a remote PostgreSQL database.

---

## Folder Structure

```
pythonserver/
├── config.ini.example      ← DB credentials template (safe to commit)
├── config.ini              ← Your actual credentials (NEVER commit this)
├── db_config.py            ← Reads config.ini, provides DB_CONFIG
├── requirements.txt        ← Python dependencies
├── daily_pipeline.py       ← Master orchestrator
├── setup.sh                ← One-time server setup
├── deploy.sh               ← Git pull + dependency refresh
├── nepse_holiday.py
├── nepse_companies.py
├── chukulscraper_safe.py
├── nepse_daily_share.py
├── nepse_live_index.py
├── process_summary.py
├── compute_indicators.py
└── .gitignore              ← Excludes config.ini, .venv, logs
```

---

## First-Time Server Setup

### 1. Push this folder to GitHub (from your local Windows machine)

```bash
# In your stocks project root on Windows (Git Bash or PowerShell):
cd C:/xampp/htdocs/stocks
git init                              # if not already a git repo
git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
git add pythonserver/
git commit -m "Add standalone Python server"
git push -u origin main
```

> **Important:** Make sure `config.ini` is in `.gitignore` before pushing!

---

### 2. SSH into your Python server and clone the repo

```bash
ssh user@your-python-server-ip

# Clone just the pythonserver subfolder using sparse checkout:
git clone --no-checkout https://github.com/YOUR_USER/YOUR_REPO.git nepse
cd nepse
git sparse-checkout init --cone
git sparse-checkout set pythonserver
git checkout main
cd pythonserver
```

---

### 3. Run the one-time setup

```bash
chmod +x setup.sh deploy.sh
./setup.sh
```

This will:
- Create `.venv/` with all Python dependencies
- Create `config.ini` from the template

---

### 4. Fill in your database credentials

```bash
nano config.ini
```

```ini
[database]
host     = your-postgres-host.example.com
dbname   = your_database_name
user     = your_db_user
password = your_db_password
port     = 5432
```

---

### 5. Test the connection and run manually

```bash
# Quick DB connection test
.venv/bin/python -c "from db_config import DB_CONFIG; import psycopg2; psycopg2.connect(**DB_CONFIG); print('Connected!')"

# Run the full pipeline manually
.venv/bin/python daily_pipeline.py
```

---

### 6. Install the daily cron job (3:30 PM Nepal Time = 09:45 UTC)

```bash
# Add to current user's crontab:
(crontab -l 2>/dev/null; echo "45 9 * * * cd $(pwd) && ./.venv/bin/python daily_pipeline.py 2>&1 | tee -a pipeline.log > current_scrape.log") | crontab -

# Verify it was added:
crontab -l
```

---

## Updating Code (After Pushing Changes from Windows)

Every time you push new code to GitHub:

**On your Windows machine:**
```bash
git add pythonserver/
git commit -m "Update scrapers"
git push
```

**On the Python server:**
```bash
cd /path/to/nepse/pythonserver
./deploy.sh
```

`deploy.sh` will:
1. `git pull` the latest code
2. Reinstall any new dependencies
3. Run a quick DB connection test

---

## Monitoring

- **Live log during pipeline run:** `tail -f current_scrape.log`
- **Historical pipeline runs:** `tail -n 100 pipeline.log`
- **Pipeline status in DB:** check `system_notifications` table or the `pipeline_status` column in `calendar`

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `ModuleNotFoundError` | Run `./deploy.sh` to reinstall deps |
| `connection refused` | Check `config.ini` host/port; ensure DB allows remote connections |
| `permission denied` | Check DB user has INSERT/UPDATE rights on required tables |
| Cron not firing | `sudo systemctl status cron` and check `crontab -l` |
