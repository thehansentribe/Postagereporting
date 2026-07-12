---
name: postage-db-structure
description: >
  Complete SQLite database schema and architecture guide for the postage reporting system.
  Use this skill whenever building, modifying, or querying the postage database — including
  creating tables, understanding foreign key relationships, importing any data source, writing
  reports, or explaining how the system fits together. This is the authoritative reference for
  the entire data model. Always read this before writing any database code for this project.
---

# Postage Reporting System — Database Architecture

## System Overview

Two files arrive daily and feed the database:

| File | Source | Content |
|---|---|---|
| `BM_{M}_{D}_{YY}.xls` | Pitney Bowes Business Manager | Daily postage by account, class, weight |
| `Copy_of_Export_Billing_{ID}.csv` | Postage machine billing export | Individual parcel/package records |

Supporting reference tables (loaded once, updated as rates change):
- **Customer hierarchy** — parent/child account structure
- **Flat mail cost table** — USPS presort rate tiers by weight
- **Parcel cost table** — to be populated when rates are available

The database file is named **`postage.db`** (SQLite).

---

## The Critical Link — Customer Number

**`customer_number`** is the integer key that connects everything.

| Table | Column | Notes |
|---|---|---|
| `customers` | `customer_number` | Primary key |
| `postage_data` | `account_code` | FK → customers |
| `billing_records` | `custom_account_code` | FK → customers (col N in source CSV) |

### Leading Zero Rule — APPLY EVERYWHERE

Source files store account codes with leading zeros. Strip them before any comparison or storage.

```python
def strip_zeros(value):
    """Convert '0986' → 986, '0001' → 1, '88' → 88"""
    try:
        return int(str(value).strip().lstrip('0') or '0')
    except (ValueError, TypeError):
        return None
```

This applies to:
- `Customer Code` in `Parent_Customer_.csv`
- `Parent Co Number` in `Parent_Customer_.csv`
- `Account Code` in `BM_*_report.csv` (approximately 317 of 3,314 rows have leading zeros)
- `Custom Account Code` (column N) in `Copy_of_Export_Billing_*.csv`

---

## Full Schema — All Tables

### Table: `customers`

Loaded from `Parent_Customer_.csv`. Full replace on each load (delete all, re-import).

```sql
CREATE TABLE IF NOT EXISTS customers (
    customer_number  INTEGER PRIMARY KEY,
    customer_name    TEXT    NOT NULL,
    parent_number    INTEGER,
    parent_name      TEXT,
    FOREIGN KEY (parent_number) REFERENCES customers(customer_number)
);

CREATE INDEX IF NOT EXISTS idx_customers_parent ON customers(parent_number);
```

**Column mapping from source CSV:**

| CSV Column | Header | DB Column | Transform |
|---|---|---|---|
| A | `Customer` | `customer_name` | store as-is |
| B | `Customer Code` | `customer_number` | strip leading zeros → INTEGER |
| C | `Parent Co` | `parent_name` | may be empty |
| D | `Parent Co Number` | `parent_number` | strip leading zeros → INTEGER, may be empty |

**Parent/child logic:**

| Condition | What to store |
|---|---|
| Col C empty AND Col D empty | Standalone — `parent_number = NULL`, `parent_name = NULL` |
| Col C has name AND Col D has number | Child — `parent_number = int(D)`, `parent_name = C` |
| Col C has name but Col D empty | Data error — log warning, store `parent_number = NULL`, `parent_name = C` |

**Expected after import:** 2,602 rows total, 2,154 with a parent, 448 standalone, 26 unique parent numbers.

**Validation queries (run after every customer import):**
```sql
SELECT COUNT(*) FROM customers;                                              -- expect 2602
SELECT COUNT(*) FROM customers WHERE parent_number IS NOT NULL;              -- expect 2154
SELECT COUNT(DISTINCT parent_number) FROM customers WHERE parent_number IS NOT NULL; -- expect 26
SELECT COUNT(*) FROM customers WHERE parent_number IS NULL AND parent_name IS NULL;  -- expect 448
SELECT COUNT(*) FROM customers WHERE parent_number IS NULL AND parent_name IS NOT NULL; -- expect 0
SELECT COUNT(*) FROM customers WHERE parent_number = customer_number;        -- expect 0
```

---

### Table: `postage_imports`

Tracks which daily Pitney Bowes files have been loaded. Used for deduplication.

