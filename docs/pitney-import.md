---
name: pitney-to-db
description: >
  Converts a Pitney Bowes Business Manager "DM Weight Break by Account-Carrier-Class" .xls report
  into the standardized CSV format and imports it into the postage SQLite database. Use this skill
  whenever processing a BM_*.xls file, importing daily postage data, setting up the watch folder
  importer, or writing any code that touches the postage_imports or postage_data tables.
  Always read postage-db-structure first for the full schema context.
---

# Pitney Bowes XLS → Database Import

## Overview

Every business day a file arrives named `BM_{M}_{D}_{YY}.xls` (e.g. `BM_3_20_26.xls`).
This skill covers the full pipeline:

```
BM_3_20_26.xls
    │
    ▼  Step 1: Convert XLS → XLSX (LibreOffice)
    │
    ▼  Step 2: Parse XLSX → standardized CSV rows
    │
    ▼  Step 3: Write BM_3_20_26_report.csv
    │
    ▼  Step 4: Import CSV rows → postage.db
```

Steps 1–3 produce the canonical `BM_*_report.csv`. Step 4 loads it into the database.
Steps 1–4 can be run as one pipeline or independently (e.g. drop a pre-converted CSV directly).

---

## Step 1 — Convert .xls → .xlsx

openpyxl cannot read legacy `.xls`. Use LibreOffice headless:

```python
import subprocess, os

def convert_xls_to_xlsx(xls_path: str, out_dir: str = "/tmp") -> str:
    result = subprocess.run(
        ["libreoffice", "--headless", "--convert-to", "xlsx",
         "--outdir", out_dir, xls_path],
        capture_output=True, text=True, timeout=90
    )
    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice conversion failed: {result.stderr}")
    base = os.path.basename(xls_path).replace(".xls", ".xlsx")
    xlsx_path = os.path.join(out_dir, base)
    if not os.path.exists(xlsx_path):
        raise FileNotFoundError(f"Expected output not found: {xlsx_path}")
    return xlsx_path
```

If the input is already `.xlsx`, skip this step and load it directly.

---

## Step 2 — Parse the Report

The Pitney Bowes report has a fixed multi-column layout. Column letters **never change**
across date or account. The report repeats header rows every ~25 rows (page breaks) — these
must be skipped.

### Column Map

| Letter | Content |
|---|---|
| **A** | Account code (4-digit integer, e.g. `8393`) — appears once per account block |
| **G** | Class code (e.g. `1CA5DFlt`, `NOCLASS`) — appears once per class block |
| **J** | `Sub Total` label — rows with this value must be skipped entirely |
| **M** | Weight in oz (the data rows) |
| **P** | **Pieces — report date range** (NOT year-to-date) |
| **R** | **Total Cost — report date range** (NOT year-to-date) |

Columns W and AA are year-to-date Pieces and Total Cost — **ignore these completely**.

### State Machine Parser

Account codes and class codes carry forward across rows until a new value appears.
This means one account block spans many rows, and one class block spans many weight rows.

