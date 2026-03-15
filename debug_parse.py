"""
Run: python debug_parse.py <pdf_url_or_path>
Shows exactly what the parser sees line by line.
"""
import io, re, sys, requests, pypdf

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

TAG_TX_F = re.compile(
    r'\[(?:ST|OT|OP|CS|DO|RE|CO)\]\s*'
    r'(S \(partial\)|P \(partial\)|S \(exchange\)|P \(exchange\)|[SP])\s+'
    r'(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}/\d{1,2}/\d{4})(.*)'
)
SKIP_F = re.compile(
    r'^(F\s*S:|S\s*O:|L\s*:|D\s*:|Filing Status|Subholding|Location|Description'
    r'|I CERTIFY|Digitally|Filing ID|\* For|\bYes\b.*\bNo\b|^\s*Yes$|^\s*No$'
    r'|Name:|Status:|State/District:|Clerk of|my knowledge'
    r'|ID\s*Owner\s*Asset|TypeDate|DateAmount|Gains\s*>|\$200\?'
    r'|Equitable Advisors)', re.I
)

arg = sys.argv[1]
if arg.startswith("http"):
    resp = requests.get(arg, headers=HEADERS, timeout=30)
    pdf = pypdf.PdfReader(io.BytesIO(resp.content))
else:
    pdf = pypdf.PdfReader(arg)

text = "\n".join(page.extract_text() or "" for page in pdf.pages)
text = text.replace('\x00', '')
lines = [l.strip() for l in text.split('\n')]

print("=== PARSER TRACE ===")
in_tx = False
for i, line in enumerate(lines):
    if not line: continue
    
    in_tx_trigger = '$200?' in line or 'ID OwnerAsset' in line
    skipped = bool(SKIP_F.match(line))
    lone_amt = bool(re.match(r'^\$[\d,]+(?:\.\d{2})?$', line))
    tag_match = bool(TAG_TX_F.search(line))
    
    if in_tx_trigger:
        in_tx = True
    
    status = []
    if in_tx_trigger: status.append("→IN_TX")
    if skipped:       status.append("SKIP")
    if lone_amt:      status.append("AMT_CONT")
    if tag_match:     status.append("★TX_MATCH")
    
    flag = " | ".join(status) if status else ("  active" if in_tx else "  header")
    print(f"{i:3}: [{flag}] {repr(line[:80])}")