```sql
CREATE TABLE IF NOT EXISTS postage_imports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name    TEXT    NOT NULL UNIQUE,
    file_date    DATE    NOT NULL,
    imported_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    row_count    INTEGER
);
```

| Column | Description |
|---|---|
| `file_name` | Original filename, e.g. `BM_3_20_26_report.csv` — deduplication key |
| `file_date` | Date parsed from filename as `YYYY-MM-DD` |
| `row_count` | Number of rows inserted (for verification) |

**Date parsing from filename:**
```python
# BM_3_20_26_report.csv  →  2026-03-20
# BM_11_5_26_report.csv  →  2026-11-05
import re
from datetime import date

def parse_bm_date(filename):
    m = re.search(r'BM_(\d+)_(\d+)_(\d+)', filename)
    if m:
        month, day, year_2d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year = 2000 + year_2d
        return date(year, month, day).isoformat()
    raise ValueError(f"Cannot parse date from filename: {filename}")
```

---

### Table: `postage_data`

One row per account + class + weight combination per daily file.

```sql
CREATE TABLE IF NOT EXISTS postage_data (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id          INTEGER NOT NULL,
    file_date          DATE    NOT NULL,
    account_code       INTEGER NOT NULL,
    mail_class         TEXT    NOT NULL,
    weight_oz          REAL    NOT NULL,
    pieces             INTEGER NOT NULL,
    total_cost         REAL    NOT NULL,
    unmatched_account  INTEGER DEFAULT 0,
    FOREIGN KEY (import_id) REFERENCES postage_imports(id) ON DELETE CASCADE,
    UNIQUE (import_id, account_code, mail_class, weight_oz)
);

CREATE INDEX IF NOT EXISTS idx_postage_date    ON postage_data(file_date);
CREATE INDEX IF NOT EXISTS idx_postage_account ON postage_data(account_code);
CREATE INDEX IF NOT EXISTS idx_postage_import  ON postage_data(import_id);
CREATE INDEX IF NOT EXISTS idx_postage_class   ON postage_data(mail_class);
```

| Column | Description |
|---|---|
| `import_id` | FK → postage_imports.id, CASCADE DELETE |
| `file_date` | Denormalized date — enables fast date filtering without a join |
| `account_code` | Customer number, leading zeros stripped |
| `mail_class` | Class code from source (e.g. `1CA5DFlt`, `NOCLASS`, `Priority`) |
| `weight_oz` | Weight break in ounces |
| `pieces` | Piece count — store even if 0 |
| `total_cost` | Total postage cost — store even if 0.00 |
| `unmatched_account` | 1 if account_code not found in customers table |

**Column mapping from `BM_*_report.csv`:**

| CSV Header | DB Column | Transform |
|---|---|---|
| `Account Code` | `account_code` | strip leading zeros → INTEGER |
| `Class` | `mail_class` | store as-is |
| `Weight  (oz.)` (double space) | `weight_oz` | REAL |
| `Pieces` | `pieces` | INTEGER |
| `Total Cost` | `total_cost` | REAL |

**Deduplication on import:**
```python
existing = cur.execute(
    "SELECT id FROM postage_imports WHERE file_name = ?", (file_name,)
).fetchone()
if existing:
    cur.execute("DELETE FROM postage_imports WHERE file_name = ?", (file_name,))
    # CASCADE DELETE removes postage_data rows automatically
```

---

### Table: `flat_rate_costs`

USPS presorted flat mail rates by weight tier. Loaded from `Flatscostdata.csv`.
Replace entire table on each load.

```sql
CREATE TABLE IF NOT EXISTS flat_rate_costs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    weight_not_over_oz   REAL    NOT NULL UNIQUE,
    rate_5digit          REAL,
    rate_3digit          REAL,
    rate_aadc            REAL,
    rate_mixed_adc       REAL,
    rate_machinable_pres REAL,
    rate_retail          REAL,
    effective_date       DATE,
    notes                TEXT
);
```

**Column mapping from `Flatscostdata.csv`:**

