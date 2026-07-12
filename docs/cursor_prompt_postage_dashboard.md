# Postage Reporting Dashboard — Full Build Specification

Build a complete postage reporting web application using **Python (Flask) + SQLite + vanilla HTML/CSS/JS**. Do not use React or any JS framework. Read the `postage-db-structure`, `pitney-to-db`, and `parcel-billing-import` skills before writing any code — they are the authoritative reference for the database schema, import logic, and all data rules.

---

## Tech Stack

- **Backend:** Python 3.11+, Flask, sqlite3 (stdlib only — no SQLAlchemy)
- **Database:** SQLite at `postage.db` in the project root
- **Frontend:** Single HTML file served by Flask, vanilla JS, CSS custom properties for theming
- **File processing:** LibreOffice (headless) for XLS conversion, stdlib `csv` for parsing
- **No npm, no node, no React, no external JS frameworks**

---

## Project Structure

```
project_root/
├── app.py                  ← Flask application, all routes
├── db.py                   ← Database init, all query functions
├── importer.py             ← All file import logic (postage + billing)
├── watcher.py              ← Watch folder polling service
├── postage.db              ← SQLite database (created on first run)
├── templates/
│   └── index.html          ← Single-page app template
├── static/
│   ├── style.css
│   └── app.js
└── watch/
    ├── incoming/           ← Drop files here
    ├── processed/
    │   └── YYYY-MM-DD/
    ├── failed/
    └── watch.log
```

---

## Database — Initialize on Startup

On `app.py` startup, call `db.init_db()` which runs `PRAGMA foreign_keys = ON`, `PRAGMA journal_mode = WAL`, and creates all tables if they don't exist.

Create these tables exactly as defined in the `postage-db-structure` skill:
- `customers` — parent/child customer hierarchy
- `postage_imports` — daily postage file tracking
- `postage_data` — daily weight-break postage rows
- `flat_rate_costs` — USPS flat mail rate table
- `parcel_costs` — parcel rate table (structure only, data TBD)
- `billing_imports` — parcel billing file tracking
- `billing_records` — individual parcel piece records

The customer_number / account_code / custom_account_code integer link between all tables is described fully in the skill. Strip leading zeros on every import.

---

## Customer Terminology

- **Parent account:** A customer whose `customer_number` is referenced as `parent_number` by other customers. These are the "group" accounts (e.g., "Blue Cross Blue Shield -EFD", "Hallmark", "State of Kansas").
- **Child account:** A customer that has a `parent_number` set — they belong to a parent group.
- **Main account / Standalone:** A customer with `parent_number IS NULL AND parent_name IS NULL` — they have no parent and no children. These are independent accounts.
- Some parent accounts also appear as their own customer row (they are both parent and a customer).

---

## File Importer (`importer.py`)

### Postage files — `BM_*.xls` and `BM_*_report.csv`

Follow the `pitney-to-db` skill exactly.

- Accept `.xls` files: convert to `.xlsx` with LibreOffice, then parse with openpyxl
- Accept `_report.csv` files directly (pre-converted)
- Parse date from filename: `BM_3_20_26.xls` → `2026-03-20`
- Strip leading zeros from Account Code
- Deduplicate by `file_name` in `postage_imports` (delete + re-import if already exists)
- Set `unmatched_account = 1` for any account_code not found in customers table
- Store ALL rows including zero-piece rows

### Billing / Parcel files — `Copy_of_Export_Billing_*.csv`

Follow the `parcel-billing-import` skill exactly.

- Open with `utf-8-sig` encoding
- Strip all header whitespace
- Column N (`Custom Account Code`, index 13) is the customer link — strip leading zeros
- Deduplicate by `BillingID` column value (delete + re-import if already exists)
- Set `unmatched_account = 1` for any unmatched custom_account_code

### Customer file — `Parent_Customer_.csv`

- Full replace (delete all, re-import)
- Strip leading zeros from Customer Code and Parent Co Number
- Handle three cases: standalone (no parent), child (name + number), data error (name without number — log warning)
- Expected: 2,602 rows, 2,154 with parent, 448 standalone, 26 unique parents

