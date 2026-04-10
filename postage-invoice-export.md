---
name: postage-invoice-export
description: >
  Generates a formatted Excel invoice (.xlsx) from postage_data in the database, matching the
  BC_BS_first_cls_flats_2026.xlsx layout exactly. Use this skill whenever the user wants to
  export postage data to Excel, generate a billing invoice, or says anything like "export the
  postage report", "generate the invoice", "download the postage Excel", or "export for BCBS".
  Produces one sheet per date in the selected range. Each sheet has three sections: an invoice
  header, a weight-break summary table (1–13 oz + Foreign), and a cost-centers table listing
  every child account with piece count, charges, and savings. Always read postage-db-structure
  first for the full schema.
---

# Postage Invoice Export — Excel Format

## Overview

Produces one `.xlsx` file. One worksheet per unique `file_date` within the selected date range,
named `Mon DD YYYY` (e.g., `Mar 20 2026`). Sheets are ordered chronologically.

The format is a three-section weekly billing invoice:

```
Section 1: Invoice header (rows 1–14)     — billing address, balance reconciliation, date
Section 2: Weight break table (rows 15–34) — pieces per oz by weight, rates, costs, savings
Section 3: Cost centers table (rows 36–N)  — per-child-account pieces, charges, savings
```

---

## Rate Logic — EFD Discount

The flat rate cost table (`flat_rate_costs`) stores the retail rate per oz.
The **EFD 1st Class rate** (the discounted rate charged to EFD customers) is always:

```python
efd_rate = retail_rate - 0.10   # $0.10 discount per piece at every weight
```

This is consistent across all weights (confirmed from source data).

Rates come from the `flat_rate_costs` table by weight:
```sql
SELECT weight_not_over_oz, rate_retail
FROM flat_rate_costs
ORDER BY weight_not_over_oz;
```

The `weight_not_over_oz` column matches directly to the oz weight in `postage_data.weight_oz`
(e.g., `weight_not_over_oz = 1` → 1 oz rate, `weight_not_over_oz = 13` → 13 oz rate).

---

## Data Query

For each sheet date, query postage data for the selected parent account:

```sql
-- Weight-break aggregates (Section 2)
SELECT
    p.weight_oz,
    SUM(p.pieces) AS total_pieces,
    SUM(p.total_cost) AS total_cost
FROM postage_data p
JOIN customers c ON p.account_code = c.customer_number
WHERE p.file_date = :date
  AND (c.parent_number = :parent_number OR c.customer_number = :parent_number)
  AND p.mail_class IN ('1CA5DFlt','1ClFlat','1CSPiece','1CNAPres','1CAAADCL','1CMAADCL','1stClNMLtr')
  AND p.weight_oz BETWEEN 1 AND 13
GROUP BY p.weight_oz
ORDER BY p.weight_oz;

-- Child account breakdown (Section 3)
SELECT
    c.customer_number,
    c.customer_name,
    SUM(p.pieces) AS total_pieces,
    SUM(p.total_cost) AS total_cost
FROM postage_data p
JOIN customers c ON p.account_code = c.customer_number
WHERE p.file_date = :date
  AND (c.parent_number = :parent_number OR c.customer_number = :parent_number)
GROUP BY c.customer_number, c.customer_name
ORDER BY c.customer_number;
```

For "All Accounts" export (no parent filter), run the same queries without the parent filter
and group by parent account, producing one sheet per date per parent that has data.

---

## Excel Layout — Cell-by-Cell Specification

All rows are 1-indexed. All columns are 1-indexed (A=1, B=2, etc.).

### Section 1 — Invoice Header (Rows 1–14)