| CSV Header | DB Column | Notes |
|---|---|---|
| `Weight Not Over (oz.)` | `weight_not_over_oz` | REAL — the max oz for this tier |
| `5-Digit` | `rate_5digit` | strip `$`, convert to REAL |
| `3 digit` | `rate_3digit` | strip `$`, convert to REAL |
| `AADC` | `rate_aadc` | strip `$`, convert to REAL |
| `Mixed ADC` | `rate_mixed_adc` | strip `$`, trailing space in header |
| `Machinable\nPresorted` | `rate_machinable_pres` | header has newline |
| `Retail Cost` | `rate_retail` | REAL |

**Rate lookup logic (match class code → rate column):**

```python
CLASS_TO_RATE_COLUMN = {
    '1CA5DFlt':  'rate_5digit',       # First Class Flat, 5-Digit presort
    '1CA3DPcl':  'rate_3digit',       # First Class Parcel, 3-Digit presort
    '1CAAADCL':  'rate_aadc',         # First Class, AADC presort
    '1CMAADCL':  'rate_mixed_adc',    # First Class, Mixed AADC presort
    '1CNAPres':  'rate_machinable_pres', # Nonauto Presorted (Machinable)
    '1CSPiece':  'rate_retail',       # Single piece (retail / non-presorted)
    '1ClFlat':   'rate_retail',       # First Class Flat, retail
    '1stClNMLtr':'rate_retail',       # First Class Letter, retail
}
# Classes not in this map (Priority, NOCLASS, OtherCls, PkgS*, Int*) have no flat rate
```

**Rate lookup query:**
```sql
-- Get rate for a given class and weight
SELECT rate_5digit, rate_3digit, rate_aadc, rate_mixed_adc, rate_machinable_pres, rate_retail
FROM flat_rate_costs
WHERE weight_not_over_oz >= :weight_oz
ORDER BY weight_not_over_oz ASC
LIMIT 1;
-- Use the column matching the mail class (see CLASS_TO_RATE_COLUMN above)
```

**13 rows expected after import** (1 oz through 13 oz).

---

### Table: `parcel_costs`

Parcel/package rate table. Structure is defined and ready; data will be provided later.

```sql
CREATE TABLE IF NOT EXISTS parcel_costs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    service_type         TEXT    NOT NULL,
    weight_min_oz        REAL    NOT NULL,
    weight_max_oz        REAL    NOT NULL,
    zone                 TEXT,
    rate                 REAL,
    effective_date       DATE,
    notes                TEXT,
    UNIQUE (service_type, weight_min_oz, weight_max_oz, zone)
);

CREATE INDEX IF NOT EXISTS idx_parcel_costs_lookup
    ON parcel_costs(service_type, weight_min_oz, weight_max_oz, zone);
```

| Column | Description |
|---|---|
| `service_type` | e.g. `USPS_GROUND_ADVANTAGE`, `PRIORITY`, `PRIORITY_FLAT_RATE_BOX` |
| `weight_min_oz` | Lower bound (exclusive) of this weight tier |
| `weight_max_oz` | Upper bound (inclusive) of this weight tier |
| `zone` | USPS zone (1–9), or NULL if zone-independent (flat rate boxes) |
| `rate` | Cost in dollars |

---

### Table: `billing_imports`

Tracks which `Copy_of_Export_Billing_*.csv` files have been loaded.

```sql
CREATE TABLE IF NOT EXISTS billing_imports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    billing_id  TEXT    NOT NULL UNIQUE,
    file_name   TEXT    NOT NULL,
    imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    row_count   INTEGER
);
```

| Column | Description |
|---|---|
| `billing_id` | Value from `BillingID` column in the CSV (e.g. `3784`, `3785`) — deduplication key |
| `file_name` | Original filename |
| `row_count` | Rows imported |

---

### Table: `billing_records`

One row per mail piece from the parcel billing export. Column N (`Custom Account Code`) is the customer link.

