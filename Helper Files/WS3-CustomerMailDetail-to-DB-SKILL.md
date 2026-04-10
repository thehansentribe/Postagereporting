---
name: ws3-customer-mail-detail-to-db
description: >
  Parses NetSort "WS3_FCFL_CustomerMailDetail" XLS report files and loads
  the extracted data into a SQLite database. Use whenever the user uploads
  one or more WS3_FCFL_CustomerMailDetail*.xls files and asks to extract,
  import, process, or load the data. Also triggers for phrases like
  "process these mail detail files", "load the customer mail detail reports",
  or "put these into the database". Produces a SQLite .db file and a summary
  CSV of every rate-type row extracted.
---

# WS3 Customer Mail Detail → SQLite DB

Parses the NetSort "Customer Mail Detail" XLS report (one per mail run date)
and loads each rate-type line into a SQLite database ready for downstream
reporting and linking to other customer tables.

---

## Database Schema

```sql
-- One row per unique 6-digit customer code.
-- Populated on first encounter; never duplicated.
CREATE TABLE IF NOT EXISTS customers (
    customer_code TEXT PRIMARY KEY,   -- e.g. "301079"
    customer_name TEXT NOT NULL        -- e.g. "Blue Cross Blue Shield"
);

-- One row per XLS file processed (one file = one mail-run date).
CREATE TABLE IF NOT EXISTS mail_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    mail_date    TEXT NOT NULL,   -- YYYY-MM-DD derived from filename
    mail_id      TEXT,            -- e.g. "040626_F" from report header
    run_datetime TEXT             -- timestamp string from report header
);

-- One row per rate-type line in the report body.
-- pcs_rejected: col 75 for Single Piece; (num_pieces - pcs_accepted) for all others.
-- cost_per_piece: pre-calculated at insert time as ROUND(postage_applied / num_pieces, 4).
CREATE TABLE IF NOT EXISTS mail_detail (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES mail_runs(run_id),
    customer_code   TEXT    NOT NULL REFERENCES customers(customer_code),
    profile_name    TEXT    NOT NULL,  -- e.g. "301079 Blue Cross Blue Shield - EFD .970"
    rate_type       TEXT    NOT NULL,  -- "ThreeDigitAuto" | "ADC Auto" | "MXD ADC Auto" | "Single Piece"
    postage_claimed REAL,
    postage_applied REAL,
    num_pieces      INTEGER,
    pcs_accepted    INTEGER,
    pcs_rejected    INTEGER,
    cost_per_piece  REAL               -- pre-calculated: postage_applied / num_pieces
);
```

---

## Column Map (0-based index inside each spreadsheet row)

| Index | Field |
|-------|-------|
| 0 | Unused on customer header rows (empty) |
| 1 | Customer display name (e.g. "Blue Cross Blue Shield") — **index 1, not 0** |
| 7 | Profile line — "CCCCCC Profile Name" (6-digit code + full label) |
| 14 | Rate affix dollar amount (e.g. "$ 0.970") — customer header row only |
| 16 | Rate affix type ("Metered") — customer header row only |
| 19 | Rate Type (data row): ThreeDigitAuto / ADC Auto / MXD ADC Auto / Single Piece |
| 40 | Postage Claimed (data row) |
| 53 | Postage Applied (data row) |
| 66 | Number of Pieces (data row) |
| 70 | # of pcs Accepted (non-Single-Piece rows) |
| 75 | # of pcs Rejected (Single Piece rows only) |
| 95 | Total pcs Fed (data row) |

---

## Step 1 — Convert XLS → XLSX

openpyxl cannot read legacy `.xls` files. Use LibreOffice to convert first:

```python
import subprocess, os, re, sqlite3, glob, csv, shutil
from openpyxl import load_workbook

DB_PATH = "/home/claude/mail_detail.db"

def convert_xls(xls_path):
    """Convert legacy .xls to .xlsx via LibreOffice. Returns xlsx path."""
    subprocess.run(
        ["libreoffice", "--headless", "--convert-to", "xlsx",
         "--outdir", "/tmp/", xls_path],
        capture_output=True, text=True, timeout=60
    )
    return "/tmp/" + os.path.basename(xls_path).replace(".xls", ".xlsx")
```

---

## Step 2 — Parse the date from the filename

The filename pattern is `WS3_FCFL_CustomerMailDetail_M-D-YY.xls`.

```python
def parse_date_from_filename(path):
    """Return YYYY-MM-DD from filename like WS3_FCFL_CustomerMailDetail_4-6-26.xls"""
    m = re.search(r'(\d{1,2})-(\d{1,2})-(\d{2,4})\.xls', os.path.basename(path), re.IGNORECASE)
    if not m:
        return None
    month, day, year = m.group(1), m.group(2), m.group(3)
    if len(year) == 2:
        year = "20" + year
    return f"{year}-{int(month):02d}-{int(day):02d}"
```

