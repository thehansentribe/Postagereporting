---
name: parcel-billing-import
description: >
  Imports Copy_of_Export_Billing_*.csv parcel billing files into the postage SQLite database.
  Use this skill whenever importing a billing export CSV, writing the billing watch folder handler,
  querying billing_records, or building any feature that uses parcel/package billing data.
  Column N (Custom Account Code) is the customer number linking parcels to the customer hierarchy.
  Always read postage-db-structure first for the full schema.
---

# Parcel Billing CSV Import

## Overview

Parcel billing files arrive named `Copy_of_Export_Billing_{ID}.csv` (e.g. `Copy_of_Export_Billing_3785.csv`).
Each file contains one row per mail piece with 95 columns of billing, routing, and tracking data.

**The critical customer link is Column N — `Custom Account Code`.**
Strip leading zeros and match to `customers.customer_number`.

---

## Source File Format

| Property | Value |
|---|---|
| Encoding | UTF-8, may have BOM — open with `utf-8-sig` |
| Line endings | CRLF |
| Columns | 95 |
| Key dedup column | `BillingID` (column AO, index 72) — e.g. `3784`, `3785` |
| Customer link | `Custom Account Code` (column N, index 13) — strip leading zeros |
| Timestamp format | `M/D/YYYY HH:MM` — e.g. `4/1/2026 15:34` |
| Weight column | `Weight` (column D) — already in ounces |

---

## Complete Column Map (all 95 columns)

The columns below are indexed 0-based. Use `csv.DictReader` and strip all header whitespace.