### Flat rate cost file — `Flatscostdata.csv`

- Full replace
- Normalize headers (strip whitespace and newlines from header names)
- Strip `$` from currency values before converting to REAL

---

## Watch Folder (`watcher.py`)

Poll `watch/incoming/` every 60 seconds. Detect file type by name pattern:

| Pattern | Import type |
|---|---|
| `BM_*.xls` | Pitney XLS → convert → postage import |
| `BM_*_report.csv` | Postage CSV direct import |
| `*Export_Billing*.csv` or `*billing*.csv` (case-insensitive) | Billing/parcel import |
| `Parent_Customer*.csv` | Customer file import |
| `Flatscostdata*.csv` | Flat rate cost import |

On success: move file to `watch/processed/YYYY-MM-DD/filename`. Write one line to `watch/watch.log`: `{timestamp} | OK | {filename} | {row_count} rows`.

On failure: move file to `watch/failed/`. Write `{filename}.log` with full traceback. Write failure line to `watch/watch.log`.

One bad file must never stop the loop. Wrap each file in its own try/except.

The watcher runs as a background daemon thread started when Flask starts — not a separate process. Use `threading.Thread(target=watch_loop, daemon=True)`.

Expose a `/api/watcher/status` endpoint that returns `{"active": true/false, "last_scan": "timestamp", "last_log_lines": [...last 10 lines of watch.log...]}`.

---

## Flask API Routes

```
GET  /                          → serve index.html
GET  /api/customers             → list of parent accounts for dropdown
GET  /api/postage               → postage weight-break data (query params below)
GET  /api/parcels               → parcel weight-break data (query params below)
GET  /api/summary               → import summary (customer + class totals)
GET  /api/watcher/status        → watcher status + last 10 log lines
POST /api/scan                  → trigger immediate scan of watch/incoming/
POST /api/import/customers      → import customer CSV (multipart upload)
POST /api/import/flatrates      → import flat rate cost CSV
```

### `/api/customers` response

```json
[
  {"customer_number": 3901, "customer_name": "Blue Cross Blue Shield -EFD", "child_count": 105},
  {"customer_number": 730, "customer_name": "City of Kansas City Missouri", "child_count": 337},
  ...
]
```

Sort alphabetically by customer_name. Include "All Accounts" as the first option (return `null` for customer_number).

### `/api/postage` query parameters

| Param | Type | Description |
|---|---|---|
| `start_date` | YYYY-MM-DD | Required |
| `end_date` | YYYY-MM-DD | Required |
| `parent_number` | integer | Filter to one parent group (optional) |
| `customer_number` | integer | Filter to one specific child account (optional) |
| `show_parents` | bool | Include parent-level accounts in results |
| `show_main` | bool | Include standalone main accounts |
| `consolidate` | bool | Collapse date — sum all dates into one row per child+class |
| `remove_zeros` | bool | Exclude rows where ALL oz columns are 0 |
| `hide_costs` | bool | Omit total_cost from response |

### `/api/postage` response structure

```json
{
  "total_records": 79,
  "total_pieces": 517,
  "total_cost": 1161.29,
  "rows": [
    {
      "date": "2026-04-02",
      "parent_name": "Blue Cross Blue Shield -EFD",
      "parent_number": 3901,
      "child_name": "2240--BCBS",
      "child_number": 2169,
      "mail_class": "1CA5DFlt",
      "oz_0": 0, "oz_1": 0, "oz_2": 2, "oz_3": 2, "oz_4": 0,
      "oz_5": 0, "oz_6": 1, "oz_7": 0, "oz_8": 0, "oz_9": 0,
      "oz_10": 0, "oz_11": 0, "oz_12": 0, "oz_13": 0, "oz_13plus": 0,
      "total_qty": 5,
      "total_cost": 7.84
    }
  ]
}
```

**Postage query logic:**