---

## Step 3 — Parse the XLSX and extract rows

```python
SKIP_COL0 = {
    'Customer Mail Details', 'Name of Customer', 'Profile Name',
    'Report:', 'Entry:', 'Sort:', 'Date:'
}
RATE_TYPES   = {'ThreeDigitAuto', 'ADC Auto', 'MXD ADC Auto', 'Single Piece'}
TOTAL_LABELS = {'Profile Total', 'Customer Total'}

def parse_currency(val):
    """Strip the replacement char prefix and commas, return float or None."""
    if val is None:
        return None
    s = re.sub(r'^[^\d\-]+', '', str(val).strip()).replace(',', '')
    try:
        return float(s)
    except ValueError:
        return None

def parse_int(val):
    if val is None:
        return None
    try:
        return int(float(str(val).strip().replace(',', '')))
    except ValueError:
        return None

def calc_cost_per_piece(postage_applied, num_pieces):
    """Pre-calculate cost per piece. Returns None if num_pieces is 0 or None."""
    if not postage_applied or not num_pieces or num_pieces == 0:
        return None
    return round(postage_applied / num_pieces, 4)

def parse_xlsx(xlsx_path):
    """
    Returns:
        mail_id (str), run_datetime (str),
        customers (dict: code -> name),
        rows (list of dicts)
    """
    wb = load_workbook(xlsx_path, read_only=True)
    ws = wb.active

    mail_id = None
    run_datetime = None
    customers = {}
    rows = []

    current_customer_name = None
    current_customer_code = None
    current_profile_name  = None

    for raw_row in ws.iter_rows(values_only=True):
        c = raw_row

        def g(i):
            try:
                return c[i]
            except IndexError:
                return None

        col0  = str(g(0)  or '').strip()
        col1  = str(g(1)  or '').strip()   # customer name lives at index 1
        col5  = str(g(5)  or '').strip()
        col7  = str(g(7)  or '').strip()
        col19 = str(g(19) or '').strip()
        col21 = str(g(21) or '').strip()

        # ── Header metadata ──────────────────────────────────────────────
        if g(48) in ('Mail ID :', 'Mail ID:'):
            mail_id = str(g(57) or '').strip()
        if g(84) == 'Date:' and g(90):
            run_datetime = str(g(90)).strip()

        # ── Skip header/footer/total lines ───────────────────────────────
        if col5 == 'Customer Mail Details':
            continue
        if col0 in SKIP_COL0:
            continue
        if col21 in TOTAL_LABELS:
            continue

        # ── Customer name line: name @ col1, rate @ col14, "Metered" @ col16
        if col1 and str(g(14) or '').strip().startswith('$') and str(g(16) or '').strip() == 'Metered':
            current_customer_name = col1
            current_customer_code = None
            current_profile_name  = None
            continue

        # ── Profile line: col7 starts with 6-digit code ──────────────────
        if col7 and re.match(r'^\d{6}\s', col7):
            current_profile_name  = col7
            current_customer_code = re.match(r'^(\d{6})', col7).group(1)
            if current_customer_code and current_customer_name:
                customers.setdefault(current_customer_code, current_customer_name)
            continue

        # ── Rate-type data row ────────────────────────────────────────────
        if col19 in RATE_TYPES and current_customer_code:
            is_single       = (col19 == 'Single Piece')
            num_pieces      = parse_int(g(66))
            pcs_accepted    = parse_int(g(75)) if is_single else parse_int(g(70))
            # Single Piece: all pieces are rejects (col 75)
            # All other rate types: rejects = num_pieces - pcs_accepted
            if is_single:
                pcs_rejected = parse_int(g(75))
            else:
                pcs_rejected = (num_pieces - pcs_accepted) if (num_pieces is not None and pcs_accepted is not None) else None
            postage_applied = parse_currency(g(53))
            rows.append({
                'customer_code':   current_customer_code,
                'profile_name':    current_profile_name or '',
                'rate_type':       col19,
                'postage_claimed': parse_currency(g(40)),
                'postage_applied': postage_applied,
                'num_pieces':      num_pieces,
                'pcs_accepted':    pcs_accepted,
                'pcs_rejected':    pcs_rejected,
                'cost_per_piece':  calc_cost_per_piece(postage_applied, num_pieces),
            })

    wb.close()
    return mail_id, run_datetime, customers, rows
```

---

## Step 4 — Create DB and insert data