| Index | Header | DB Column | Type | Notes |
|---|---|---|---|---|
| 0 | `Piece ID` | `piece_id` | TEXT | Unique piece identifier |
| 1 | `Machine Serial` | `machine_serial` | TEXT | |
| 2 | `Time Stamp` | `time_stamp` | TEXT | `M/D/YYYY HH:MM` — store as-is |
| 3 | `Weight` | `weight_oz` | REAL | Ounces |
| 4 | `Handling Type` | `handling_type` | TEXT | e.g. `PARCELS` |
| 5 | `USPS Mail Class` | `usps_mail_class` | TEXT | e.g. `USPS GROUND ADVANTAGE` |
| 6 | `USPS Mail Prep Type` | `usps_mail_prep_type` | TEXT | e.g. `COMMERCIAL` |
| 7 | `Routing Category` | `routing_category` | TEXT | e.g. `Resolved` |
| 8 | `Routing String` | `routing_string` | TEXT | ZIP+4 routing |
| 9 | `Bundle Qualification` | `bundle_qualification` | TEXT | |
| 10 | `Bundle Zip` | `bundle_zip` | TEXT | |
| 11 | `Account ID` | `account_id` | TEXT | Internal machine account ID |
| 12 | `AccountName` | `account_name` | TEXT | Human-readable name |
| **13** | **`Custom Account Code`** | **`custom_account_code`** | **INTEGER** | **Col N — strip leading zeros — customer link** |
| 14 | `Customer Barcode Symbology` | `customer_barcode_symbology` | TEXT | |
| 15 | `Customer Barcode` | `customer_barcode` | TEXT | |
| 16 | `Department ID` | `department_id` | TEXT | |
| 17 | `Department Name` | `department_name` | TEXT | |
| 18 | `Manifest ID` | `manifest_id` | TEXT | |
| 19 | `Piece Postage` | `piece_postage` | REAL | |
| 20 | `LBS Postage` | `lbs_postage` | REAL | |
| 21 | `Final Postage` | `final_postage` | REAL | |
| 22 | `Fully Paid Postage` | `fully_paid_postage` | REAL | Retail/base rate |
| 23 | `Billing Amount` | `billing_amount` | REAL | Actual amount charged |
| 24 | `IMB Tracking Code` | `imb_tracking_code` | TEXT | |
| 25 | `SackLevel` | `sack_level` | TEXT | |
| 26 | `SackZip` | `sack_zip` | TEXT | |
| 27 | `DestinationEntryLevel` | `destination_entry_level` | TEXT | |
| 28 | `Zone` | `zone` | TEXT | USPS zone (1-8, or NONE) |
| 29 | `Irregular` | `irregular` | TEXT | |
| 30 | `Custom1` | `custom1` | TEXT | |
| 31 | `Custom2` | `custom2` | TEXT | |
| 32 | `Driver Route` | `driver_route` | TEXT | |
| 33 | `ADC` | `adc` | TEXT | |
| 34 | `Schemed 3D` | `schemed_3d` | TEXT | |
| 35 | `Schemed 5D` | `schemed_5d` | TEXT | |
| 36 | `Manifest Date` | `manifest_date` | TEXT | |
| 37 | `Length` | `length_in` | REAL | |
| 38 | `Width` | `width_in` | REAL | |
| 39 | `Height` | `height_in` | REAL | |
| 40 | `Girth` | `girth_in` | REAL | |
| 41 | `IsFlatRateConversion` | `is_flat_rate_conversion` | TEXT | |
| 42 | `Nonrectangular` | `nonrectangular` | TEXT | |
| 43 | `SubType` | `sub_type` | TEXT | |
| 44 | `OCR` | `ocr` | TEXT | |
| 45 | `BMC` | `bmc` | TEXT | |
| 46 | `ASF` | `asf` | TEXT | |
| 47 | `SCF` | `scf` | TEXT | |
| 48 | `Master Mail Class` | `master_mail_class` | TEXT | |
| 49 | `EZConfirm PIC` | `ezconfirm_pic` | TEXT | |
| 50 | `EZConfirm ProcessingType` | `ezconfirm_processing_type` | TEXT | |
| 51 | `EZConfirm Name` | `ezconfirm_name` | TEXT | |
| 52 | `EZConfirm Company` | `ezconfirm_company` | TEXT | |
| 53 | `EZConfirm Address1` | `ezconfirm_address1` | TEXT | |
| 54 | `EZConfirm Address2` | `ezconfirm_address2` | TEXT | |
| 55 | `EZConfirm City` | `ezconfirm_city` | TEXT | |
| 56 | `EZConfirm State` | `ezconfirm_state` | TEXT | |
| 57 | `EZConfirm Zip` | `ezconfirm_zip` | TEXT | |
| 58 | `EZConfirm Zip4` | `ezconfirm_zip4` | TEXT | |
| 59 | `EZConfirm RecordCaseNumber` | `ezconfirm_record_case_number` | TEXT | |
| 60 | `EZConfirm IsUploaded` | `ezconfirm_is_uploaded` | TEXT | |
| 61 | `WABCR Symbology1` | `wabcr_symbology1` | TEXT | |
| 62 | `WABCR Data1` | `wabcr_data1` | TEXT | |
| 63 | `WABCR Symbology2` | `wabcr_symbology2` | TEXT | |
| 64 | `WABCR Data2` | `wabcr_data2` | TEXT | |
| 65 | `WABCR Symbology3` | `wabcr_symbology3` | TEXT | |
| 66 | `WABCR Data3` | `wabcr_data3` | TEXT | |
| 67 | `WABCR Symbology4` | `wabcr_symbology4` | TEXT | |
| 68 | `WABCR Data4` | `wabcr_data4` | TEXT | |
| 69 | `WABCR Symbology5` | `wabcr_symbology5` | TEXT | |
| 70 | `WABCR Data5` | `wabcr_data5` | TEXT | |
| 71 | `Job Name` | `job_name` | TEXT | |
| 72 | `BillingID` | `billing_id_ref` | TEXT | **Deduplication key** |
| 73 | `Permit Origin` | `permit_origin` | TEXT | |
| 74 | `Permit Number` | `permit_number` | TEXT | |
| 75 | `Permit Name` | `permit_name` | TEXT | |
| 76 | `EZConfirm Special Services` | `ezconfirm_special_services` | TEXT | |
| 77 | `MailPieceTagData` | `mail_piece_tag_data` | TEXT | |
| 78 | `IsOpenAndDistribute` | `is_open_and_distribute` | TEXT | |
| 79 | `Payment Method` | `payment_method` | TEXT | |
| 80 | `Premeter Qual Level` | `premeter_qual_level` | TEXT | |
| 81 | `KeyLine` | `key_line` | TEXT | |
| 82 | `IMPB` | `impb` | TEXT | USPS tracking barcode |
| 83 | `EFN` | `efn` | TEXT | |
| 84 | `Surcharge Postage` | `surcharge_postage` | REAL | |
| 85 | `FSS` | `fss` | TEXT | |
| 86 | `TubNumber` | `tub_number` | TEXT | |
| 87 | `Postal Discounts` | `postal_discounts` | REAL | |
| 88 | `HRAddress` | `hr_address` | TEXT | Destination address |
| 89 | `HRCity` | `hr_city` | TEXT | |
| 90 | `HRState` | `hr_state` | TEXT | |
| 91 | `HRZip` | `hr_zip` | TEXT | |
| 92 | `LabelListInstallerVersion` | `label_list_installer_version` | TEXT | |
| 93 | `Is Move` | `is_move` | TEXT | |
| 94 | `Is Catalog` | `is_catalog` | TEXT | |