```
Row 1:  A1="INVOICE # "  C1={invoice_seq_number}
Row 2:  M2="Week ending"
Row 3:  A3="Bill to: "   C3={parent_name}   L3="Project Date:"  M3=formula: =F14
Row 4:  A4="Attn:"       C4={contact_name}  L4="Customer ID#"   M4={customer_id_text}
Row 5:  C5={address_line1}
Row 6:  C6={city_state_zip}
Row 8:  A8="Phone #"     C8={phone}
Row 9:  A9="Fax#"        C9={fax}
Row 10: A10="email:"     C10={email}
Row 11: I11="Account Summary for {parent_name}"
Row 12: D12="Previous Acct. Balance"  G12="Funds"  H12="Deposit"
        J12="Funds"  K12=" Used"  M12="New Balance"
Row 13: E13={prev_balance}  G13={deposit_amount}  J13="$"
        K13=formula: =L34   M13=formula: =(E13+G13)-K13
Row 14: A14={efd_address}  F14={date as Excel date value}
```

**Contact info** — store in a `customer_contacts` dict keyed by customer_number, or as a
separate DB table. For initial build, hardcode BCBS contact (customer 3901):
```python
CUSTOMER_CONTACTS = {
    3901: {
        'contact_name': 'Chris Torrez',
        'address1': '1133 S.W. Topeka Blvd.',
        'city_state_zip': 'Topeka, KS 66629-0001',
        'phone': '785-291-8681',
        'fax': '785-291-8548',
        'email': 'Chris.Torrez@bcbsks.com',
        'customer_id': '            1st 0012',
    }
}
```

**EFD address** (always the same — the mailing facility):
```
2820 Roe Lane Bldg U
Kansas City, KS 66103
phone: 913-671-0011
fax:   913-403-9919
email: efdmailing@aol.com
```

These appear in:
- A14 = "2820 Roe Lane Bldg U "
- A15 = "Kansas City, KS 66103"
- A17 = "phone"  B17 = "913-671-0011"
- A18 = "fax "   B18 = "913-403-9919"
- A19 = "email " B19 = "efdmailing@aol.com"

**Previous balance / deposit**: Set to 0 / 0 initially — these fields are for manual entry.
Store as hardcoded 0 with a note that they are for client reconciliation.

**Invoice sequence number**: Increment per parent per export run. Store in DB or just use
the sheet index (1, 2, 3...) within the file.

---

### Section 2 — Weight Break Table (Rows 15–34)

#### Row 15 — Column Headers

```
F15="Weight"   G15="1st Class"   H15="EFD 1st Class"
I15="Total #'s"   J15="IMB rejects"   K15="Total #'s"
L15="Costs"   M15="Savings"
```

#### Rows 16–28 — Weight Rows (1 oz through 13 oz)

One row per oz weight. Row 16 = 1 oz, Row 17 = 2 oz, ..., Row 28 = 13 oz.

```
Row offset = 15 + weight_oz   (so 1oz=row16, 2oz=row17, ..., 13oz=row28)

F{r} = "{N} oz"              (e.g., "1 oz", "2 oz")
G{r} = {rate_retail}         (float, from flat_rate_costs)
H{r} = {rate_retail - 0.10}  (float, EFD rate)
I{r} = {pieces}              (integer, from DB query — 0 if no pieces for this weight)
J{r} = {rate_retail}         (same as G — this is the rate for IMB reject pieces)
K{r} = 0                     (integer — IMB rejects count, default 0)
L{r} = formula: =H{r}*I{r}+J{r}*K{r}   (EFD cost + reject cost)
M{r} = formula: =G{r}*I{r}+G{r}*K{r}-L{r}  (retail cost - actual cost = savings)
```

#### Rows 29–31 — Empty weight rows (reserved for overflow)

```
L{r} = formula: =H{r}*I{r}+J{r}*K{r}
M{r} = formula: =G{r}*I{r}+G{r}*K{r}-L{r}
(no other values)
```

#### Row 32 — Foreign

```
F32="Foreign"
G32={international_rate}   (use 10.10 as default — update when rates known)
H32={international_efd}    (use 10.00 as default)
I32=0   J32={intl_rate}   K32=0
L32=formula: =H32*I32+J32*K32
M32=formula: =G32*I32+G32*K32-L32
```

