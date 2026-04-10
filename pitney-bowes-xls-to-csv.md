---
name: pitney-bowes-xls-to-csv
description: >
  Converts Pitney Bowes "DM Weight Break by Account-Carrier-Class" Business Manager
  report files (.xls, .xlsx, or raw exported .csv) into a clean, flat CSV with one
  data row per weight break. Use this skill whenever the user uploads a Pitney Bowes
  Business Manager report in any format, asks to extract mailing data from a Pitney
  Bowes file, or says anything like "process this the same way", "run it through the
  same process", or "convert this report to CSV". Triggers for any file named with
  the pattern BM_*.xls, BM_*.xlsx, or BM_*.csv.
---

# Pitney Bowes Report → Clean CSV Converter

Converts a Pitney Bowes "DM Weight Break by Account-Carrier-Class" Business Manager
report in any format (.xls, .xlsx, or raw .csv export) into a clean flat CSV:

```
Account Code, Class, Weight  (oz.), Pieces, Total Cost
```

One row per weight break. Sub-totals, headers, and footer lines are excluded.
Only the **report date range** columns are captured — the year-to-date columns
are ignored.

---

## Detect Input Format First

```python
import os

input_path = "/mnt/user-data/uploads/BM_3_19_26.csv"  # or .xls / .xlsx
ext = os.path.splitext(input_path)[1].lower()

if ext == '.csv':
    output_rows = parse_bm_csv(input_path)
elif ext == '.xlsx':
    output_rows = parse_bm_xlsx(input_path)
elif ext == '.xls':
    xlsx_path = convert_xls_to_xlsx(input_path)
    output_rows = parse_bm_xlsx(xlsx_path)
else:
    raise ValueError(f"Unsupported file type: {ext}")
```

---

## Path A — Raw CSV Input (BM_*.csv)

When Pitney Bowes exports directly to CSV, the layout is identical to the XLS
but uses comma-separated columns with the same fixed positions:

| Column index | Letter equiv | Content |
|---|---|---|
| 0 | A | Account code — appears once per account block |
| 6 | G | Class code — appears once per class block |
| 9 | J | `Sub Total` label — skip these rows |
| 12 | M | Weight in oz (data rows) |
| 15 | P | **Pieces — report date range** (NOT year-to-date) |
| 17 | R | **Total Cost — report date range** (NOT year-to-date) |

```python
import csv, re

SKIP_A = {
    'Pitney Bowes', 'DM Weight Break by Account-Carrier-Class',
    'Account Code', 'Custom Field', 'Report On Working Database'
}

def parse_bm_csv(csv_path: str) -> list:
    output_rows = []
    current_account = None
    current_class = None

    with open(csv_path, encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f)
        for row in reader:
            while len(row) < 33:
                row.append('')

            a = row[0].strip()
            g = row[6].strip()
            j = row[9].strip()
            m = row[12].strip()
            p = row[15].strip()
            r = row[17].strip()

            if a and any(a.startswith(s) for s in SKIP_A):
                continue
            if a and a.startswith('Business Manager'):
                continue
            if j == 'Sub Total':
                continue

            if a and re.match(r'^\d{4}$', a):
                current_account = a
                current_class = None
                continue

            if g:
                current_class = g
                continue

            if m:
                try:
                    weight = float(m)
                    pieces = int(p) if p else 0
                    cost = float(r) if r else 0.0
                    output_rows.append([
                        current_account or '',
                        current_class or '',
                        weight,
                        pieces,
                        f'{cost:.3f}',
                    ])
                except (ValueError, TypeError):
                    pass

    return output_rows
```

---

## Path B — XLSX Input (already converted)

