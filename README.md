# Congress Stock Watcher

Monitors stock trade filings for specific Congress members and emails you when a new one is detected.

## Watch List (edit in watcher.py)
- Brian Higgins
- Debbie Wasserman Schultz
- David Rouzer
- Mark Green
- Nancy Pelosi

**Data sources (free, no API key needed):**
- House: House Stock Watcher S3 bucket (updated daily)
- Senate: Senate Electronic Financial Disclosure system

---

## Step 1 — Get a Gmail App Password

You need a **Gmail App Password** (not your regular password) so the script can send email.

1. Go to your Google Account → **Security**
2. Enable **2-Step Verification** if not already on
3. Search for **"App Passwords"** in your account settings
4. Create one — name it "Congress Watcher"
5. Copy the 16-character password (e.g. `abcd efgh ijkl mnop`)

---

## Step 2 — Run locally (optional test)

```bash
pip install -r requirements.txt

export EMAIL_SENDER="you@gmail.com"
export EMAIL_PASSWORD="abcdefghijklmnop"   # Gmail App Password, no spaces
export EMAIL_RECIPIENT="you@gmail.com"     # where to receive alerts

python watcher.py
```

You should see logs and an email if there are any new filings. The script will then sleep 8 hours and repeat.

---

## Step 3 — Deploy to Railway (free, runs 24/7)

Railway gives you a free hobby tier that can run a Python script forever.

### 3a. Push to GitHub

```bash
cd congress_watcher
git init
git add .
git commit -m "Initial commit"
# Create a new repo on github.com, then:
git remote add origin https://github.com/YOUR_USERNAME/congress-watcher.git
git push -u origin main
```

### 3b. Deploy on Railway

1. Go to **railway.app** and sign in with GitHub
2. Click **"New Project"** → **"Deploy from GitHub repo"**
3. Select your `congress-watcher` repo
4. Railway will detect the Python project automatically

### 3c. Set Environment Variables on Railway

In your Railway project → **Variables** tab, add:

| Variable | Value |
|---|---|
| `EMAIL_SENDER` | your Gmail address |
| `EMAIL_PASSWORD` | your 16-char App Password (no spaces) |
| `EMAIL_RECIPIENT` | email to receive alerts |

### 3d. Deploy

Click **Deploy**. Railway will build and start `python watcher.py` automatically.
It will restart itself if it ever crashes.

---

## How it works

1. Every 8 hours the script hits the House Stock Watcher API and Senate EFD
2. It checks every trade/filing against your watch list names
3. New filings get a unique ID and are stored locally in `seen_filings.json` so you only get notified once per filing
4. If anything new is found, an HTML email is sent with a table of all details + a link to the actual disclosure document

---

## Customizing

**Change the watch list:** Edit `WATCH_LIST` in `watcher.py`

**Change check frequency:** Edit `CHECK_INTERVAL_HOURS` in `watcher.py`

**Note on Senate data:** The Senate EFD search returns periodic transaction reports (PTRs), not individual trade rows like the House API. The PDF link in the email will open the actual filed document.