```sql
SELECT
    p.file_date,
    COALESCE(c.parent_name, c.customer_name) AS parent_name,
    COALESCE(c.parent_number, c.customer_number) AS parent_number,
    c.customer_name AS child_name,
    c.customer_number AS child_number,
    p.mail_class,
    SUM(CASE WHEN p.weight_oz = 0  THEN p.pieces ELSE 0 END) AS oz_0,
    SUM(CASE WHEN p.weight_oz = 1  THEN p.pieces ELSE 0 END) AS oz_1,
    SUM(CASE WHEN p.weight_oz = 2  THEN p.pieces ELSE 0 END) AS oz_2,
    SUM(CASE WHEN p.weight_oz = 3  THEN p.pieces ELSE 0 END) AS oz_3,
    SUM(CASE WHEN p.weight_oz = 4  THEN p.pieces ELSE 0 END) AS oz_4,
    SUM(CASE WHEN p.weight_oz = 5  THEN p.pieces ELSE 0 END) AS oz_5,
    SUM(CASE WHEN p.weight_oz = 6  THEN p.pieces ELSE 0 END) AS oz_6,
    SUM(CASE WHEN p.weight_oz = 7  THEN p.pieces ELSE 0 END) AS oz_7,
    SUM(CASE WHEN p.weight_oz = 8  THEN p.pieces ELSE 0 END) AS oz_8,
    SUM(CASE WHEN p.weight_oz = 9  THEN p.pieces ELSE 0 END) AS oz_9,
    SUM(CASE WHEN p.weight_oz = 10 THEN p.pieces ELSE 0 END) AS oz_10,
    SUM(CASE WHEN p.weight_oz = 11 THEN p.pieces ELSE 0 END) AS oz_11,
    SUM(CASE WHEN p.weight_oz = 12 THEN p.pieces ELSE 0 END) AS oz_12,
    SUM(CASE WHEN p.weight_oz = 13 THEN p.pieces ELSE 0 END) AS oz_13,
    SUM(CASE WHEN p.weight_oz > 13 THEN p.pieces ELSE 0 END) AS oz_13plus,
    SUM(p.pieces) AS total_qty,
    SUM(p.total_cost) AS total_cost
FROM postage_data p
JOIN customers c ON p.account_code = c.customer_number
WHERE p.file_date BETWEEN :start_date AND :end_date
  -- conditionally add: AND c.parent_number = :parent_number
  -- conditionally add: AND c.customer_number = :customer_number
GROUP BY p.file_date, c.customer_number, p.mail_class
  -- if consolidate=true: GROUP BY c.customer_number, p.mail_class  (omit date)
ORDER BY p.file_date, parent_name, c.customer_name, p.mail_class
```

### `/api/parcels` query parameters

Same as `/api/postage` but returns pound-bucket data from `billing_records`.

### `/api/parcels` response structure

```json
{
  "total_records": 0,
  "total_pieces": 0,
  "total_billed": 0.00,
  "total_retail": 0.00,
  "rows": [
    {
      "date": "2026-04-01",
      "parent_name": "Blue Cross Blue Shield -EFD",
      "parent_number": 3901,
      "child_name": "Bay Area Air Quality",
      "child_number": 3835,
      "mail_class": "USPS GROUND ADVANTAGE",
      "zone": "7",
      "lb_1": 0, "lb_2": 1, "lb_3": 0, "lb_4": 0, "lb_5": 0,
      "lb_6": 0, "lb_7": 0, "lb_8": 0, "lb_9": 0, "lb_10": 0,
      "lb_10plus": 0,
      "total_qty": 1,
      "total_billed": 9.20,
      "total_retail": 15.25
    }
  ]
}
```

**Parcel weight bucket rule (CRITICAL):**
- Convert `weight_oz` to lbs: `lbs = weight_oz / 16.0`
- Round UP to next whole pound using ceiling: `math.ceil(lbs)`
- 16 oz → ceil(1.0) = 1 → `lb_1`
- 17 oz → ceil(1.0625) = 2 → `lb_2`
- 160 oz → ceil(10.0) = 10 → `lb_10`
- 161 oz → ceil(10.0625) = 11 → `lb_10plus`
- Any ceiled value ≥ 11 → `lb_10plus`
- Exclude rows where `weight_oz IS NULL OR weight_oz <= 0`