```sql
CREATE TABLE IF NOT EXISTS billing_records (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    billing_import_id           INTEGER NOT NULL,
    -- Customer link
    custom_account_code         INTEGER,           -- Col N, stripped leading zeros
    account_name                TEXT,              -- Col M: AccountName
    -- Piece identification
    piece_id                    TEXT,              -- Col A
    machine_serial              TEXT,              -- Col B
    time_stamp                  TEXT,              -- Col C: "M/D/YYYY HH:MM"
    -- Mail attributes
    weight_oz                   REAL,              -- Col D: Weight
    handling_type               TEXT,              -- Col E
    usps_mail_class             TEXT,              -- Col F
    usps_mail_prep_type         TEXT,              -- Col G
    routing_category            TEXT,              -- Col H
    routing_string              TEXT,              -- Col I
    zone                        TEXT,              -- Col AC
    -- Financial
    piece_postage               REAL,              -- Col T
    lbs_postage                 REAL,              -- Col U
    final_postage               REAL,              -- Col V
    fully_paid_postage          REAL,              -- Col W: base/retail rate
    billing_amount              REAL,              -- Col X: actual charged
    surcharge_postage           REAL,              -- Col A{85}
    postal_discounts            REAL,              -- Col A{88}
    -- Routing/bundling
    bundle_qualification        TEXT,              -- Col J
    bundle_zip                  TEXT,              -- Col K
    sack_level                  TEXT,              -- Col Z
    sack_zip                    TEXT,              -- Col AA
    destination_entry_level     TEXT,              -- Col AB
    -- Dimensions
    length_in                   REAL,              -- Col AL
    width_in                    REAL,              -- Col AM
    height_in                   REAL,              -- Col AN
    girth_in                    REAL,              -- Col AO (note: BillingID is col AO by header)
    -- Department/account detail
    account_id                  TEXT,              -- Col L: internal machine ID
    department_id               TEXT,              -- Col Q
    department_name             TEXT,              -- Col R
    manifest_id                 TEXT,              -- Col S
    -- Tracking
    imb_tracking_code           TEXT,              -- Col Y
    impb                        TEXT,              -- Col Ay (83)
    efn                         TEXT,              -- Col Az (84)
    -- Job/permit
    job_name                    TEXT,              -- Col An (72)
    billing_id_ref              TEXT,              -- Col Ao (73): BillingID value
    permit_origin               TEXT,              -- Col Ap (74)
    permit_number               TEXT,              -- Col Aq (75)
    permit_name                 TEXT,              -- Col Ar (76)
    -- Physical flags
    irregular                   TEXT,              -- Col AD
    is_flat_rate_conversion     TEXT,              -- Col AP
    nonrectangular              TEXT,              -- Col AQ
    sub_type                    TEXT,              -- Col AR
    -- Address (destination)
    hr_address                  TEXT,              -- Col A{89}
    hr_city                     TEXT,              -- Col A{90}
    hr_state                    TEXT,              -- Col A{91}
    hr_zip                      TEXT,              -- Col A{92}
    -- Misc
    master_mail_class           TEXT,              -- Col AW
    payment_method              TEXT,              -- Col Av (80)
    is_open_and_distribute      TEXT,              -- Col Au (79)
    customer_barcode_symbology  TEXT,              -- Col O
    customer_barcode            TEXT,              -- Col P
    custom1                     TEXT,              -- Col AE
    custom2                     TEXT,              -- Col AF
    -- Metadata
    unmatched_account           INTEGER DEFAULT 0,
    FOREIGN KEY (billing_import_id) REFERENCES billing_imports(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_billing_import    ON billing_records(billing_import_id);
CREATE INDEX IF NOT EXISTS idx_billing_account   ON billing_records(custom_account_code);
CREATE INDEX IF NOT EXISTS idx_billing_timestamp ON billing_records(time_stamp);
CREATE INDEX IF NOT EXISTS idx_billing_zone      ON billing_records(zone);
CREATE INDEX IF NOT EXISTS idx_billing_class     ON billing_records(usps_mail_class);
```

---

## Key Relationships (ERD Summary)

```
customers (customer_number PK)
    ├── parent_number FK → customers.customer_number   [self-referential]
    ├── postage_data.account_code FK → customer_number
    └── billing_records.custom_account_code FK → customer_number

postage_imports (id PK)
    └── postage_data.import_id FK → postage_imports.id  [CASCADE DELETE]

billing_imports (id PK)
    └── billing_records.billing_import_id FK → billing_imports.id  [CASCADE DELETE]

flat_rate_costs (weight_not_over_oz)
    └── referenced by application logic using mail_class → rate_column mapping

parcel_costs (service_type + weight range + zone)
    └── referenced by application logic for parcel rate lookups
```

---

## Core Reporting Queries

### Postage summary by parent → child → class → weight for a date range