---

## Import Logic

### Helper Functions

```python
import csv, sqlite3, os
from datetime import datetime

def strip_zeros(value) -> int | None:
    """Strip leading zeros and convert to int. '0986' → 986, '' → None"""
    try:
        s = str(value).strip()
        if not s:
            return None
        return int(s.lstrip('0') or '0')
    except (ValueError, TypeError):
        return None

def safe_real(value) -> float | None:
    try:
        s = str(value).strip()
        return float(s) if s else None
    except (ValueError, TypeError):
        return None

def parse_timestamp(ts: str) -> datetime | None:
    """Parse '4/1/2026 15:34' → datetime object"""
    try:
        return datetime.strptime(ts.strip(), '%m/%d/%Y %H:%M')
    except (ValueError, AttributeError):
        return None
```

### Main Import Function

```python
def import_billing_csv(csv_path: str, db_path: str) -> dict:
    """
    Import a Copy_of_Export_Billing_*.csv file into postage.db.

    Returns: {
        'billing_id': str,
        'file_name': str,
        'rows_imported': int,
        'unmatched_accounts': list[int]
    }
    """
    file_name = os.path.basename(csv_path)

    # ── Read all rows, normalize headers ────────────────────────────────────
    with open(csv_path, encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        # Strip whitespace from every header name
        reader.fieldnames = [h.strip() for h in reader.fieldnames]
        rows = [{k.strip(): v.strip() for k, v in row.items()} for row in reader]

    if not rows:
        raise ValueError("CSV file is empty")

    # ── Extract BillingID from data (column index 72) ───────────────────────
    billing_id = rows[0].get('BillingID', '').strip()
    if not billing_id:
        raise ValueError("Cannot determine BillingID from file")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    # ── Deduplication ───────────────────────────────────────────────────────
    existing = cur.execute(
        "SELECT id FROM billing_imports WHERE billing_id = ?", (billing_id,)
    ).fetchone()
    if existing:
        cur.execute("DELETE FROM billing_imports WHERE billing_id = ?", (billing_id,))
        # CASCADE DELETE removes billing_records rows

    # ── Create import record ─────────────────────────────────────────────────
    cur.execute(
        "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES (?, ?, ?)",
        (billing_id, file_name, len(rows))
    )
    import_id = cur.lastrowid

    # ── Load valid customer numbers ──────────────────────────────────────────
    valid_customers = {
        r[0] for r in cur.execute("SELECT customer_number FROM customers").fetchall()
    }

    # ── Insert rows ─────────────────────────────────────────────────────────
    unmatched = set()
    inserted = 0

    for row in rows:
        cac = strip_zeros(row.get('Custom Account Code'))
        is_unmatched = 0 if (cac is not None and cac in valid_customers) else 1
        if is_unmatched and cac is not None:
            unmatched.add(cac)

        cur.execute("""
            INSERT INTO billing_records (
                billing_import_id,
                piece_id, machine_serial, time_stamp, weight_oz,
                handling_type, usps_mail_class, usps_mail_prep_type,
                routing_category, routing_string, bundle_qualification, bundle_zip,
                account_id, account_name, custom_account_code,
                customer_barcode_symbology, customer_barcode,
                department_id, department_name, manifest_id,
                piece_postage, lbs_postage, final_postage,
                fully_paid_postage, billing_amount, imb_tracking_code,
                sack_level, sack_zip, destination_entry_level, zone,
                irregular, custom1, custom2, driver_route, adc,
                schemed_3d, schemed_5d, manifest_date,
                length_in, width_in, height_in, girth_in,
                is_flat_rate_conversion, nonrectangular, sub_type,
                ocr, bmc, asf, scf, master_mail_class,
                ezconfirm_pic, ezconfirm_processing_type,
                ezconfirm_name, ezconfirm_company,
                ezconfirm_address1, ezconfirm_address2, ezconfirm_city,
                ezconfirm_state, ezconfirm_zip, ezconfirm_zip4,
                ezconfirm_record_case_number, ezconfirm_is_uploaded,
                wabcr_symbology1, wabcr_data1, wabcr_symbology2, wabcr_data2,
                wabcr_symbology3, wabcr_data3, wabcr_symbology4, wabcr_data4,
                wabcr_symbology5, wabcr_data5,
                job_name, billing_id_ref, permit_origin, permit_number, permit_name,
                ezconfirm_special_services, mail_piece_tag_data, is_open_and_distribute,
                payment_method, premeter_qual_level, key_line, impb, efn,
                surcharge_postage, fss, tub_number, postal_discounts,
                hr_address, hr_city, hr_state, hr_zip,
                label_list_installer_version, is_move, is_catalog,
                unmatched_account
            ) VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
        """, (
            import_id,
            row.get('Piece ID'),
            row.get('Machine Serial'),
            row.get('Time Stamp'),
            safe_real(row.get('Weight')),
            row.get('Handling Type'),
            row.get('USPS Mail Class'),
            row.get('USPS Mail Prep Type'),
            row.get('Routing Category'),
            row.get('Routing String'),
            row.get('Bundle Qualification'),
            row.get('Bundle Zip'),
            row.get('Account ID'),
            row.get('AccountName'),
            cac,
            row.get('Customer Barcode Symbology'),
            row.get('Customer Barcode'),
            row.get('Department ID'),
            row.get('Department Name'),
            row.get('Manifest ID'),
            safe_real(row.get('Piece Postage')),
            safe_real(row.get('LBS Postage')),
            safe_real(row.get('Final Postage')),
            safe_real(row.get('Fully Paid Postage')),
            safe_real(row.get('Billing Amount')),
            row.get('IMB Tracking Code'),
            row.get('SackLevel'),
            row.get('SackZip'),
            row.get('DestinationEntryLevel'),
            row.get('Zone'),
            row.get('Irregular'),
            row.get('Custom1'),
            row.get('Custom2'),
            row.get('Driver Route'),
            row.get('ADC'),
            row.get('Schemed 3D'),
            row.get('Schemed 5D'),
            row.get('Manifest Date'),
            safe_real(row.get('Length')),
            safe_real(row.get('Width')),
            safe_real(row.get('Height')),
            safe_real(row.get('Girth')),
            row.get('IsFlatRateConversion'),
            row.get('Nonrectangular'),
            row.get('SubType'),
            row.get('OCR'),
            row.get('BMC'),
            row.get('ASF'),
            row.get('SCF'),
            row.get('Master Mail Class'),
            row.get('EZConfirm PIC'),
            row.get('EZConfirm ProcessingType'),
            row.get('EZConfirm Name'),
            row.get('EZConfirm Company'),
            row.get('EZConfirm Address1'),
            row.get('EZConfirm Address2'),
            row.get('EZConfirm City'),
            row.get('EZConfirm State'),
            row.get('EZConfirm Zip'),
            row.get('EZConfirm Zip4'),
            row.get('EZConfirm RecordCaseNumber'),
            row.get('EZConfirm IsUploaded'),
            row.get('WABCR Symbology1'),
            row.get('WABCR Data1'),
            row.get('WABCR Symbology2'),
            row.get('WABCR Data2'),
            row.get('WABCR Symbology3'),
            row.get('WABCR Data3'),
            row.get('WABCR Symbology4'),
            row.get('WABCR Data4'),
            row.get('WABCR Symbology5'),
            row.get('WABCR Data5'),
            row.get('Job Name'),
            row.get('BillingID'),
            row.get('Permit Origin'),
            row.get('Permit Number'),
            row.get('Permit Name'),
            row.get('EZConfirm Special Services'),
            row.get('MailPieceTagData'),
            row.get('IsOpenAndDistribute'),
            row.get('Payment Method'),
            row.get('Premeter Qual Level'),
            row.get('KeyLine'),
            row.get('IMPB'),
            row.get('EFN'),
            safe_real(row.get('Surcharge Postage')),
            row.get('FSS'),
            row.get('TubNumber'),
            safe_real(row.get('Postal Discounts')),
            row.get('HRAddress'),
            row.get('HRCity'),
            row.get('HRState'),
            row.get('HRZip'),
            row.get('LabelListInstallerVersion'),
            row.get('Is Move'),
            row.get('Is Catalog'),
            is_unmatched,
        ))
        inserted += 1

    conn.commit()
    conn.close()

    return {
        'billing_id':         billing_id,
        'file_name':          file_name,
        'rows_imported':      inserted,
        'unmatched_accounts': sorted(unmatched),
    }
```