```python
from openpyxl import load_workbook
import csv, re, os

SKIP_A_PREFIXES = {
    'Pitney Bowes',
    'DM Weight Break',
    'Account Code',
    'Custom Field',
    'Report On Working Database',
    'Business Manager',
}

def parse_bm_xlsx(xlsx_path: str) -> list[dict]:
    """
    Parse a Pitney Bowes BM report XLSX.
    Returns list of dicts: {account_code, mail_class, weight_oz, pieces, total_cost}
    """
    wb = load_workbook(xlsx_path, read_only=True)
    ws = wb.active

    rows_out = []
    current_account = None
    current_class = None

    for row in ws.iter_rows(values_only=False):
        cells = {cell.column_letter: cell.value
                 for cell in row if cell.value is not None}

        a = str(cells.get('A', '') or '').strip()
        g = str(cells.get('G', '') or '').strip()
        j = str(cells.get('J', '') or '').strip()
        m = cells.get('M')
        p = cells.get('P')
        r = cells.get('R')

        # ── Skip header / footer lines ──────────────────────────────────────
        if a and any(a.startswith(s) for s in SKIP_A_PREFIXES):
            continue

        # ── Skip sub-total rows ─────────────────────────────────────────────
        if j == 'Sub Total':
            continue

        # ── New account block: 4-digit number in col A ──────────────────────
        if a and re.match(r'^\d{4}$', a):
            current_account = a
            current_class = None   # reset class when account changes
            continue

        # ── New class: col G has a value ────────────────────────────────────
        if g:
            current_class = g
            continue

        # ── Data row: col M has a numeric weight ────────────────────────────
        if m is not None and current_account is not None:
            try:
                weight = float(m)
                pieces = int(p) if p is not None else 0
                cost   = float(r) if r is not None else 0.0
                rows_out.append({
                    'account_code': current_account,
                    'mail_class':   current_class or 'UNKNOWN',
                    'weight_oz':    weight,
                    'pieces':       pieces,
                    'total_cost':   round(cost, 3),
                })
            except (ValueError, TypeError):
                pass  # skip malformed rows silently

    wb.close()
    return rows_out
```

**Edge cases handled:**
- Page break headers (repeating `Account Code`, `Weight (oz.)` headers every ~25 rows) → caught by `SKIP_A_PREFIXES`
- Split carrier names across two col-D rows → carrier column is not extracted, no impact
- Account code carry-forward across page breaks → state machine handles it
- `read_only=True` required for large files (10,000+ rows)

---

## Step 3 — Write the Standardized CSV

The output CSV is the canonical intermediate format. Name it by swapping `.xls` → `_report.csv`:

```python
def write_report_csv(rows: list[dict], xls_path: str, out_dir: str) -> str:
    base = os.path.basename(xls_path)
    # BM_3_20_26.xls → BM_3_20_26_report.csv
    csv_name = re.sub(r'\.xls[x]?$', '_report.csv', base, flags=re.IGNORECASE)
    out_path = os.path.join(out_dir, csv_name)

    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['Account Code', 'Class', 'Weight  (oz.)', 'Pieces', 'Total Cost'])
        for row in rows:
            writer.writerow([
                row['account_code'],
                row['mail_class'],
                row['weight_oz'],
                row['pieces'],
                f"{row['total_cost']:.3f}",
            ])

    return out_path
```

Note the **double space** in `Weight  (oz.)` — this matches the source format and must be
preserved exactly so downstream readers that key on the header name work correctly.

---

## Step 4 — Import CSV into the Database

### Date Parsing from Filename

```python
import re
from datetime import date

def parse_bm_date(filename: str) -> str:
    """
    BM_3_20_26_report.csv  →  '2026-03-20'
    BM_11_5_26_report.csv  →  '2026-11-05'
    BM_3_20_26.xls         →  '2026-03-20'
    """
    m = re.search(r'BM_(\d+)_(\d+)_(\d+)', filename)
    if not m:
        raise ValueError(f"Cannot parse date from filename: {filename}")
    month = int(m.group(1))
    day   = int(m.group(2))
    year  = 2000 + int(m.group(3))
    return date(year, month, day).isoformat()
```

### Leading Zero Strip

```python
def strip_zeros(value) -> int | None:
    try:
        return int(str(value).strip().lstrip('0') or '0')
    except (ValueError, TypeError):
        return None
```

### Full Import Function