#### Row 33 — Subtotals

```
I33=formula: =SUM(I16:I32)   (total pieces, EFD)
K33=formula: =SUM(K16:K32)   (total IMB rejects)
M33="Total Savings"          (label)
```

#### Row 34 — Totals

```
H34="Total Pieces"
I34=formula: =SUM(I33+K33)   (EFD + reject total)
K34="Total Cost:"
L34=formula: =SUM(L16:L33)
M34=formula: =SUM(M16:M33)
```

---

### Section 3 — Cost Centers Table (Rows 36–N)

#### Row 36 — Column Headers (two groups)

```
A36="Cost Centers "   D36="# Pieces "   E36="Charges "   F36="Savings "
H36="Cost Centers "   J36="# Pieces "   K36="Charges "   L36="Savings "
```

#### Rows 37 onwards — Child Account Rows

Child accounts are laid out **two per row**, left group (cols A/D/E/F) and right group
(cols H/J/K/L). Sort child accounts by customer_number ascending.

```python
# Layout logic
child_accounts = [...]  # sorted by customer_number

for i, account in enumerate(child_accounts):
    row = 37 + (i // 2)
    if i % 2 == 0:  # left column
        ws.cell(row, 1, account['customer_number'])   # or customer_name if alpha-prefixed
        ws.cell(row, 4, account['total_pieces'])
        ws.cell(row, 5, account['total_cost'])
        ws.cell(row, 6, account['savings'])
    else:            # right column
        ws.cell(row, 8, account['customer_number'])
        ws.cell(row, 10, account['total_pieces'])
        ws.cell(row, 11, account['total_cost'])
        ws.cell(row, 12, account['savings'])
```

**Account identifier**: Use `customer_number` as an integer. If the account has a letter
prefix in the source (like "A2280", "B2810"), that prefix comes from a separate coding
system not in the current database — use the plain integer `customer_number` for now.

**Savings per child account**:
```python
# Savings = (retail_rate × pieces) - actual_cost
# Since we have total_cost but not per-weight breakdown per child,
# approximate with: savings = total_pieces × 0.10
# (because EFD saves exactly $0.10 per piece vs retail)
savings = round(account['total_pieces'] * 0.10, 2)
```

#### Totals Rows (after all child rows)

Let `last_data_row` = 36 + ceil(len(child_accounts) / 2)
Let `totals_row` = last_data_row + 1

```
totals_row:
  B = formula: =SUM(E37:E{last})     (sum of left charges)
  C = formula: =SUM(F37:F{last})     (sum of left savings)

totals_row + 1:
  A = formula: =SUM(D37:D{totals})   (sum of left pieces)
  B = formula: =SUM(K37:K{totals})   (sum of right charges)
  C = formula: =SUM(L37:L{totals})   (sum of right savings)

totals_row + 2:
  A = formula: =SUM(J37:J{totals})   (sum of right pieces)

summary_row (totals_row + 4):
  E = "Total Pieces"   G = "Total"   H = "Costs"   I = "Total"   J = "Savings"

summary_row + 1:
  E = formula: =SUM(A{t+1}+A{t+2})  (total pieces left + right)
  G = formula: =SUM(B{t}+B{t+1})    (total charges)
  J = formula: =SUM(C{t}+C{t+1})    (total savings)
```

---

## Styling

Apply these styles using openpyxl:

```python
from openpyxl.styles import Font, PatternFill, Alignment, numbers

BOLD = Font(bold=True)
CURRENCY_FMT = '$#,##0.00'
INT_FMT = '#,##0'

# Header rows — bold everything in rows 1-15
for row in ws.iter_rows(min_row=1, max_row=15):
    for cell in row:
        cell.font = BOLD

# Weight break rows — bold all
for row in ws.iter_rows(min_row=16, max_row=34):
    for cell in row:
        cell.font = BOLD

# Rate columns G and H — currency format
for r in range(16, 35):
    ws.cell(r, 7).number_format = CURRENCY_FMT   # col G
    ws.cell(r, 8).number_format = CURRENCY_FMT   # col H

# Cost and Savings columns L and M — currency format
for r in range(16, 35):
    ws.cell(r, 12).number_format = CURRENCY_FMT  # col L
    ws.cell(r, 13).number_format = CURRENCY_FMT  # col M

# Piece count columns I and K — integer format
for r in range(16, 35):
    ws.cell(r, 9).number_format = INT_FMT   # col I
    ws.cell(r, 11).number_format = INT_FMT  # col K

# Cost centers — bold headers, all content bold, currency for charges/savings
for r in range(36, last_row + 1):
    for c in [1, 4, 5, 6, 8, 10, 11, 12]:
        ws.cell(r, c).font = BOLD

# Date in F14 — format as date
ws.cell(14, 6).number_format = 'MM/DD/YYYY'

# Column widths (approximate)
ws.column_dimensions['A'].width = 12
ws.column_dimensions['C'].width = 30
ws.column_dimensions['F'].width = 10
ws.column_dimensions['G'].width = 12
ws.column_dimensions['H'].width = 14
ws.column_dimensions['I'].width = 10
ws.column_dimensions['J'].width = 13
ws.column_dimensions['K'].width = 10
ws.column_dimensions['L'].width = 12
ws.column_dimensions['M'].width = 14
```

---

## Complete Export Function