```python
def init_db(db_path):
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS customers (
            customer_code TEXT PRIMARY KEY,
            customer_name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mail_runs (
            run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            mail_date    TEXT NOT NULL,
            mail_id      TEXT,
            run_datetime TEXT
        );
        CREATE TABLE IF NOT EXISTS mail_detail (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          INTEGER NOT NULL REFERENCES mail_runs(run_id),
            customer_code   TEXT    NOT NULL REFERENCES customers(customer_code),
            profile_name    TEXT    NOT NULL,
            rate_type       TEXT    NOT NULL,
            postage_claimed REAL,
            postage_applied REAL,
            num_pieces      INTEGER,
            pcs_accepted    INTEGER,
            pcs_rejected    INTEGER,
            cost_per_piece  REAL
        );
    """)
    con.commit()
    return con

def load_file(con, xls_path):
    mail_date = parse_date_from_filename(xls_path)
    xlsx_path = convert_xls(xls_path)
    mail_id, run_dt, customers, rows = parse_xlsx(xlsx_path)

    # Upsert customers (first encounter wins for name)
    con.executemany(
        "INSERT OR IGNORE INTO customers (customer_code, customer_name) VALUES (?,?)",
        list(customers.items())
    )

    # Insert mail run
    cur = con.execute(
        "INSERT INTO mail_runs (mail_date, mail_id, run_datetime) VALUES (?,?,?)",
        (mail_date, mail_id, run_dt)
    )
    run_id = cur.lastrowid

    # Insert detail rows
    con.executemany(
        """INSERT INTO mail_detail
           (run_id, customer_code, profile_name, rate_type,
            postage_claimed, postage_applied,
            num_pieces, pcs_accepted, pcs_rejected, cost_per_piece)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [(run_id,
          r['customer_code'], r['profile_name'], r['rate_type'],
          r['postage_claimed'], r['postage_applied'],
          r['num_pieces'], r['pcs_accepted'], r['pcs_rejected'],
          r['cost_per_piece'])
         for r in rows]
    )
    con.commit()
    return run_id, len(rows)
```

---

## Step 5 — Main driver (process all uploaded files)

```python
xls_files = sorted(glob.glob("/mnt/user-data/uploads/WS3_FCFL_CustomerMailDetail*.xls"))

con = init_db(DB_PATH)

for f in xls_files:
    run_id, n = load_file(con, f)
    print(f"  {os.path.basename(f)} → run_id={run_id}, {n} rows")

# ── Write verification CSV ────────────────────────────────────────────────────
csv_out = "/mnt/user-data/outputs/mail_detail_export.csv"
with open(csv_out, 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['run_id','mail_date','mail_id','customer_code','customer_name',
                'profile_name','rate_type','postage_claimed','postage_applied',
                'num_pieces','pcs_accepted','pcs_rejected','cost_per_piece'])
    w.writerows(con.execute("""
        SELECT d.run_id, r.mail_date, r.mail_id,
               d.customer_code, c.customer_name,
               d.profile_name, d.rate_type,
               d.postage_claimed, d.postage_applied,
               d.num_pieces, d.pcs_accepted, d.pcs_rejected, d.cost_per_piece
        FROM mail_detail d
        JOIN mail_runs r ON r.run_id = d.run_id
        JOIN customers  c ON c.customer_code = d.customer_code
        ORDER BY r.mail_date, d.customer_code, d.id
    """).fetchall())

shutil.copy(DB_PATH, "/mnt/user-data/outputs/mail_detail.db")
con.close()
print(f"\nDB  → /mnt/user-data/outputs/mail_detail.db")
print(f"CSV → {csv_out}")
```

---

## Notes & Edge Cases

- **Multiple profiles per customer**: Some customers (e.g. Blue Cross Blue Shield / 301079)
  have several profile lines under the same customer block. Each profile line starts a
  new `current_profile_name`; rows are captured per-profile correctly.
- **Duplicate rate types per profile**: The same rate type (e.g. `ThreeDigitAuto`) can
  appear multiple times for one profile (different weight classes within the same rate).
  Each occurrence becomes its own `mail_detail` row — do not deduplicate.
- **Currency prefix character**: The `□` (Unicode replacement char) before postage amounts
  is stripped by `parse_currency()` using a regex that removes all leading non-digit chars.
- **`INSERT OR IGNORE` for customers**: Customer codes are stable across runs. The name
  from the first file processed wins; subsequent files with the same code are silently
  skipped. If names change over time, add an `UPDATE` step.
- **pcs_rejected logic**:
  - Single Piece rows: `pcs_rejected = col 75` (explicit reject count from report).
  - All other rate types: `pcs_rejected = num_pieces - pcs_accepted` (derived).
- **cost_per_piece**: Pre-calculated at insert as `ROUND(postage_applied / num_pieces, 4)`.
  Stored as `NULL` if `num_pieces` is 0 or NULL to avoid divide-by-zero.
- **LibreOffice must be installed**: Required for `.xls` → `.xlsx` conversion.
  Already available in the Claude computer environment.
- **DB is cumulative**: Re-running the script on the same files will insert duplicate
  `mail_runs` and `mail_detail` rows. Add a `UNIQUE` constraint on `mail_runs(mail_date, mail_id)`
  and use `INSERT OR IGNORE` if idempotency is needed.