**Parcel query logic — parse date from `time_stamp` column:**

The `billing_records.time_stamp` is stored as `M/D/YYYY HH:MM` text. Filter by date in Python after fetching, OR use this SQLite expression to compare dates:

```sql
-- In SQLite, extract date from 'M/D/YYYY HH:MM' format:
WHERE date(
    substr(time_stamp, -4, 4) || '-' ||
    printf('%02d', CAST(substr(time_stamp, 1, instr(time_stamp,'/')-1) AS INT)) || '-' ||
    printf('%02d', CAST(substr(substr(time_stamp, instr(time_stamp,'/')+1), 1,
        instr(substr(time_stamp, instr(time_stamp,'/')+1),'/')-1) AS INT))
) BETWEEN :start_date AND :end_date
```

Then pivot the weight buckets using CASE/SUM in Python (fetch raw data, compute buckets in a loop).

### `/api/summary` response

```json
{
  "date_range": {"start": "2026-03-29", "end": "2026-04-05"},
  "postage": {
    "total_pieces": 118155,
    "total_cost": 75099.63,
    "by_customer": [
      {"customer_number": 88, "customer_name": "Hallmark", "pieces": 389, "cost": 262.56},
      ...
    ],
    "by_class": [
      {"mail_class": "NOCLASS", "pieces": 66494, "cost": 12278.01},
      {"mail_class": "1CNAPres", "pieces": 33506, "cost": 22985.12},
      ...
    ]
  },
  "parcels": {
    "total_pieces": 0,
    "total_billed": 0.00,
    "by_customer": [],
    "by_class": []
  },
  "imports": [
    {"file_name": "BM_3_29_26_report.csv", "file_date": "2026-03-29", "row_count": 3314, "imported_at": "..."},
    ...
  ]
}
```

---

## Frontend — `templates/index.html`

Single HTML page. All data loaded via `fetch()` calls to the Flask API. No page reloads.

### Color Theme

```css
:root {
  --color-primary: #7c3aed;        /* purple — buttons, active tab, accents */
  --color-primary-light: #ede9fe;  /* lavender — control panel background */
  --color-primary-mid: #a855f7;    /* medium purple — headings, icons */
  --color-summary-bg: #f0fdf4;     /* light green — summary bar background */
  --color-summary-text: #16a34a;   /* green — summary numbers */
  --color-tab-active: #7c3aed;     /* purple — active tab */
  --color-tab-inactive: #e5e7eb;   /* gray — inactive tabs */
  --color-watcher-active: #16a34a;
  --color-watcher-inactive: #ef4444;
  --color-table-header: #f9fafb;
  --color-table-border: #e5e7eb;
  --color-table-row-alt: #fafafa;
}
```

### Page Layout

```
┌─────────────────────────────────────────────┐
│  📊  Postage Dashboard                       │ ← header, purple h1
├─────────────────────────────────────────────┤
│ 🔴 Watcher Inactive          [🔄 Scan Now]  │ ← watcher bar (lavender bg)
├─────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────┐│
│  │  [🔍 Search Customer #]  [Select Account ▼] ││ ← control panel (lavender border + bg)
│  │  [📅 Start Date ____]    [📅 End Date ____] ││
│  │  ☑ Show Parent Accounts  ☐ Show Main Accounts  ☐ Consolidate Date & Mailclass ││
│  │  ☐ Remove Zero Count Rows  ☐ Hide Costs      [▶ Load All] ││
│  └─────────────────────────────────────────┘│
├─────────────────────────────────────────────┤
│ [📮 Postage] [📦 Parcels] [📋 Import Summary]│ ← tab bar
├─────────────────────────────────────────────┤
│  [summary bar: Total Records | Total Pieces | Total Cost]   │
│  [data table — horizontally scrollable]      │
└─────────────────────────────────────────────┘
```