```python
import sqlite3
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from datetime import datetime, date
import os

def export_postage_invoice(
    db_path: str,
    parent_number: int,
    start_date: str,        # 'YYYY-MM-DD'
    end_date: str,          # 'YYYY-MM-DD'
    output_path: str
) -> dict:
    """
    Generate the postage invoice Excel for one parent account over a date range.
    One sheet per date that has data.
    Returns: {'sheets': N, 'output_path': str}
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Get rates from DB
    rates = {}
    for row in cur.execute("SELECT weight_not_over_oz, rate_retail FROM flat_rate_costs ORDER BY weight_not_over_oz"):
        rates[int(row['weight_not_over_oz'])] = row['rate_retail']

    # Get all dates in range that have data for this parent
    dates = [r[0] for r in cur.execute("""
        SELECT DISTINCT p.file_date
        FROM postage_data p
        JOIN customers c ON p.account_code = c.customer_number
        WHERE p.file_date BETWEEN ? AND ?
          AND (c.parent_number = ? OR c.customer_number = ?)
        ORDER BY p.file_date
    """, (start_date, end_date, parent_number, parent_number)).fetchall()]

    # Get parent name
    parent = cur.execute(
        "SELECT customer_name FROM customers WHERE customer_number = ?",
        (parent_number,)
    ).fetchone()
    parent_name = parent['customer_name'] if parent else f"Account {parent_number}"

    contact = CUSTOMER_CONTACTS.get(parent_number, {})
    wb = Workbook()
    wb.remove(wb.active)  # remove default sheet

    for sheet_idx, file_date in enumerate(dates):
        dt = datetime.strptime(file_date, '%Y-%m-%d')
        sheet_name = dt.strftime('%b %-d %Y')   # e.g., "Mar 20 2026"
        ws = wb.create_sheet(sheet_name)

        # -- Weight break data --
        weight_data = {}
        for row in cur.execute("""
            SELECT p.weight_oz, SUM(p.pieces) AS pieces, SUM(p.total_cost) AS cost
            FROM postage_data p
            JOIN customers c ON p.account_code = c.customer_number
            WHERE p.file_date = ?
              AND (c.parent_number = ? OR c.customer_number = ?)
              AND p.weight_oz BETWEEN 1 AND 13
            GROUP BY p.weight_oz
        """, (file_date, parent_number, parent_number)):
            weight_data[int(row['weight_oz'])] = {
                'pieces': row['pieces'] or 0,
                'cost': row['cost'] or 0.0,
            }

        # -- Child account data --
        children = cur.execute("""
            SELECT c.customer_number, c.customer_name,
                   SUM(p.pieces) AS pieces, SUM(p.total_cost) AS cost
            FROM postage_data p
            JOIN customers c ON p.account_code = c.customer_number
            WHERE p.file_date = ?
              AND (c.parent_number = ? OR c.customer_number = ?)
            GROUP BY c.customer_number, c.customer_name
            ORDER BY c.customer_number
        """, (file_date, parent_number, parent_number)).fetchall()

        _write_invoice_sheet(
            ws=ws,
            sheet_idx=sheet_idx + 1,
            file_date=dt,
            parent_number=parent_number,
            parent_name=parent_name,
            contact=contact,
            rates=rates,
            weight_data=weight_data,
            children=children,
        )

    conn.close()
    wb.save(output_path)
    return {'sheets': len(dates), 'output_path': output_path}


def _write_invoice_sheet(ws, sheet_idx, file_date, parent_number, parent_name,
                          contact, rates, weight_data, children):
    BOLD = Font(bold=True)
    CURR = '$#,##0.00'
    INT_FMT = '#,##0'

    # ── Section 1: Header ──────────────────────────────────────────────────
    ws['A1'] = 'INVOICE # ';   ws['A1'].font = BOLD
    ws['C1'] = sheet_idx;      ws['C1'].font = BOLD
    ws['M2'] = 'Week ending';  ws['M2'].font = BOLD

    ws['A3'] = 'Bill to: ';    ws['A3'].font = BOLD
    ws['C3'] = parent_name;    ws['C3'].font = BOLD
    ws['L3'] = 'Project Date:'; ws['L3'].font = BOLD
    ws['M3'] = '=F14';          ws['M3'].font = BOLD

    ws['A4'] = 'Attn:';        ws['A4'].font = BOLD
    ws['C4'] = contact.get('contact_name', '');  ws['C4'].font = BOLD
    ws['L4'] = 'Customer ID#'; ws['L4'].font = BOLD
    ws['M4'] = contact.get('customer_id', f'            {parent_number}'); ws['M4'].font = BOLD

    ws['C5'] = contact.get('address1', '')
    ws['C6'] = contact.get('city_state_zip', '')
    ws['A8'] = 'Phone #'; ws['C8'] = contact.get('phone', '')
    ws['A9'] = 'Fax#';    ws['C9'] = contact.get('fax', '')
    ws['A10'] = 'email:'; ws['C10'] = contact.get('email', '')

    ws.cell(11, 9, f'Account Summary for {parent_name}').font = BOLD

    ws['D12'] = 'Previous Acct. Balance'; ws['D12'].font = BOLD
    ws['G12'] = 'Funds  '; ws['H12'] = 'Deposit'; ws['J12'] = 'Funds  '
    ws['K12'] = ' Used';   ws['M12'] = 'New Balance'
    for c in ['D12','G12','H12','J12','K12','M12']:
        ws[c].font = BOLD

    ws['E13'] = 0;    ws['E13'].font = BOLD    # prev balance — 0 for now
    ws['G13'] = 0;    ws['G13'].font = BOLD    # deposit — 0 for now
    ws['J13'] = '$'
    ws['K13'] = '=L34';              ws['K13'].font = BOLD
    ws['M13'] = '=(E13+G13)-K13';   ws['M13'].font = BOLD

    ws['A14'] = '2820 Roe Lane Bldg U '
    ws.cell(14, 6, file_date).number_format = 'MM/DD/YYYY'

    ws['A15'] = 'Kansas City, KS 66103'
    ws['A17'] = 'phone'; ws['B17'] = '913-671-0011'
    ws['A18'] = 'fax ';  ws['B18'] = '913-403-9919'
    ws['A19'] = 'email '; ws['B19'] = 'efdmailing@aol.com'

    # ── Section 2: Weight Break Table ────────────────────────────────────
    # Headers row 15
    for col, val in [(6,'Weight'),(7,'1st Class'),(8,'EFD 1st Class'),
                     (9,"Total #'s"),(10,'IMB rejects'),(11,"Total #'s"),
                     (12,'Costs'),(13,'Savings')]:
        c = ws.cell(15, col, val);  c.font = BOLD

    # Weight rows 16–28 (1 oz – 13 oz)
    for oz in range(1, 14):
        r = 15 + oz
        retail = rates.get(oz, 0.0)
        efd = round(retail - 0.10, 4)
        pieces = weight_data.get(oz, {}).get('pieces', 0)

        ws.cell(r, 6, f'{oz} oz').font = BOLD
        ws.cell(r, 7, retail).number_format = CURR;  ws.cell(r,7).font = BOLD
        ws.cell(r, 8, efd).number_format = CURR;     ws.cell(r,8).font = BOLD
        ws.cell(r, 9, pieces).number_format = INT_FMT; ws.cell(r,9).font = BOLD
        ws.cell(r, 10, retail).number_format = CURR; ws.cell(r,10).font = BOLD
        ws.cell(r, 11, 0).number_format = INT_FMT;   ws.cell(r,11).font = BOLD
        ws.cell(r, 12, f'=H{r}*I{r}+J{r}*K{r}').number_format = CURR; ws.cell(r,12).font = BOLD
        ws.cell(r, 13, f'=G{r}*I{r}+G{r}*K{r}-L{r}').number_format = CURR; ws.cell(r,13).font = BOLD

    # Rows 29–31: empty (reserved)
    for r in [29, 30, 31]:
        ws.cell(r, 12, f'=H{r}*I{r}+J{r}*K{r}').font = BOLD
        ws.cell(r, 13, f'=G{r}*I{r}+G{r}*K{r}-L{r}').font = BOLD

    # Row 32: Foreign
    intl_retail = 10.10;  intl_efd = 10.00
    ws.cell(32, 6, 'Foreign').font = BOLD
    ws.cell(32, 7, intl_retail).number_format = CURR; ws.cell(32,7).font = BOLD
    ws.cell(32, 8, intl_efd).number_format = CURR;   ws.cell(32,8).font = BOLD
    ws.cell(32, 9, 0).font = BOLD
    ws.cell(32, 10, intl_retail).number_format = CURR; ws.cell(32,10).font = BOLD
    ws.cell(32, 11, 0).font = BOLD
    ws.cell(32, 12, '=H32*I32+J32*K32').number_format = CURR; ws.cell(32,12).font = BOLD
    ws.cell(32, 13, '=G32*I32+G32*K32-L32').number_format = CURR; ws.cell(32,13).font = BOLD

    # Row 33: subtotals
    ws.cell(33, 9,  '=SUM(I16:I32)').font = BOLD
    ws.cell(33, 11, '=SUM(K16:K32)').font = BOLD
    ws.cell(33, 13, 'Total Savings').font = BOLD

    # Row 34: totals
    ws.cell(34, 8,  'Total Pieces').font = BOLD
    ws.cell(34, 9,  '=SUM(I33+K33)').font = BOLD
    ws.cell(34, 11, 'Total Cost:').font = BOLD
    ws.cell(34, 12, '=SUM(L16:L33)').number_format = CURR; ws.cell(34,12).font = BOLD
    ws.cell(34, 13, '=SUM(M16:M33)').number_format = CURR; ws.cell(34,13).font = BOLD

    # ── Section 3: Cost Centers ───────────────────────────────────────────
    # Row 36: headers
    for col, val in [(1,'Cost Centers '),(4,'# Pieces '),(5,'Charges '),(6,'Savings '),
                     (8,'Cost Centers '),(10,'# Pieces '),(11,'Charges '),(12,'Savings ')]:
        ws.cell(36, col, val).font = BOLD

    # Child rows
    child_list = list(children)
    last_data_row = 36
    for i, child in enumerate(child_list):
        row = 37 + (i // 2)
        last_data_row = row
        savings = round((child['pieces'] or 0) * 0.10, 2)
        if i % 2 == 0:
            ws.cell(row, 1, child['customer_number']).font = BOLD
            ws.cell(row, 4, child['pieces'] or 0).font = BOLD
            ws.cell(row, 5, child['cost'] or 0.0).number_format = CURR; ws.cell(row,5).font = BOLD
            ws.cell(row, 6, savings).number_format = CURR; ws.cell(row,6).font = BOLD
        else:
            ws.cell(row, 8, child['customer_number']).font = BOLD
            ws.cell(row, 10, child['pieces'] or 0).font = BOLD
            ws.cell(row, 11, child['cost'] or 0.0).number_format = CURR; ws.cell(row,11).font = BOLD
            ws.cell(row, 12, savings).number_format = CURR; ws.cell(row,12).font = BOLD

    # Totals
    t = last_data_row + 1
    ws.cell(t,   2, f'=SUM(E37:E{last_data_row})')
    ws.cell(t,   3, f'=SUM(F37:F{last_data_row})')
    ws.cell(t+1, 1, f'=SUM(D37:D{t})')
    ws.cell(t+1, 2, f'=SUM(K37:K{t})')
    ws.cell(t+1, 3, f'=SUM(L37:L{t})')
    ws.cell(t+2, 1, f'=SUM(J37:J{t})')

    s = t + 4  # summary row
    ws.cell(s,   5, 'Total Pieces').font = BOLD
    ws.cell(s,   7, 'Total').font = BOLD
    ws.cell(s,   8, 'Costs').font = BOLD
    ws.cell(s,   9, 'Total').font = BOLD
    ws.cell(s,  10, 'Savings').font = BOLD
    ws.cell(s+1, 5, f'=SUM(A{t+1}+A{t+2})').font = BOLD
    ws.cell(s+1, 7, f'=SUM(B{t}+B{t+1})').font = BOLD
    ws.cell(s+1,10, f'=SUM(C{t}+C{t+1})').font = BOLD
```

