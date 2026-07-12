---
name: bc-priority-export
description: Export billing data from the postage SQLite database into an Excel (.xlsx) file matching the BC Priority End of Week format. Use this skill whenever the user wants to export billing records, generate an invoice or billing report, create an XLS/Excel file from the database, or says anything like "export the billing data", "generate the BC Priority report", "create the weekly billing file", "export for a date range", or "make an Excel from the database". The output is a single-sheet workbook with columns for Count, lbs, Zone, Base postage, EFD postage, and Savings — matching the layout of the BC_Priority_End_of_Week template.
---

# BC Priority Export Skill

## Overview

This skill queries the `billing_records` table (and optionally joins `customers`) for a
user-specified date range, then writes an `.xlsx` file that replicates the column structure
of the **BC Priority End of Week** template. The result is a **single sheet** (no tabs per week).

---

## Output Format — Column Layout

The export matches the BC Priority End of Week template. Each data row represents one piece
from `billing_records`. The columns are:

| Excel Col | Header | Source Field | Notes |
|---|---|---|---|
| A | Customer # | `custom_account_code` | Strip leading zeros |
| B | Customer Name | `account_name` | From billing_records |
| C | Parent Name | `parent_name` (via customers join) | NULL if standalone |
| D | Piece ID | `piece_id` | |
| E | Time Stamp | `time_stamp` | |
| F | Mail Class | `usps_mail_class` | |
| G | Zone | `zone` | |
| H | Weight (oz) | `weight_oz` | Raw oz from billing data |
| I | Weight (lbs) | computed | `=H2/16` formula |
| J | Count | 1 | Always 1 per piece row |
| K | Base Postage | `fully_paid_postage` | Retail/base rate |
| L | EFD Postage | `billing_amount` | Actual charged amount |
| M | Savings | computed | `=K2-L2` formula |
| N | Permit | `permit_name` | |
| O | Department | `department_name` | |
| P | Handling Type | `handling_type` | |

> **Note:** If the user wants a summary view (pieces aggregated by zone/weight class)
> instead of individual piece rows, ask for clarification. Default is one row per piece.

---

## Summary Totals Row

After all data rows, add a totals row:
- Col A: "TOTALS"
- Col J: `=SUM(J2:Jn)` — total piece count
- Col K: `=SUM(K2:Kn)` — total base postage
- Col L: `=SUM(L2:Ln)` — total EFD postage  
- Col M: `=SUM(M2:Mn)` — total savings

---

## Date Filtering

The `billing_records.time_stamp` column has format `M/D/YYYY HH:MM`.

The user will provide start and end dates. Filter using:

```sql
SELECT br.*, c.parent_name, c.customer_name as db_customer_name
FROM billing_records br
LEFT JOIN customers c ON br.custom_account_code = c.customer_number
WHERE date(
    substr(br.time_stamp, -4) || '-' ||
    printf('%02d', CAST(substr(br.time_stamp, 1, instr(br.time_stamp,'/')-1) AS INT)) || '-' ||
    printf('%02d', CAST(substr(substr(br.time_stamp, instr(br.time_stamp,'/')+1),
        1, instr(substr(br.time_stamp, instr(br.time_stamp,'/')+1),'/')-1) AS INT))
) BETWEEN :start_date AND :end_date
ORDER BY br.time_stamp, br.custom_account_code, br.usps_mail_class
```

**Simpler alternative** — parse dates in Python after querying, then filter:
```python
from datetime import datetime
def parse_ts(ts):
    try:
        return datetime.strptime(ts.strip(), '%m/%d/%Y %H:%M')
    except:
        return None
```

---

## Excel Styling

Use `openpyxl` to create the workbook.

### Header row (row 1)
- Bold, Arial 11
- Fill: light blue (`BDD7EE` — standard Excel table header blue)
- Freeze pane at A2 (so header stays visible when scrolling)

### Data rows
- Arial 10
- Currency columns (K, L, M): `$#,##0.00` number format
- Weight (lbs) column (I): `0.000` format
- Alternating row shading optional (light gray `F2F2F2` on even rows)

### Column widths (approximate)
```
A: 12, B: 30, C: 30, D: 28, E: 18, F: 25, G: 6,
H: 12, I: 12, J: 8, K: 14, L: 14, M: 12, N: 25, O: 20, P: 15
```

### Totals row
- Bold
- Top border on the totals row cells

---

## Workflow