### Controls Panel

**Select Account dropdown** — populated from `/api/customers`. Options format:
- "All Accounts" (value: "")
- "Blue Cross Blue Shield -EFD (3901)"
- "City of Kansas City Missouri (730)"
- etc. — sorted alphabetically

**Search Customer #** — text input. When a number is typed, it filters the dropdown to find and select that customer, OR passes `customer_number` directly to the API query. Typing a number and pressing Enter loads data for just that customer.

**Start Date / End Date** — `<input type="date">`. Default start = 30 days before today, default end = today.

**Checkboxes:**
- `Show Parent Accounts` — checked by default. When checked, include parent-level rows (accounts that ARE parents). When unchecked, hide rows where the account IS the parent itself (only show children).
- `Show Main Accounts` — unchecked by default. When checked, include standalone accounts that have no parent and no children.
- `Consolidate Date & Mailclass` — unchecked by default. When checked, collapse all dates into a single row per child account + mail class (sum all weight columns across the date range).
- `Remove Zero Count Rows` — unchecked by default. When checked, exclude rows where every single oz/lb bucket is 0.
- `Hide Costs` — unchecked by default. When checked, hide the Total Cost column from the table.

**Load All button** — triggers the data fetch with current filter settings.

### Watcher Status Bar

```html
<div class="watcher-bar">
  <span class="watcher-dot"></span>   <!-- green or red pulsing dot -->
  <span class="watcher-label">Watcher Active</span>   <!-- or "Watcher Inactive" -->
  <button class="btn-scan">🔄 Scan Now</button>
</div>
```

Poll `/api/watcher/status` every 30 seconds to update. "Scan Now" button calls `POST /api/scan` and then immediately re-fetches the current tab data.

---

## Tab 1 — Postage

### Summary Bar (above table)

Green background bar showing three stats:
```
Total Records: 79    Total Pieces: 517    Total Cost: $1,161.29
```
These come from the `/api/postage` response top-level fields.

### Data Table

**Fixed columns (frozen — do not scroll):**

| Column | Width | Notes |
|---|---|---|
| Date | 90px | `YYYY-MM-DD` format. If `consolidate=true`, show "Combined" |
| Parent Name | 140px | Parent account name |
| Parent # | 70px | Parent account number |
| Child Name | 140px | Child account name |
| Child # | 70px | Child account number |
| Class | 100px | Mail class code |

**Scrolling columns (horizontal scroll):**

| Column | Width | Notes |
|---|---|---|
| 0 oz | 55px | Piece count |
| 1 oz | 55px | |
| 2 oz | 55px | |
| 3 oz | 55px | |
| 4 oz | 55px | |
| 5 oz | 55px | |
| 6 oz | 55px | |
| 7 oz | 55px | |
| 8 oz | 55px | |
| 9 oz | 55px | |
| 10 oz | 55px | |
| 11 oz | 55px | |
| 12 oz | 55px | |
| 13 oz | 55px | |
| 13+ oz | 65px | Everything over 13 oz |
| Total Qty | 75px | Sum of all oz columns. **Bold.** |
| Total Cost | 85px | Dollar amount. **Bold.** Hidden when `hide_costs=true` |

**Table implementation:**
- Use CSS `position: sticky` on the fixed columns (Date through Class) to keep them visible while the oz columns scroll horizontally
- All oz count cells: right-aligned, monospace font
- Zero values: display as `0`, muted gray color (`#9ca3af`)
- Non-zero values: display as integer, black text, slightly bold
- Alternating row background: white / `#fafafa`
- Row height: allow wrapping for long parent/child names (min-height ~50px)
- Table header: light gray background, bold text, sticky top position

**Sorting:** Click any column header to sort by that column ascending/descending.

**Empty state:** When no data matches the filters, show a centered message: "No data found for the selected date range and filters."

---

## Tab 2 — Parcels