```python
from openpyxl import load_workbook
import re

SKIP_A = {
    'Pitney Bowes', 'DM Weight Break by Account-Carrier-Class',
    'Account Code', 'Custom Field', 'Report On Working Database'
}

def parse_bm_xlsx(xlsx_path: str) -> list:
    output_rows = []
    current_account = None
    current_class = None

    wb = load_workbook(xlsx_path, read_only=True)
    ws = wb.active

    for row in ws.iter_rows(values_only=False):
        cells = {cell.column_letter: cell.value for cell in row if cell.value is not None}

        a = str(cells.get('A', '') or '').strip()
        g = str(cells.get('G', '') or '').strip()
        j = str(cells.get('J', '') or '').strip()
        m = cells.get('M')
        p = cells.get('P')
        r = cells.get('R')

        if a and any(a.startswith(s) for s in SKIP_A):
            continue
        if a and a.startswith('Business Manager'):
            continue
        if j == 'Sub Total':
            continue

        if a and re.match(r'^\d{4}$', a):
            current_account = a
            current_class = None
            continue

        if g:
            current_class = g
            continue

        if m is not None:
            try:
                weight = float(m)
                pieces = int(p) if p is not None else 0
                cost = float(r) if r is not None else 0.0
                output_rows.append([
                    current_account or '',
                    current_class or '',
                    weight,
                    pieces,
                    f'{cost:.3f}',
                ])
            except (ValueError, TypeError):
                pass

    wb.close()
    return output_rows
```

---

## Path C — XLS Input (legacy binary)

Convert to XLSX first, then use Path B:

```python
import subprocess, os

def convert_xls_to_xlsx(xls_path: str, out_dir: str = '/tmp') -> str:
    result = subprocess.run(
        ['libreoffice', '--headless', '--convert-to', 'xlsx',
         '--outdir', out_dir, xls_path],
        capture_output=True, text=True, timeout=90
    )
    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice conversion failed: {result.stderr}")
    xlsx_path = os.path.join(out_dir,
                 os.path.basename(xls_path).replace('.xls', '.xlsx'))
    if not os.path.exists(xlsx_path):
        raise FileNotFoundError(f"Expected output not found: {xlsx_path}")
    return xlsx_path
```

---

## Step 3 — Write the Output CSV

```python
import os, csv, re

def write_output_csv(output_rows: list, input_path: str) -> str:
    base = os.path.basename(input_path)
    # BM_3_19_26.xls  → BM_3_19_26_report.csv
    # BM_3_19_26.csv  → BM_3_19_26_report.csv  (avoids overwriting source)
    out_name = re.sub(r'\.(xls[x]?|csv)$', '_report.csv', base, flags=re.IGNORECASE)
    out_path = f"/mnt/user-data/outputs/{out_name}"

    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['Account Code', 'Class', 'Weight  (oz.)', 'Pieces', 'Total Cost'])
        writer.writerows(output_rows)

    print(f"Written {len(output_rows)} rows to {out_path}")
    return out_path
```

Then call `present_files([out_path])` to share the file.

---

## Notes & Edge Cases

- **Column positions are identical** between XLS and CSV exports — both use the same
  fixed layout. Path A (CSV) and Path B (XLSX) produce identical output.
- **Split carrier names** (`NOCARRIE` / `R` across two col-D rows) — carrier is not
  extracted so this has no impact.
- **Page break headers** repeat `Account Code`, `Weight (oz.)` etc. every ~25 rows.
  The `SKIP_A` set and `Business Manager` prefix check handle these in both paths.
- **Account code carry-forward** — persists across page breaks until a new 4-digit
  code appears. Both parsers implement this correctly.
- **`read_only=True`** is required for openpyxl on large files (10,000+ rows).
- **`utf-8-sig` encoding** for CSV — Pitney Bowes CSV exports include a BOM.
- **Output filename** — always appends `_report` before `.csv` to avoid overwriting
  the source when the input is itself a `.csv`.
- **Year-to-date columns** (W/AA in XLS, indices 22/26 in CSV) are ignored.
  Only the report-date-range columns P/R (indices 15/17) are used.