---

## Post-Import Validation

```python
def validate_billing_import(db_path: str, billing_id: str):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    imp = cur.execute(
        "SELECT id, file_name, row_count FROM billing_imports WHERE billing_id = ?",
        (billing_id,)
    ).fetchone()
    print(f"Import: id={imp[0]}, file={imp[1]}, row_count={imp[2]}")

    actual = cur.execute(
        "SELECT COUNT(*) FROM billing_records WHERE billing_import_id = ?", (imp[0],)
    ).fetchone()[0]
    print(f"Actual rows in billing_records: {actual}")

    unmatched = cur.execute("""
        SELECT custom_account_code, account_name, COUNT(*) as pieces
        FROM billing_records
        WHERE billing_import_id = ? AND unmatched_account = 1
        GROUP BY custom_account_code, account_name
        ORDER BY pieces DESC
    """, (imp[0],)).fetchall()

    if unmatched:
        print(f"Unmatched accounts ({len(unmatched)}):")
        for row in unmatched:
            print(f"  code={row[0]}, name={row[1]}, pieces={row[2]}")
    else:
        print("All account codes matched customers table")

    # Weight distribution check
    wt_dist = cur.execute("""
        SELECT
            MIN(CAST(CEIL(weight_oz / 16.0) AS INTEGER), 11) AS lb_bucket,
            COUNT(*) as pieces,
            SUM(billing_amount) as total_billed
        FROM billing_records
        WHERE billing_import_id = ? AND weight_oz > 0
        GROUP BY lb_bucket
        ORDER BY lb_bucket
    """, (imp[0],)).fetchall()
    print("Weight distribution (lbs bucket | pieces | total billed):")
    for row in wt_dist:
        label = f"{row[0]} lb" if row[0] <= 10 else "10+ lb"
        print(f"  {label}: {row[1]} pieces, ${row[2]:.2f}")

    conn.close()
```