### Summary Bar

```
Total Records: 0    Total Pieces: 0    Total Billed: $0.00    Total Retail: $0.00
```

### Data Table

**Fixed columns:**

| Column | Width | Notes |
|---|---|---|
| Date | 90px | `YYYY-MM-DD` or "Combined" |
| Parent Name | 140px | |
| Parent # | 70px | |
| Child Name | 140px | |
| Child # | 70px | |
| Mail Class | 120px | e.g. "USPS GROUND ADVANTAGE" |
| Zone | 55px | USPS zone |

**Scrolling columns:**

| Column | Width | Notes |
|---|---|---|
| 1 lb | 55px | Piece count — packages ceiled to exactly 1 lb |
| 2 lb | 55px | |
| 3 lb | 55px | |
| 4 lb | 55px | |
| 5 lb | 55px | |
| 6 lb | 55px | |
| 7 lb | 55px | |
| 8 lb | 55px | |
| 9 lb | 55px | |
| 10 lb | 55px | |
| 10+ lb | 65px | Everything ceiled to 11 lbs or more |
| Total Qty | 75px | Bold |
| Total Billed | 90px | EFD/actual amount. Bold. Hidden when hide_costs=true |
| Total Retail | 90px | Base/retail rate. Hidden when hide_costs=true |

**Weight bucket rules** — CRITICAL — implement in `db.py` in the query layer:
- `weight_oz / 16.0` = lbs
- `math.ceil(lbs)` = lb bucket
- Bucket 1 through 10 → display as `1 lb` through `10 lb`
- Bucket 11 and above → fold into `10+ lb` column
- Exclude any piece where `weight_oz IS NULL OR weight_oz <= 0`
- Example: 17 oz → ceil(1.0625) = 2 → counts in `lb_2` column

Same styling rules as postage table (sticky columns, zero muting, alternating rows, etc.).

---

## Tab 3 — Import Summary

Date range controls (Start Date, End Date, Run button) — independent from the main filter controls at the top.

### Two side-by-side panels

**Left panel — Customer Summary** (📦 icon, "Customer Summary" heading):

Table with columns: `Customer #` | `Customer Name` | `Pieces` | `Cost`

Query: sum of `postage_data.pieces` and `postage_data.total_cost` grouped by `account_code`, joined to `customers`, filtered by date range. Include all accounts even with 0 pieces. Sort by customer_name ascending.

Footer row: `Total: {N} pieces, ${cost}`

**Right panel — Postage Classes** (🏷️ icon, "Postage Classes" heading):

Table with columns: `Class` | `Pieces` | `Cost`

Query: sum of pieces and total_cost grouped by `mail_class`, filtered by date range. Sort by pieces descending.

Footer row: `Total: {N} pieces, ${cost}`

**Below the two panels — Import Log:**

Table showing recent imports in the date range:

| File Name | Date | Rows | Imported At | Type |
|---|---|---|---|---|
| BM_3_29_26_report.csv | 2026-03-29 | 3,314 | 2026-03-29 14:22 | Postage |
| BM_4_02_26_report.csv | 2026-04-02 | 3,314 | 2026-04-02 14:05 | Postage |

Show postage imports and billing imports in one combined log, sorted by imported_at descending. Color-code: postage rows in light blue, billing rows in light orange.

---

## Data Fetch Flow

When "Load All" is clicked (or Enter pressed in the search box):

1. Read all filter values from the controls
2. Build query params
3. Fetch `/api/postage?...` and render Postage tab table + summary bar
4. Fetch `/api/parcels?...` and render Parcels tab table + summary bar
5. Show loading spinner in the active tab's table area during fetch
6. If an error occurs, show a red error banner below the controls

Auto-load on page load with default date range (last 30 days, All Accounts, Show Parent Accounts checked).

---

## Important Business Rules

1. **Never discard rows** — store zero-piece postage rows, store all billing rows even if account is unmatched. The UI filter "Remove Zero Count Rows" hides them visually but the DB keeps them.