1. Ask the user for the start date and end date if not already provided (format: MM/DD/YYYY or YYYY-MM-DD)
2. Ask for the database path if not already known (or use the default `postage.db` / `postage_reporting.db`)
3. Query the database using the date filter above
4. Build the workbook with openpyxl
5. Use Excel formulas (`=H2/16`, `=K2-L2`) — do NOT pre-calculate these in Python
6. Add SUM formulas in the totals row
7. Save to `/mnt/user-data/outputs/BC_Priority_Export_{start}_{end}.xlsx`
8. Present the file

---

## Python Skeleton

```python
import sqlite3
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from datetime import datetime

def export_bc_priority(db_path, start_date, end_date, output_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT br.custom_account_code, br.account_name, c.parent_name,
               br.piece_id, br.time_stamp, br.usps_mail_class, br.zone,
               br.weight_oz, br.fully_paid_postage, br.billing_amount,
               br.permit_name, br.department_name, br.handling_type
        FROM billing_records br
        LEFT JOIN customers c ON br.custom_account_code = c.customer_number
        ORDER BY br.time_stamp, br.custom_account_code
    """)
    all_rows = cur.fetchall()
    conn.close()

    # Filter by date in Python (simpler than SQL date parsing for this format)
    start = datetime.strptime(start_date, '%Y-%m-%d')
    end = datetime.strptime(end_date, '%Y-%m-%d')

    def ts_date(ts):
        try:
            return datetime.strptime(ts.strip(), '%m/%d/%Y %H:%M')
        except:
            return None

    filtered = [r for r in all_rows if ts_date(r['time_stamp']) and
                start <= ts_date(r['time_stamp']) <= end.replace(hour=23, minute=59)]

    wb = Workbook()
    ws = wb.active
    ws.title = f"{start_date} to {end_date}"

    headers = ['Customer #', 'Customer Name', 'Parent Name', 'Piece ID', 'Time Stamp',
               'Mail Class', 'Zone', 'Weight (oz)', 'Weight (lbs)', 'Count',
               'Base Postage', 'EFD Postage', 'Savings', 'Permit', 'Department', 'Handling Type']

    header_fill = PatternFill('solid', start_color='BDD7EE')
    header_font = Font(name='Arial', bold=True, size=11)

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center')

    for i, row in enumerate(filtered, 2):
        ws.cell(i, 1, row['custom_account_code'])
        ws.cell(i, 2, row['account_name'])
        ws.cell(i, 3, row['parent_name'])
        ws.cell(i, 4, row['piece_id'])
        ws.cell(i, 5, row['time_stamp'])
        ws.cell(i, 6, row['usps_mail_class'])
        ws.cell(i, 7, row['zone'])
        ws.cell(i, 8, row['weight_oz'])
        ws.cell(i, 9, f'=H{i}/16')          # weight in lbs
        ws.cell(i, 10, 1)                    # count — always 1 per piece
        ws.cell(i, 11, row['fully_paid_postage'])
        ws.cell(i, 12, row['billing_amount'])
        ws.cell(i, 13, f'=K{i}-L{i}')       # savings
        ws.cell(i, 14, row['permit_name'])
        ws.cell(i, 15, row['department_name'])
        ws.cell(i, 16, row['handling_type'])

        # Currency format for postage columns
        for col in [11, 12, 13]:
            ws.cell(i, col).number_format = '$#,##0.00'
        ws.cell(i, 9).number_format = '0.000'

    # Totals row
    n = len(filtered) + 1
    tot = n + 1
    bold = Font(name='Arial', bold=True, size=11)
    top_border = Border(top=Side(style='thin'))
    ws.cell(tot, 1, 'TOTALS').font = bold
    for col, formula_col in [(10, 'J'), (11, 'K'), (12, 'L'), (13, 'M')]:
        c = ws.cell(tot, col, f'=SUM({formula_col}2:{formula_col}{n})')
        c.font = bold
        c.border = top_border
        if col in [11, 12, 13]:
            c.number_format = '$#,##0.00'

    # Column widths
    widths = [12, 30, 30, 28, 18, 25, 6, 12, 12, 8, 14, 14, 12, 25, 20, 15]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = 'A2'

    wb.save(output_path)
    return len(filtered)
```

---

## Reporting Back

After export, tell the user:
- Total pieces exported
- Date range covered
- Any accounts with `unmatched_account = 1` that appear in the export (flag them)
- Path/link to the file

---

## Common Issues

- **No billing data in range:** Confirm the date format and that billing CSVs for that period have been imported
- **unmatched_account rows:** These will still be exported but won't have parent info from customers; flag them
- **time_stamp format variations:** The source format is `M/D/YYYY HH:MM`; if a row fails to parse, skip it and count it as a parse warning
- **Large exports:** Files with 50k+ rows may take a moment; inform the user it's running