```python
import sqlite3, csv, os

def import_bm_csv(csv_path: str, db_path: str) -> dict:
    """
    Import a BM_*_report.csv into postage.db.
    Returns: {'file_name': str, 'file_date': str, 'rows_imported': int,
               'unmatched': list[int]}
    """
    file_name = os.path.basename(csv_path)
    file_date = parse_bm_date(file_name)

    # Read all rows from CSV
    data_rows = []
    with open(csv_path, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            data_rows.append(row)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    # ── Deduplication ───────────────────────────────────────────────────────
    existing = cur.execute(
        "SELECT id FROM postage_imports WHERE file_name = ?", (file_name,)
    ).fetchone()
    if existing:
        cur.execute("DELETE FROM postage_imports WHERE file_name = ?", (file_name,))
        # CASCADE DELETE removes postage_data rows for this import

    # ── Create import record ─────────────────────────────────────────────────
    cur.execute(
        "INSERT INTO postage_imports (file_name, file_date, row_count) VALUES (?, ?, ?)",
        (file_name, file_date, len(data_rows))
    )
    import_id = cur.lastrowid

    # ── Load valid customer numbers for unmatched detection ──────────────────
    valid_accounts = {
        row[0] for row in cur.execute("SELECT customer_number FROM customers").fetchall()
    }

    # ── Insert rows ─────────────────────────────────────────────────────────
    unmatched = set()
    inserted = 0
    for row in data_rows:
        account_code = strip_zeros(row.get('Account Code', ''))
        if account_code is None:
            continue

        mail_class = str(row.get('Class', '')).strip()
        try:
            weight_oz  = float(row.get('Weight  (oz.)', 0) or 0)
            pieces     = int(row.get('Pieces', 0) or 0)
            total_cost = float(row.get('Total Cost', 0) or 0)
        except (ValueError, TypeError):
            continue

        is_unmatched = 0 if account_code in valid_accounts else 1
        if is_unmatched:
            unmatched.add(account_code)

        try:
            cur.execute("""
                INSERT OR REPLACE INTO postage_data
                    (import_id, file_date, account_code, mail_class,
                     weight_oz, pieces, total_cost, unmatched_account)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (import_id, file_date, account_code, mail_class,
                  weight_oz, pieces, total_cost, is_unmatched))
            inserted += 1
        except sqlite3.IntegrityError:
            pass  # duplicate within file — skip

    conn.commit()
    conn.close()

    return {
        'file_name':     file_name,
        'file_date':     file_date,
        'rows_imported': inserted,
        'unmatched':     sorted(unmatched),
    }
```

---

## Step 5 — Import the Flat Rate Cost Table

Run once on setup and whenever USPS rates change.

```python
import csv, sqlite3

def import_flat_rate_costs(csv_path: str, db_path: str):
    """Load Flatscostdata.csv into flat_rate_costs table (full replace)."""

    def parse_rate(val: str) -> float | None:
        try:
            return float(str(val).replace('$', '').strip())
        except (ValueError, TypeError):
            return None

    rows = []
    with open(csv_path, encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        # Normalize header names (strip whitespace and newlines)
        reader.fieldnames = [h.strip().replace('\n', ' ') for h in reader.fieldnames]
        for row in reader:
            rows.append({
                'weight_not_over_oz':   float(row['Weight Not Over (oz.)'].strip()),
                'rate_5digit':          parse_rate(row.get('5-Digit')),
                'rate_3digit':          parse_rate(row.get('3 digit')),
                'rate_aadc':            parse_rate(row.get('AADC')),
                'rate_mixed_adc':       parse_rate(row.get('Mixed ADC')),
                'rate_machinable_pres': parse_rate(row.get('Machinable Presorted')),
                'rate_retail':          parse_rate(row.get('Retail Cost')),
            })

    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM flat_rate_costs")
    conn.executemany("""
        INSERT INTO flat_rate_costs
            (weight_not_over_oz, rate_5digit, rate_3digit, rate_aadc,
             rate_mixed_adc, rate_machinable_pres, rate_retail)
        VALUES
            (:weight_not_over_oz, :rate_5digit, :rate_3digit, :rate_aadc,
             :rate_mixed_adc, :rate_machinable_pres, :rate_retail)
    """, rows)
    conn.commit()
    conn.close()
    print(f"Loaded {len(rows)} rate rows")
```