2. **Leading zeros** — always strip from account codes before storing or comparing. `"0986"` → `986`. This is the most common source of errors.

3. **Postage oz buckets** — the postage data has exact integer oz values (0, 1, 2, ... 13, then 16, 20, 24, etc.). The display uses integer bins 0–13 and a `13+` catch-all. A 16 oz piece counts in `13+`. A 13 oz piece counts in `13`.

4. **Parcel lb buckets** — parcel weights are in oz in the source. Convert to lbs by dividing by 16, then ceiling. A 160 oz (exactly 10 lb) package → ceil(10.0) = 10 → `10 lb` bucket. A 161 oz package → ceil(10.0625) = 11 → `10+ lb` bucket.

5. **Date parsing for billing records** — `time_stamp` is stored as `M/D/YYYY HH:MM` text. Parse with `datetime.strptime(ts.strip(), '%m/%d/%Y %H:%M')`.

6. **File deduplication** — if the same postage file (by filename) or billing file (by BillingID) is imported again, delete the old records and re-import fresh. Never create duplicates.

7. **Watcher thread** — starts automatically when Flask starts. It runs as a daemon thread so it dies with the Flask process. It does not prevent Flask from starting even if the watch directory doesn't exist yet (create directories on first scan).

---

## Startup & Running

```bash
# Install dependencies
pip install flask openpyxl

# Run
python app.py
# App available at http://localhost:5000
```

On first run:
1. Create `postage.db` and all tables
2. Create watch directory structure
3. Start watcher daemon thread
4. Serve the dashboard

---

## Error Handling

- All `/api/*` routes return JSON: `{"error": "message"}` with appropriate HTTP status on failure
- Import errors are caught per-file, logged to `watch/failed/`, and never crash the server
- If `postage.db` doesn't exist yet, all data endpoints return empty results (don't crash)
- If LibreOffice is not installed, log a clear error: "LibreOffice required for XLS conversion. Install with: sudo apt install libreoffice"

---

## Reference Data — The 26 Parent Accounts

After importing `Parent_Customer_.csv`, validate that these parent account numbers exist and have the expected approximate child counts:

```
88=Hallmark(102), 730=City of KC MO(337), 780=AAFP(85), 833=Multi Service/TreviPay(37),
984=KC Public Schools(146), 986=Jackson County MO(48), 3857=One Gas(2), 3871=U of Kansas(50),
3876=Health Trust HCA(25), 3899=GEHA-EFD(13), 3900=Security Benefit Zinnia(115),
3901=Blue Cross Blue Shield EFD(105), 5006=Unified Gov WY CO KCK(50),
5175=Security Bank(9), 5237=Jackson County Circuit Court(86), 8012=JoCo Facilities(43),
8128=City of Olathe(10), 8286=KU Edwards(1), 8309=KU Hospital Authority(6),
8366=Platte County MO(1), 8393=Advent Health(2), 9103=City of Wamego(1),
9543=GAINWELL TECHNOLOGIES(21), 9639=State of Kansas(816), 9697=Fidelity State Bank(29),
9728=OGDEN PUBLICATION(14)
```

---

## Skill Files to Read First

Before writing any code, read these skill files in this order:
1. `postage-db-structure` — complete schema, relationships, validation queries
2. `pitney-to-db` — XLS conversion pipeline, postage import logic, flat rate cost import
3. `parcel-billing-import` — billing CSV column map, import function, weight bucket logic

---

## Export Buttons

Add two export buttons to the dashboard toolbar, positioned above the tab bar on the right side.

```
[📥 Export Postage Invoice]   [📦 Export Parcel Report]
```

Both buttons are always visible. Both respect the currently selected account and date range. Show a loading spinner on the button while the file generates. On completion, the browser triggers an automatic file download.

---

## Export 1 — Postage Invoice Excel

**Button label:** `📥 Export Postage Invoice`

**Trigger:** `GET /api/export/postage-invoice?parent_number={N}&start_date={Y-M-D}&end_date={Y-M-D}`

