import os
import re
import io
import smtplib
import logging
import requests
import pypdf
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from itertools import groupby
from collections import Counter
from supabase import create_client, Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

# Optimized Watch List (AI, Space, Energy/Electricity, Biotech/Pharma)
WATCH_LIST = [
    ("Pelosi", "House"),            # Heavy AI/Tech (Nvidia, Broadcom, Microsoft)
    ("Khanna", "House"),            # Extremely active, high volume in Tech, Bio, and Aerospace
    ("Gottheimer", "House"),        # High volume options trader, Tech, Biotech
    ("McCaul", "House"),            # Space/Defense, Tech, high net worth
    ("Hern", "House"),              # Energy, Aerospace, Pharma
    ("DelBene", "House"),           # AI/Tech (Former Microsoft executive)
    ("Beyer", "House"),             # Tech, Biotech
    ("Crenshaw", "House"),          # Tech, Space/Defense
    ("Green", "House"),             # Energy, Pharma
    ("Wasserman Schultz", "House"), # Biotech, Pharma
]

CHECK_INTERVAL_HOURS = 8
CURRENT_YEAR = datetime.now().year

# Email Credentials (Resend)
EMAIL_RECIPIENT = os.environ["EMAIL_RECIPIENT"]
RESEND_API_KEY  = os.environ["RESEND_API_KEY"]

# Supabase Credentials
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

TRANSACTION_TYPES = {
    "S": "SELL", "P": "BUY",
    "S (partial)": "SELL (partial)", "P (partial)": "BUY (partial)",
    "E": "Exchange", "W": "Exercise of Option",
}

TYPE_COLORS = {
    "SELL":           ("#fff0f0", "#c0392b"),
    "SELL (partial)": ("#fff0f0", "#c0392b"),
    "BUY":            ("#f0fff4", "#1a7a3c"),
    "BUY (partial)":  ("#f0fff4", "#1a7a3c"),
    "Exchange":       ("#f0f4ff", "#2c5aa0"),
    "Exercise of Option": ("#f5f0ff", "#6b3fa0"),
}

# ── Regex (compiled once) ─────────────────────────────────────────────────────