**Header name normalization:** The source CSV has `Machinable\nPresorted` (literal newline in
the header) and `Mixed ADC ` (trailing space). Strip both when reading.

---

## Complete Pipeline Function

```python
def process_bm_file(xls_path: str, db_path: str, csv_out_dir: str = None) -> dict:
    """
    Full pipeline: XLS → XLSX → parsed rows → CSV → database import.

    Args:
        xls_path:    Path to the incoming BM_*.xls file
        db_path:     Path to postage.db
        csv_out_dir: Where to save the _report.csv (defaults to same dir as xls)

    Returns: import result dict from import_bm_csv()
    """
    if csv_out_dir is None:
        csv_out_dir = os.path.dirname(xls_path)

    if xls_path.lower().endswith('.xls'):
        xlsx_path = convert_xls_to_xlsx(xls_path, out_dir='/tmp')
    else:
        xlsx_path = xls_path

    rows = parse_bm_xlsx(xlsx_path)
    csv_path = write_report_csv(rows, xls_path, csv_out_dir)
    result = import_bm_csv(csv_path, db_path)
    return result
```

---

## Watch Folder Handler

```python
import shutil, traceback, time
from datetime import date as dt_date
from pathlib import Path

def watch_loop(watch_dir: str, db_path: str):
    watch = Path(watch_dir)
    incoming  = watch / 'incoming'
    processed = watch / 'processed'
    failed    = watch / 'failed'
    log_file  = watch / 'watch.log'

    for d in [incoming, processed, failed]:
        d.mkdir(parents=True, exist_ok=True)

    while True:
        for f in sorted(incoming.glob('*.xls')) + sorted(incoming.glob('*.csv')):
            ts = time.strftime('%Y-%m-%d %H:%M:%S')
            try:
                # Determine file type
                if f.suffix.lower() == '.xls':
                    result = process_bm_file(str(f), db_path)
                elif 'billing' in f.name.lower() or 'export' in f.name.lower():
                    result = import_billing_csv(str(f), db_path)   # see parcel-billing-import skill
                else:
                    result = import_bm_csv(str(f), db_path)

                # Move to processed/YYYY-MM-DD/
                dest_dir = processed / dt_date.today().isoformat()
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(dest_dir / f.name))

                with open(log_file, 'a') as log:
                    log.write(f"{ts} | OK | {f.name} | {result.get('rows_imported',0)} rows\n")

            except Exception as e:
                # Move to failed/ and write .log
                shutil.move(str(f), str(failed / f.name))
                err_log = failed / (f.name + '.log')
                with open(err_log, 'w') as el:
                    el.write(f"Failed at {ts}\n{traceback.format_exc()}")
                with open(log_file, 'a') as log:
                    log.write(f"{ts} | FAIL | {f.name} | {e}\n")

        time.sleep(60)
```

---

## Post-Import Validation

Run after every import to confirm data quality:

```python
def validate_import(db_path: str, file_name: str):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    imp = cur.execute(
        "SELECT id, file_date, row_count FROM postage_imports WHERE file_name = ?",
        (file_name,)
    ).fetchone()
    print(f"Import: id={imp[0]}, date={imp[1]}, row_count={imp[2]}")

    actual = cur.execute(
        "SELECT COUNT(*) FROM postage_data WHERE import_id = ?", (imp[0],)
    ).fetchone()[0]
    print(f"Actual rows in postage_data: {actual}")

    unmatched = cur.execute(
        "SELECT account_code, SUM(pieces) FROM postage_data "
        "WHERE import_id = ? AND unmatched_account = 1 "
        "GROUP BY account_code ORDER BY 2 DESC",
        (imp[0],)
    ).fetchall()
    if unmatched:
        print(f"Unmatched accounts ({len(unmatched)}):", unmatched)
    else:
        print("No unmatched accounts")

    conn.close()
```

**Expected values for a typical daily file:**
- `row_count`: ~3,314
- `unmatched_account = 1`: ~17 account codes (the known system/test accounts)
