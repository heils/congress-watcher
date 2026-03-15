import os
import re
import io
import json
import time
import smtplib
import logging
import requests
import pypdf
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

WATCH_LIST = [
    ("Higgins",           "House"),
    ("Wasserman Schultz", "House"),
    ("Rouzer",            "House"),
    ("Green",             "House"),
    ("Pelosi",            "House"),
]

CHECK_INTERVAL_HOURS = 8
SEEN_FILE    = "seen_filings.json"
CURRENT_YEAR = datetime.now().year

EMAIL_SENDER    = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD  = os.environ["EMAIL_PASSWORD"]
EMAIL_RECIPIENT = os.environ["EMAIL_RECIPIENT"]

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
TICKER_RE   = re.compile(r'\(([A-Z][A-Z0-9.]{0,5})\)')   # handles BRK.B, etc.
AMT_RE      = re.compile(r'\$[\d,]+(?:\.\d{2})?\s*-\s*\$[\d,]+(?:\.\d{2})?|\$[\d,]+\.\d{2}')
AMT_CUT     = re.compile(r'(\$[\d,]+(?:\.\d{2})?)\s*-\s*$')
# Core pattern: [TAG] immediately (or with 1 space) before S/P + two dates
TAG_TX      = re.compile(
    r'\[[A-Z]{2,3}\]\s*'
    r'(S \(partial\)|P \(partial\)|S \(exchange\)|P \(exchange\)|E|W|[SP])\s+'
    r'(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}/\d{1,2}/\d{4})(.*)'
)
# Boilerplate lines to skip — expanded to catch post-table junk
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
    s = re.sub(r'\[.*?\].*', '', raw)   # strip from [TAG] onward
    s = TICKER_RE.sub('', s)
    s = OWNER_RE.sub('', s)
    s = re.sub(r'^\$[\d,]+\S*\s*', '', s)
    # Strip leading date fragments like "1/16 /26." that leak from description lines
    s = re.sub(r'^\d{1,2}/\d{1,2}[\s/]*\d{2,4}\.?\s*', '', s)
    # Strip leading description fragments
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

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

# ── PDF parser ────────────────────────────────────────────────────────────────