OWNER_RE    = re.compile(r'^(SP|DC|JT|OT|H)\s+')
TICKER_RE   = re.compile(r'\(([A-Z][A-Z0-9.]{0,5})\)')
AMT_RE      = re.compile(r'\$[\d,]+(?:\.\d{2})?\s*-\s*\$[\d,]+(?:\.\d{2})?|\$[\d,]+\.\d{2}')
AMT_CUT     = re.compile(r'(\$[\d,]+(?:\.\d{2})?)\s*-\s*$')
TAG_TX      = re.compile(
    r'\[[A-Z]{2,3}\]\s*'
    r'(S \(partial\)|P \(partial\)|S \(exchange\)|P \(exchange\)|E|W|[SP])\s+'
    r'(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}/\d{1,2}/\d{4})(.*)'
)
SKIP        = re.compile(
    r'^(F\s*S:|S\s*O:|L\s*:|D\s*:|Filing Status|Subholding|Location|Description'
    r'|I CERTIFY|Digitally|Filing ID|\* For|\bYes\b.*\bNo\b|^\s*Yes$|^\s*No$'
    r'|Name:|Status:|State/District:|Clerk of|my knowledge'
    r'|ID\s*Owner\s*Asset|TypeDate|DateAmount|Gains\s*>|\$200\?'
    r'|Equitable Advisors'
    r'|^I\s+V\s+D$|^I\s+P\s+O$|^C\s+S$|^[A-Z]\s+[A-Z]\s+[A-Z]$'
    r'|Marjorie\s+(IRA|401K|Trust)|^\w+\s+(IRA|401K|Trust)$'
    r'|^\d{1,2}/\d{1,2}\s*/\d{2,4}\.?$'
    r'|^(were |as a result|at a strike|with a strike|and an expir|\d+ shares))',
    re.I
)
BAD_TICKERS = {
    'SP','DC','JT','OT','H','ST','OT','OP','CS','DO','RE','CO',
    'ETF','LP','LLC','INC','LTD','USA','US','DR','MR','MS','IRA'
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_date(raw: str) -> str:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%b %d, %Y")
        except Exception:
            pass
    return raw.strip()

def clean_name(raw: str) -> str:
    name = re.sub(r'\b(Hon\.\.?|Dr\.?|Mr\.?|Mrs\.?|Ms\.?)\s*', '', raw)
    name = name.strip().strip(',').strip()
    if ',' in name:
        parts = [p.strip() for p in name.split(',', 1)]
        name = f"{parts[1]} {parts[0]}"
    return name.strip()

def get_ticker(asset_raw: str) -> str:
    m = TICKER_RE.search(asset_raw)
    if m:
        return m.group(1)
    for word in asset_raw.split():
        if re.match(r'^[A-Z]{2,5}$', word) and word not in BAD_TICKERS:
            return word
    return "N/A"

def clean_asset(raw: str) -> str:
    s = re.sub(r'\[.*?\].*', '', raw)
    s = TICKER_RE.sub('', s)
    s = OWNER_RE.sub('', s)
    s = re.sub(r'^\$[\d,]+\S*\s*', '', s)
    s = re.sub(r'^\d{1,2}/\d{1,2}[\s/]*\d{2,4}\.?\s*', '', s)
    s = re.sub(r'^(were |as a result|at a strike|and an expir).*', '', s, flags=re.I)
    return s.strip()

def get_amount(rest: str, next_line: str) -> str:
    m = AMT_RE.search(rest)
    if m:
        return m.group(0)
    pm = AMT_CUT.search(rest)
    if pm and next_line:
        m2 = AMT_RE.search(pm.group(1) + ' - ' + next_line.strip())
        if m2:
            return m2.group(0)
    return "N/A"

# ── Database Functions (Supabase) ─────────────────────────────────────────────

def is_filing_seen(doc_url: str) -> bool:
    uid = f"filing_{doc_url}"
    try:
        result = supabase.table("seen_filings").select("filing_id").eq("filing_id", uid).execute()
        return len(result.data) > 0
    except Exception as e:
        log.error(f"Error checking Supabase for filing {uid}: {e}")
        return False

def mark_filing_seen(doc_url: str):
    uid = f"filing_{doc_url}"
    try:
        supabase.table("seen_filings").insert({"filing_id": uid}).execute()
    except Exception as e:
        log.error(f"Error saving filing to Supabase {uid}: {e}")

def check_for_clusters(new_trade: dict) -> list:
    """Finds matching trades by OTHER politicians within the last 30 days"""
    if new_trade["ticker"] in ("N/A", "—", ""):
        return []

    try:
        # Convert our clean date format ("Feb 14, 2026") to SQL format ("2026-02-14")
        trade_date = datetime.strptime(new_trade["transaction_date"], "%b %d, %Y")
    except ValueError:
        return [] 
        
    thirty_days_ago = (trade_date - timedelta(days=30)).strftime("%Y-%m-%d")
    trade_date_str = trade_date.strftime("%Y-%m-%d")

    try:
        # Query Supabase for matching ticker & direction by a different person
        response = supabase.table("parsed_trades") \
            .select("*") \
            .eq("ticker", new_trade["ticker"]) \
            .eq("transaction_type", new_trade["transaction_type"]) \
            .neq("representative_name", new_trade["name"]) \
            .gte("transaction_date", thirty_days_ago) \
            .lte("transaction_date", trade_date_str) \
            .execute()
        return response.data
    except Exception as e:
        log.error(f"Error checking clusters in Supabase: {e}")
        return []

def log_trade(new_trade: dict):
    """Save trade to DB for future clustering lookbacks"""
    if new_trade["ticker"] in ("N/A", "—", ""):
        return

    try:
        trade_date = datetime.strptime(new_trade["transaction_date"], "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        # Fallback to today if parsing fails
        trade_date = datetime.now().strftime("%Y-%m-%d")

    try:
        supabase.table("parsed_trades").insert({
            "filing_url": new_trade["doc_url"],
            "representative_name": new_trade["name"],
            "ticker": new_trade["ticker"],
            "transaction_type": new_trade["transaction_type"],
            "amount_range": new_trade["amount"],
            "transaction_date": trade_date
        }).execute()
    except Exception as e:
        log.error(f"Error logging trade to Supabase: {e}")

# ── PDF parser ────────────────────────────────────────────────────────────────

def parse_ptr_pdf(pdf_url: str) -> list[dict]:
    transactions = []
    try:
        resp = requests.get(pdf_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        pdf  = pypdf.PdfReader(io.BytesIO(resp.content))
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        text = text.replace('\x00', '')

        sm = re.search(r'Digitally Signed[\s:].+?(\d{1,2}/\d{1,2}/\d{4})', text)
        signed_date = fmt_date(sm.group(1)) if sm else ""

        text  = re.sub(r'Filing ID #\d+', '', text)
        lines = [l.strip() for l in text.split('\n')]
        asset_lines  = []
        orphan_asset = ""
        in_tx        = False

        NO_TAG_TX = re.compile(
            r'^(.+?)\s+(S \(partial\)|P \(partial\)|[SP])\s+'
            r'(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}/\d{1,2}/\d{4})\s+'
            r'(\$[\d,]+(?:\.\d{2})?(?:\s*-\s*\$[\d,]+(?:\.\d{2})?)?)'
        )
        ORPHAN_TAG = re.compile(r'^(.*?)\[[A-Z]{2,3}\]\s*$')

        for i, line in enumerate(lines):
            if not line:
                continue

            if '$200?' in line or 'ID OwnerAsset' in line:
                in_tx        = True
                asset_lines  = []
                orphan_asset = ""
                continue

            if SKIP.match(line):
                continue
            if re.match(r'^\$[\d,]+(?:\.\d{2})?$', line):
                continue

            next_line = lines[i + 1] if i + 1 < len(lines) else ""

            tag_m = TAG_TX.search(line)
            if tag_m and in_tx:
                before_tag = OWNER_RE.sub('', line[:tag_m.start()]).strip()
                parts = []
                if orphan_asset:
                    parts.append(orphan_asset)
                    orphan_asset = ""
                parts.extend(asset_lines)
                if before_tag:
                    parts.append(before_tag)
                full_asset  = ' '.join(parts)
                asset_lines = []

                tx_raw = tag_m.group(1)
                transactions.append({
                    "ticker":            get_ticker(full_asset),
                    "asset":             clean_asset(full_asset)[:60],
                    "transaction_type":  TRANSACTION_TYPES.get(tx_raw, tx_raw),
                    "transaction_date":  fmt_date(tag_m.group(2)),
                    "notification_date": fmt_date(tag_m.group(3)),
                    "amount":            get_amount(tag_m.group(4), next_line),
                    "signed_date":       signed_date,
                })
                continue

            if not in_tx:
                continue

            orp_m = ORPHAN_TAG.match(line)
            if orp_m:
                fragment = OWNER_RE.sub('', orp_m.group(1)).strip()
                if transactions and fragment:
                    ticker_m = TICKER_RE.search(fragment)
                    if ticker_m and transactions[-1].get("ticker") == "N/A":
                        transactions[-1]["ticker"] = ticker_m.group(1)
                        existing = transactions[-1].get("asset", "")
                        extra = re.sub(r'\[.*?\]', '', fragment)
                        extra = TICKER_RE.sub('', extra).strip()
                        if extra and extra not in existing:
                            transactions[-1]["asset"] = (existing + " " + extra).strip()[:60]
                        asset_lines = []
                        continue
                orphan_asset = ' '.join(asset_lines + ([fragment] if fragment else []))
                asset_lines  = []
                continue

            no_tag_m = NO_TAG_TX.match(line)
            if no_tag_m and in_tx:
                asset_fragment = OWNER_RE.sub('', no_tag_m.group(1)).strip()
                full_asset     = ' '.join(asset_lines + ([asset_fragment] if asset_fragment else []))
                asset_lines    = []
                tx_raw         = no_tag_m.group(2)
                transactions.append({
                    "ticker":            get_ticker(full_asset),
                    "asset":             clean_asset(full_asset)[:60],
                    "transaction_type":  TRANSACTION_TYPES.get(tx_raw, tx_raw),
                    "transaction_date":  fmt_date(no_tag_m.group(3)),
                    "notification_date": fmt_date(no_tag_m.group(4)),
                    "amount":            no_tag_m.group(5),
                    "signed_date":       signed_date,
                })
                continue

            if re.match(r'^[A-Z]\s{1,3}[A-Z](\s{1,3}[A-Z])?$', line):
                continue
            if OWNER_RE.match(line) and asset_lines:
                asset_lines = []
            clean = OWNER_RE.sub('', line).strip()
            if clean:
                asset_lines.append(clean)

    except Exception as e:
        log.error(f"PDF parse error for {pdf_url}: {e}")

    key_counts: Counter = Counter()
    unique = []
    for t in transactions:
        base_key = (t["ticker"], t["transaction_date"], t["transaction_type"], t["amount"])
        key_counts[base_key] += 1
        if key_counts[base_key] == 1:
            unique.append(t)
        elif t["asset"] and t["asset"] != unique[-1].get("asset",""):
            unique.append(t)
    return unique

# ── House Clerk search ────────────────────────────────────────────────────────

def fetch_house_filings(last_name: str) -> list[dict]:
    url     = "https://disclosures-clerk.house.gov/FinancialDisclosure/ViewMemberSearchResult"
    results = []

    for year in [CURRENT_YEAR, CURRENT_YEAR - 1]:
        try:
            resp = requests.post(
                url,
                data={"LastName": last_name, "FilingYear": str(year), "State": "", "District": ""},
                headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            resp.raise_for_status()

            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', resp.text, re.DOTALL)
            for row in rows:
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                if len(cells) < 4:
                    continue
                name_raw    = re.sub(r'<[^>]+>', '', cells[0]).strip()
                filing_cell = re.sub(r'<[^>]+>', '', cells[3]).strip()
                link_match  = re.search(r'href=["\']([^"\'>\s]+)["\']', cells[0])
                doc_url     = ""
                if link_match:
                    doc_url = link_match.group(1)
                    if not doc_url.startswith("http"):
                        doc_url = "https://disclosures-clerk.house.gov/" + doc_url.lstrip("/")
                if name_raw and filing_cell and doc_url:
                    results.append({
                        "name":         clean_name(name_raw),
                        "filing_type":  filing_cell,
                        "doc_url":      doc_url,
                        "year":         str(year),
                    })
        except Exception as e:
            log.error(f"House Clerk error for '{last_name}' ({year}): {e}")

    return results

# ── Check all ─────────────────────────────────────────────────────────────────

def check_all() -> list[dict]:
    new_items = []

    for last_name, _ in WATCH_LIST:
        filings = fetch_house_filings(last_name)
        for f in filings:
            doc_url = f["doc_url"]
            if not is_filing_seen(doc_url):
                log.info(f"New filing: {f['name']} — {f['filing_type']} — parsing PDF...")
                transactions = parse_ptr_pdf(doc_url)

                if transactions:
                    for tx in transactions:
                        new_trade = {
                            "name":         f["name"],
                            "filing_type":  f["filing_type"],
                            "doc_url":      f["doc_url"],
                            **tx,
                        }
                        
                        # Database Cluster Check
                        cluster_matches = check_for_clusters(new_trade)
                        if cluster_matches:
                            new_trade["cluster_alert"] = cluster_matches
                            
                        new_items.append(new_trade)
                        
                        # Log to persistent ledger
                        log_trade(new_trade)
                else:
                    new_items.append({
                        "name":               f["name"],
                        "filing_type":        f["filing_type"],
                        "doc_url":            f["doc_url"],
                        "ticker":             "—",
                        "asset":              "See filing",
                        "transaction_type":   "—",
                        "transaction_date":   "—",
                        "notification_date":  "—",
                        "amount":             "—",
                        "signed_date":        "—",
                    })
                
                # Mark as processed in Supabase
                mark_filing_seen(doc_url)

    return new_items

# ── Email ─────────────────────────────────────────────────────────────────────

def build_email_html(items: list[dict]) -> str:
    # Handle the empty items case unconditionally
    if not items:
        return f"""<html><body style="font-family:sans-serif;color:#222;max-width:960px;margin:auto;padding:24px;">
  <h2 style="color:#1a1a2e;margin-bottom:4px;">Congress Stock Filing Alert</h2>
  <p style="color:#555;margin-top:0;">
    No new transactions found during this check.
  </p>
  <p style="color:#aaa;font-size:11px;margin-top:24px;">
    Source: disclosures-clerk.house.gov &middot; Checks every {CHECK_INTERVAL_HOURS}h
  </p>
</body></html>"""

    rows  = ""
    total = len(items)
    names = ", ".join(sorted({t["name"] for t in items}))
    MAX_ROWS_PER_FILING = 30 

    for doc_url, group in groupby(items, key=lambda x: x.get("doc_url", "")):
        group_list    = list(group)
        is_first_row  = True
        shown         = 0
        hidden_count  = 0

        for tx in group_list:
            if shown >= MAX_ROWS_PER_FILING:
                hidden_count += 1
                continue

            tx_type    = tx.get("transaction_type", "—")
            bg, fg     = TYPE_COLORS.get(tx_type, ("#fff", "#222"))
            name_cell  = f"<b>{tx['name']}</b>" if is_first_row else ""
            link       = (f'<a href="{doc_url}" style="color:#3b6fd4;text-decoration:underline;">PDF ↗</a>'
                          if is_first_row and doc_url else "")
            row_border = "border-top:2px solid #ccc;" if is_first_row else "border-top:1px solid #f0f0f0;"
            is_first_row = False
            shown += 1

            # Inject the Cluster Alert visual indicator inside the asset column if triggered
            cluster_html = ""
            if tx.get("cluster_alert"):
                matches = tx["cluster_alert"]
                cluster_text = "<br>".join([
                    f"🚨 <b>{m['representative_name']}</b> also executed a <b>{m['transaction_type']}</b> on {m['transaction_date']} ({m.get('amount_range', '—')})"
                    for m in matches
                ])
                cluster_html = f'<div style="margin-top:8px;padding:8px;background:#fff3cd;color:#856404;border-radius:4px;font-size:11px;border:1px solid #ffeeba;"><b>CLUSTER ALERT:</b><br>{cluster_text}</div>'

            rows += f"""
        <tr style="{row_border}">
          <td style="padding:9px 8px;">{name_cell}</td>
          <td style="padding:9px 8px;font-weight:600;color:{fg};background:{bg};text-align:center;">{tx_type}</td>
          <td style="padding:9px 8px;font-weight:600;">{tx.get('ticker','—')}</td>
          <td style="padding:9px 8px;font-size:12px;">{tx.get('asset','—')}{cluster_html}</td>
          <td style="padding:9px 8px;">{tx.get('transaction_date','—')}</td>
          <td style="padding:9px 8px;">{tx.get('notification_date','—')}</td>
          <td style="padding:9px 8px;">{tx.get('amount','—')}</td>
          <td style="padding:9px 8px;">{link}</td>
        </tr>"""

        if hidden_count > 0:
            rows += f"""
        <tr style="border-top:1px solid #f0f0f0;background:#fffbe6;">
          <td colspan="8" style="padding:8px;font-size:12px;color:#888;">
            + {hidden_count} more transaction(s) not shown &mdash;
            <a href="{doc_url}" style="color:#3b6fd4;">view full PDF ↗</a>
          </td>
        </tr>"""

    return f"""<html><body style="font-family:sans-serif;color:#222;max-width:1100px;margin:auto;padding:24px;">
  <h2 style="color:#1a1a2e;margin-bottom:4px;">Congress Stock Filing Alert</h2>
  <p style="color:#555;margin-top:0;">
    <b>{total}</b> new transaction(s) from <b>{names}</b><br>
    <span style="font-size:12px;color:#999;">Detected {datetime.now().strftime('%B %d, %Y at %H:%M UTC')}</span>
  </p>
  <table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:16px;">
    <thead>
      <tr style="background:#1a1a2e;color:#fff;text-align:left;">
        <th style="padding:10px 8px;">Name</th>
        <th style="padding:10px 8px;">Action</th>
        <th style="padding:10px 8px;">Ticker</th>
        <th style="padding:10px 8px;">Asset</th>
        <th style="padding:10px 8px;">Trade Date</th>
        <th style="padding:10px 8px;">Notified</th>
        <th style="padding:10px 8px;">Amount</th>
        <th style="padding:10px 8px;">PDF</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  <p style="color:#aaa;font-size:11px;margin-top:24px;">
    Source: disclosures-clerk.house.gov &middot; Checks every {CHECK_INTERVAL_HOURS}h
  </p>
</body></html>"""

def send_email(items: list[dict]):
    # Note: We now only need EMAIL_RECIPIENT and RESEND_API_KEY from os.environ
    recipient = os.environ["EMAIL_RECIPIENT"]
    resend_key = os.environ["RESEND_API_KEY"]
    
    if items:
        names = ", ".join(sorted({t["name"] for t in items}))
        subject = f"Congress Trade Alert — {len(items)} transaction(s) — {names}"
        plain = "\n".join(
            f"{t['name']} | {t['transaction_type']} {t['ticker']} | {t['transaction_date']} | {t['amount']}"
            for t in items
        )
    else:
        subject = "Congress Trade Status — No New Transactions"
        plain = "No new transactions found during this check."

    html_content = build_email_html(items)

    # Use Resend's API to bypass the Railway SMTP block
    headers = {
        "Authorization": f"Bearer {resend_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "from": "Congress Watcher <onboarding@resend.dev>", # Resend's default testing address
        "to": recipient,
        "subject": subject,
        "html": html_content,
        "text": plain
    }

    response = requests.post("https://api.resend.com/emails", json=payload, headers=headers)
    
    if response.status_code in (200, 201):
        status = f"{len(items)} transaction(s)" if items else "No new transactions"
        log.info(f"Email sent successfully via Resend — {status}")
    else:
        log.error(f"Failed to send email via Resend: {response.text}")
        response.raise_for_status()

# ── Main loop ─────────────────────────────────────────────────────────────────

def run():
    names = [f"{ln} (House)" for ln, _ in WATCH_LIST]
    log.info(f"Congress Watcher started. Watching: {', '.join(names)}")

    log.info("Running check...")
    new_items = check_all()

    if new_items:
        log.info(f"Found {len(new_items)} new transaction(s). Sending email...")
    else:
        log.info("No new filings found. Sending status email...")

    try:
        # Email will now trigger regardless of new_items being empty or not
        send_email(new_items)
    except Exception as e:
        log.error(f"Failed to send email: {e}")
        raise

    log.info("Done.")

if __name__ == "__main__":
    while True:
        try:
            run()
        except Exception as e:
            log.error(f"An error occurred during the run: {e}")
        
        # Calculate seconds to sleep based on your 8-hour variable
        sleep_seconds = CHECK_INTERVAL_HOURS * 3600
        log.info(f"Sleeping for {CHECK_INTERVAL_HOURS} hours before the next check...")
        time.sleep(sleep_seconds)