---

## Date Filtering for Queries

The `time_stamp` column is stored as the original string `M/D/YYYY HH:MM`.
Filter in Python after fetching, or use SQLite string functions:

```python
# Python-side date filter (recommended — simpler and reliable)
from datetime import datetime, date

def billing_in_range(time_stamp: str, start: date, end: date) -> bool:
    dt = parse_timestamp(time_stamp)
    if dt is None:
        return False
    return start <= dt.date() <= end
```

```sql
-- SQL-side date filter (use when filtering before aggregation on large tables)
-- time_stamp format: 'M/D/YYYY HH:MM'
-- Extract date portion as 'YYYY-MM-DD' for comparison:
WHERE date(
    substr(time_stamp, -4, 4)                          -- year: last 4 chars
    || '-' ||
    printf('%02d',
        CAST(substr(time_stamp, 1, instr(time_stamp,'/')-1) AS INTEGER)
    )                                                   -- month
    || '-' ||
    printf('%02d',
        CAST(substr(
            substr(time_stamp, instr(time_stamp,'/')+1),
            1,
            instr(substr(time_stamp, instr(time_stamp,'/')+1), '/') - 1
        ) AS INTEGER)
    )                                                   -- day
) BETWEEN :start_date AND :end_date
```

---

## Weight Bucket Rules (Parcel Dashboard)