**Read the `postage-invoice-export` skill for the complete cell-by-cell layout specification.**

Summary of what that skill produces:
- One `.xlsx` file, one sheet per date in the range that has data for the selected parent account
- Each sheet is named `Mon DD YYYY` (e.g., `Mar 20 2026`)
- Each sheet has three sections:
  1. **Invoice header** — billing address, contact info, balance reconciliation (rows 1–14)
  2. **Weight break table** — piece counts for 1 oz through 13 oz plus Foreign row, with retail rates, EFD rates (retail − $0.10), piece counts, cost formulas, and savings formulas (rows 15–34)
  3. **Cost centers table** — every child account for that date listed two-per-row with piece count, charges, and savings (rows 36 onward)
- Rates come from `flat_rate_costs` table. EFD rate = `rate_retail − 0.10`
- IMB rejects default to 0 (not tracked in DB — manual entry in Excel)
- Previous balance / deposit fields default to 0 (manual reconciliation fields)

**Requires:** Parent account must be selected (not "All Accounts"). If "All Accounts" is selected when the button is clicked, show an alert: "Please select a specific parent account to export the postage invoice."

**Filename:** `Postage_Invoice_{parent_number}_{start_date}_{end_date}.xlsx`

---

## Export 2 — Parcel Report Excel

**Button label:** `📦 Export Parcel Report`

**Trigger:** `GET /api/export/parcel-report?parent_number={N}&start_date={Y-M-D}&end_date={Y-M-D}`

**Read the `parcel-billing-import` and `bc-priority-export` skills for the complete column specification.**

Summary of what this export produces:
- One `.xlsx` file, single sheet, one row per individual parcel piece from `billing_records`
- Filtered by the selected date range and parent account
- Dates are parsed from `billing_records.time_stamp` (format `M/D/YYYY HH:MM`)

**Column layout (one row per piece):**

| Col | Header | Source | Notes |
|---|---|---|---|
| A | Customer # | `custom_account_code` | integer |
| B | Customer Name | `account_name` | from billing_records |
| C | Parent Name | `parent_name` via customers join | NULL if standalone |
| D | Piece ID | `piece_id` | |
| E | Time Stamp | `time_stamp` | as stored |
| F | Mail Class | `usps_mail_class` | |
| G | Zone | `zone` | |
| H | Weight (oz) | `weight_oz` | raw ounces |
| I | Weight (lbs) | Excel formula | `=H{r}/16` |
| J | Count | 1 | always 1 per piece |
| K | Base Postage | `fully_paid_postage` | retail/base rate |
| L | EFD Postage | `billing_amount` | actual charged |
| M | Savings | Excel formula | `=K{r}-L{r}` |
| N | Permit | `permit_name` | |
| O | Department | `department_name` | |
| P | Handling Type | `handling_type` | |

**Totals row** after all data:
- A: "TOTALS" (bold)
- J: `=SUM(J2:J{n})` — total piece count
- K: `=SUM(K2:K{n})` — total base postage
- L: `=SUM(L2:L{n})` — total EFD postage
- M: `=SUM(M2:M{n})` — total savings

**Styling:**
- Header row (row 1): bold, Arial 11, light blue fill (`BDD7EE`)
- Frozen pane at A2
- Currency format (`$#,##0.00`) for columns K, L, M
- Decimal format (`0.000`) for column I (lbs)
- Totals row: bold, thin top border

**Works with "All Accounts"** — if no parent filter is selected, export all billing records in the date range.

**Filename:** `Parcel_Report_{parent_number_or_ALL}_{start_date}_{end_date}.xlsx`

---

## Skill Files to Read (updated list)

Before writing any export code, read these skills in this order:
1. `postage-db-structure` — complete schema, relationships, validation queries
2. `pitney-to-db` — XLS conversion pipeline, postage import logic, flat rate cost import
3. `parcel-billing-import` — billing CSV column map, import function, weight bucket logic
4. `postage-invoice-export` — cell-by-cell Excel layout for Export 1