```sql
SELECT
    p.file_date,
    COALESCE(c.parent_name, c.customer_name)       AS parent_name,
    COALESCE(c.parent_number, c.customer_number)   AS parent_number,
    c.customer_name,
    c.customer_number,
    p.mail_class,
    p.weight_oz,
    p.pieces,
    p.total_cost
FROM postage_data p
JOIN customers c ON p.account_code = c.customer_number
WHERE p.file_date BETWEEN :start_date AND :end_date
  AND p.pieces > 0
ORDER BY p.file_date, parent_name, c.customer_name, p.mail_class, p.weight_oz;
```

### Parcel summary by parent → child → zone → weight bucket

```sql
-- Weight bucket: ceil(weight_oz / 16.0), cap at 11 for "10+ lbs" display
SELECT
    COALESCE(c.parent_name, c.customer_name)         AS parent_name,
    COALESCE(c.parent_number, c.customer_number)     AS parent_number,
    c.customer_name,
    c.customer_number,
    br.usps_mail_class,
    br.zone,
    MIN(CAST(CEIL(br.weight_oz / 16.0) AS INTEGER), 11) AS weight_bucket_lb,
    COUNT(*)                                          AS piece_count,
    SUM(br.billing_amount)                            AS total_billed,
    SUM(br.fully_paid_postage)                        AS total_retail
FROM billing_records br
LEFT JOIN customers c ON br.custom_account_code = c.customer_number
WHERE br.weight_oz IS NOT NULL
  AND br.weight_oz > 0
  -- date filter: apply to br.time_stamp parsed in app layer, or use:
  -- AND date(substr(br.time_stamp,-4)||'-'||printf('%02d',...)) BETWEEN :start AND :end
GROUP BY parent_name, c.customer_name, br.usps_mail_class, br.zone, weight_bucket_lb
ORDER BY parent_name, c.customer_name, br.usps_mail_class, br.zone, weight_bucket_lb;
```

### Rate lookup — what the presort rate should have been

```sql
SELECT
    p.file_date,
    p.account_code,
    p.mail_class,
    p.weight_oz,
    p.pieces,
    p.total_cost                              AS actual_cost,
    p.pieces * frc.rate_5digit                AS expected_5digit_cost,
    p.pieces * frc.rate_retail                AS retail_cost
FROM postage_data p
LEFT JOIN flat_rate_costs frc
    ON frc.weight_not_over_oz = (
        SELECT MIN(weight_not_over_oz)
        FROM flat_rate_costs
        WHERE weight_not_over_oz >= p.weight_oz
    )
WHERE p.mail_class IN ('1CA5DFlt','1CA3DPcl','1CAAADCL','1CMAADCL','1CNAPres','1CSPiece')
  AND p.pieces > 0;
```

---

## Daily Import File Naming Conventions

| Source | Pattern | Example | Date |
|---|---|---|---|
| Pitney Bowes raw | `BM_{M}_{D}_{YY}.xls` | `BM_3_20_26.xls` | 2026-03-20 |
| Pitney Bowes converted | `BM_{M}_{D}_{YY}_report.csv` | `BM_3_20_26_report.csv` | 2026-03-20 |
| Parcel billing | `Copy_of_Export_Billing_{ID}.csv` | `Copy_of_Export_Billing_3785.csv` | from BillingID col |

---

## Watch Folder Layout

```
project_root/
├── postage.db
├── watch/
│   ├── incoming/          ← drop BM_*.xls and Export_Billing_*.csv here
│   ├── processed/
│   │   └── YYYY-MM-DD/    ← successfully imported files moved here
│   ├── failed/            ← failed imports moved here
│   │   └── filename.log   ← error traceback alongside failed file
│   └── watch.log          ← one-line status per file processed
```

---

## Known Unmatched Accounts

These 17 account codes appear in postage data but have no customer record.
Store their rows with `unmatched_account = 1` — never discard them.

```
1, 11, 111, 86, 88, 605, 703, 738, 959,
3472, 3561, 5182, 5341, 5543, 6220, 8297, 8298
```

Note: `88` (Hallmark) appears here because Hallmark IS in the customers table as a standalone
parent account, but some postage rows reference `88` directly. Verify before treating as unmatched.

---

## Database Initialization Script

Run once to create all tables:

```python
import sqlite3

def init_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript("""
        -- (paste all CREATE TABLE and CREATE INDEX statements from above)
    """)
    conn.commit()
    conn.close()
```

Always enable `PRAGMA foreign_keys = ON` at the start of every connection.
Use `PRAGMA journal_mode = WAL` for better concurrent read performance.