---

## API Route

```python
@app.route('/api/export/postage-invoice')
def export_postage_invoice_route():
    parent_number = request.args.get('parent_number', type=int)
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    if not all([parent_number, start_date, end_date]):
        return jsonify({'error': 'parent_number, start_date, end_date required'}), 400

    filename = f'Postage_Invoice_{parent_number}_{start_date}_{end_date}.xlsx'
    output_path = os.path.join('/tmp', filename)

    result = export_postage_invoice(
        db_path='postage.db',
        parent_number=parent_number,
        start_date=start_date,
        end_date=end_date,
        output_path=output_path
    )

    return send_file(output_path, as_attachment=True,
                     download_name=filename,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
```

---

## Filename Convention

`Postage_Invoice_{parent_number}_{start_date}_{end_date}.xlsx`

Example: `Postage_Invoice_3901_2026-03-01_2026-03-31.xlsx`

---

## Notes

- **IMB rejects** default to 0 — we do not currently track them in the database. The K column
  cells are left as integer 0 and can be manually updated in Excel.
- **Balance / Deposit fields** (E13, G13) default to 0 — these are for manual reconciliation
  by the billing team.
- **Contact info** for accounts not in `CUSTOMER_CONTACTS` — leave blank (empty string).
  A future enhancement would store contact info in the database.
- **Foreign postage** (row 32) defaults pieces to 0. International mail (classes `Int1stFl`,
  `Int1stLt`, `Int1stS`) is not included in the weight-break section but could be summed
  separately and placed in row 32 col I.
- Use `flask.send_file()` with `as_attachment=True` to trigger browser download.
- Delete the temp file after sending, or use `tempfile.NamedTemporaryFile`.