```python
import math

def weight_bucket_label(weight_oz: float) -> str:
    """
    Convert oz to lb bucket label for dashboard display.
    Rules:
      - weight_oz / 16 = lbs
      - Round UP to next whole pound (ceiling)
      - 1 lb through 10 lb get individual buckets
      - 11 lb and above → '10+ lb'
      - 0 or None → 'Unknown'
    """
    if not weight_oz:
        return 'Unknown'
    lbs_ceiled = math.ceil(weight_oz / 16.0)
    if lbs_ceiled <= 10:
        return f'{lbs_ceiled} lb'
    return '10+ lb'

def weight_bucket_int(weight_oz: float) -> int:
    """Return numeric bucket (1-10, 11=10+) for aggregation."""
    if not weight_oz:
        return 0
    return min(math.ceil(weight_oz / 16.0), 11)
```

Examples:
- 1 oz → ceil(0.0625) = 1 → `1 lb`
- 16 oz (1 lb exactly) → ceil(1.0) = 1 → `1 lb`
- 17 oz → ceil(1.0625) = 2 → `2 lb`
- 160 oz (10 lb) → ceil(10.0) = 10 → `10 lb`
- 161 oz → ceil(10.0625) = 11 → `10+ lb`
- 192 oz (12 lb) → ceil(12.0) = 12 → `10+ lb`

---

## Export Query (BC Priority format)

For generating the weekly billing export Excel file:

```sql
SELECT
    br.custom_account_code,
    br.account_name,
    COALESCE(c.parent_name, c.customer_name) AS parent_name,
    br.piece_id,
    br.time_stamp,
    br.usps_mail_class,
    br.zone,
    br.weight_oz,
    -- weight_lbs: compute as Excel formula =H/16 in the export, not here
    1                                          AS piece_count,
    br.fully_paid_postage,                    -- base/retail rate
    br.billing_amount,                        -- actual charged (EFD)
    -- savings: compute as Excel formula =K-L in the export, not here
    br.permit_name,
    br.department_name,
    br.handling_type
FROM billing_records br
LEFT JOIN customers c ON br.custom_account_code = c.customer_number
WHERE br.unmatched_account = 0   -- or include all: remove this line
ORDER BY br.time_stamp, br.custom_account_code, br.usps_mail_class;
-- Apply date filter in Python or via the SQL date expression above
```

Output columns map to Excel:
`A=custom_account_code, B=account_name, C=parent_name, D=piece_id, E=time_stamp,
F=usps_mail_class, G=zone, H=weight_oz, I=H/16 (formula), J=1 (count),
K=fully_paid_postage, L=billing_amount, M=K-L (formula), N=permit_name,
O=department_name, P=handling_type`