def parse_ptr_pdf(pdf_url: str) -> list[dict]:
    """
    Download a House PTR PDF and extract all individual transactions.

    Real PDFs from disclosures-clerk.house.gov contain null bytes (\\x00) between
    characters. After stripping those, each transaction line looks like:

        AssetName (TICKER)  [ST]S MM/DD/YYYY MM/DD/YYYY $amount
        AssetName  [OT] P MM/DD/YYYY MM/DD/YYYY $amount -    <- amount may wrap

    Asset names often span multiple preceding lines.
    """
    transactions = []
    try:
        resp = requests.get(pdf_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        pdf  = pypdf.PdfReader(io.BytesIO(resp.content))
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        text = text.replace('\x00', '')

        sm = re.search(r'Digitally Signed[\s:].+?(\d{1,2}/\d{1,2}/\d{4})', text)
        signed_date = fmt_date(sm.group(1)) if sm else ""

        # Strip "Filing ID #XXXXX" that pypdf sometimes appends mid-line
        text  = re.sub(r'Filing ID #\d+', '', text)
        lines = [l.strip() for l in text.split('\n')]
        asset_lines  = []
        orphan_asset = ""   # ticker/asset saved from an orphaned [TAG] line
        in_tx        = False

        # Fallback: line has dates but no [TAG] — catches Broadcom page-break case
        NO_TAG_TX = re.compile(
            r'^(.+?)\s+(S \(partial\)|P \(partial\)|[SP])\s+'
            r'(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}/\d{1,2}/\d{4})\s+'
            r'(\$[\d,]+(?:\.\d{2})?(?:\s*-\s*\$[\d,]+(?:\.\d{2})?)?)'
        )
        # Orphaned tag: line ends with [TAG] and nothing after (page-break artifact)
        ORPHAN_TAG = re.compile(r'^(.*?)\[[A-Z]{2,3}\]\s*$')

        for i, line in enumerate(lines):
            if not line:
                continue

            # Detect entry into transaction table — must happen BEFORE skip check
            # because "ID OwnerAsset" and "$200?" both match the SKIP pattern
            if '$200?' in line or 'ID OwnerAsset' in line:
                in_tx        = True
                asset_lines  = []
                orphan_asset = ""
                continue

            if SKIP.match(line):
                continue
            if re.match(r'^\$[\d,]+(?:\.\d{2})?$', line):
                continue  # lone amount continuation

            next_line = lines[i + 1] if i + 1 < len(lines) else ""

            # Main pattern: line contains [TAG]S or [TAG] P followed by dates
            tag_m = TAG_TX.search(line)
            if tag_m and in_tx:
                before_tag = OWNER_RE.sub('', line[:tag_m.start()]).strip()
                # If we have a saved orphan asset from a page-break, prepend it
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

            # Orphaned [TAG] line (page break split asset from its tx line)
            # e.g. "(AVGO)  [ST]" or "Common Stock (COST)  [ST]" with no dates
            orp_m = ORPHAN_TAG.match(line)
            if orp_m:
                fragment = OWNER_RE.sub('', orp_m.group(1)).strip()
                # Special case: if last transaction was a no-tag fallback and this
                # orphan has the ticker for it, update that transaction in place
                if transactions and fragment:
                    ticker_m = TICKER_RE.search(fragment)
                    if ticker_m and transactions[-1].get("ticker") == "N/A":
                        transactions[-1]["ticker"] = ticker_m.group(1)
                        # Also improve asset name by appending the fragment
                        existing = transactions[-1].get("asset", "")
                        extra = re.sub(r'\[.*?\]', '', fragment)
                        extra = TICKER_RE.sub('', extra).strip()
                        if extra and extra not in existing:
                            transactions[-1]["asset"] = (existing + " " + extra).strip()[:60]
                        asset_lines = []
                        continue
                # Otherwise save as orphan for next transaction
                orphan_asset = ' '.join(asset_lines + ([fragment] if fragment else []))
                asset_lines  = []
                continue

            # Fallback: asset line that has dates but no [TAG] (other page-break variant)
            # e.g. "Broadcom Inc. - Common Stock P 05/05/2025 05/06/2025 $1,001 - $15,000"
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

            # Accumulate asset name lines
            if re.match(r'^[A-Z]\s{1,3}[A-Z](\s{1,3}[A-Z])?$', line):
                continue  # spaced letter boilerplate
            if OWNER_RE.match(line) and asset_lines:
                asset_lines = []  # new owner prefix = new asset entry
            clean = OWNER_RE.sub('', line).strip()
            if clean:
                asset_lines.append(clean)

    except Exception as e:
        log.error(f"PDF parse error for {pdf_url}: {e}")

    # Dedup — use a counter per key so same-ticker same-account duplicates are kept
    # (e.g. two IRA accounts both buying OXY same day same amount)
    from collections import Counter
    key_counts: Counter = Counter()
    unique = []
    for t in transactions:
        base_key = (t["ticker"], t["transaction_date"], t["transaction_type"], t["amount"])
        key_counts[base_key] += 1
        # Only keep first occurrence per key — true duplicates from PDF parsing artifacts
        if key_counts[base_key] == 1:
            unique.append(t)
        elif t["asset"] and t["asset"] != unique[-1].get("asset",""):
            # Different asset description = genuinely different holding, keep it
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

def check_all(seen: set) -> list[dict]:
    new_items = []

    for last_name, _ in WATCH_LIST:
        filings = fetch_house_filings(last_name)
        for f in filings:
            uid = f"filing_{f['doc_url']}"
            if uid not in seen:
                seen.add(uid)
                log.info(f"New filing: {f['name']} — {f['filing_type']} — parsing PDF...")
                transactions = parse_ptr_pdf(f["doc_url"])

                if transactions:
                    for tx in transactions:
                        new_items.append({
                            "name":         f["name"],
                            "filing_type":  f["filing_type"],
                            "doc_url":      f["doc_url"],
                            **tx,
                        })
                else:
                    # PDF parse failed — still notify with link
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

    return new_items

# ── Email ─────────────────────────────────────────────────────────────────────

def build_email_html(items: list[dict]) -> str:
    # Group by filing (doc_url) so we can handle large filings gracefully
    from itertools import groupby
    rows  = ""
    total = len(items)
    names = ", ".join(sorted({t["name"] for t in items}))
    MAX_ROWS_PER_FILING = 30  # Gmail clips at ~102KB; keep emails lean

    # Group consecutive items by doc_url
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

            rows += f"""
        <tr style="{row_border}">
          <td style="padding:9px 8px;">{name_cell}</td>
          <td style="padding:9px 8px;font-weight:600;color:{fg};background:{bg};text-align:center;">{tx_type}</td>
          <td style="padding:9px 8px;font-weight:600;">{tx.get('ticker','—')}</td>
          <td style="padding:9px 8px;font-size:12px;">{tx.get('asset','—')}</td>
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

    return f"""<html><body style="font-family:sans-serif;color:#222;max-width:960px;margin:auto;padding:24px;">
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
    msg   = MIMEMultipart("alternative")
    names = ", ".join(sorted({t["name"] for t in items}))
    msg["Subject"] = f"Congress Trade Alert — {len(items)} transaction(s) — {names}"
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT

    plain = "\n".join(
        f"{t['name']} | {t['transaction_type']} {t['ticker']} | {t['transaction_date']} | {t['amount']}"
        for t in items
    )
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(build_email_html(items), "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())
    log.info(f"Email sent — {len(items)} transaction(s)")

# ── Main loop ─────────────────────────────────────────────────────────────────

def run():
    names = [f"{ln} (House)" for ln, _ in WATCH_LIST]
    log.info(f"Congress Watcher started. Watching: {', '.join(names)}")
    log.info(f"Checking every {CHECK_INTERVAL_HOURS} hours.")

    while True:
        log.info("Running check...")
        seen      = load_seen()
        new_items = check_all(seen)

        if new_items:
            log.info(f"Found {len(new_items)} new transaction(s). Sending email...")
            try:
                send_email(new_items)
            except Exception as e:
                log.error(f"Failed to send email: {e}")
        else:
            log.info("No new filings found.")

        save_seen(seen)
        log.info(f"Sleeping {CHECK_INTERVAL_HOURS}h until next check...")
        time.sleep(CHECK_INTERVAL_HOURS * 3600)

if __name__ == "__main__":
    run()