"""File import logic: Pitney postage, parcel billing, customers, flat rates."""

from __future__ import annotations

import csv
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

# Column A: skip title / header rows (case-insensitive prefix).
SKIP_A_PREFIXES = (
    "pitney bowes",
    "dm weight break",
    "account code",
    "custom field",
    "report on working database",
    "business manager",
    "custom",
    "report",
    "business",
)


def _bm_skip_row_a(a: str) -> bool:
    if not a:
        return False
    low = a.lower()
    return any(low.startswith(p) for p in SKIP_A_PREFIXES)


def _bm_is_g_column_label_not_class_code(g: str) -> bool:
    """True when column G is the sheet header word 'Class', not a Pitney mail class."""
    return g.strip().lower() == "class"


def _bm_cell_float(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    t = str(val).strip().replace(",", "")
    if not t:
        return None
    try:
        return float(t)
    except (ValueError, TypeError):
        return None


def _bm_cell_int(val: Any) -> int:
    if val is None:
        return 0
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    t = str(val).strip().replace(",", "")
    if not t:
        return 0
    try:
        return int(float(t))
    except (ValueError, TypeError):
        return 0


def strip_zeros(value: Any) -> int | None:
    try:
        return int(str(value).strip().lstrip("0") or "0")
    except (ValueError, TypeError):
        return None


def parse_bm_date(filename: str) -> str:
    """
    BM_3_20_26_report.csv / BM_3_20_26.xls -> 2026-03-20
    BM 3.19.26.xls / BM 3_19_26.xls -> 2026-03-19
    """
    base = os.path.basename(filename)
    m = re.search(r"BM[_\s]+(\d+)[_.\s/-]+(\d+)[_.\s/-]+(\d+)", base, re.IGNORECASE)
    if not m:
        raise ValueError(f"Cannot parse date from filename: {filename}")
    month, day, year_2d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    year = 2000 + year_2d if year_2d < 100 else year_2d
    return date(year, month, day).isoformat()


def _find_libreoffice_executable() -> str | None:
    for cmd in ("libreoffice", "soffice"):
        path = shutil.which(cmd)
        if path:
            return path
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    for base in (pf, pf86):
        for exe in (
            os.path.join(base, "LibreOffice", "program", "soffice.exe"),
            os.path.join(base, "LibreOffice 5", "program", "soffice.exe"),
        ):
            if os.path.isfile(exe):
                return exe
    return None


def convert_xls_to_xlsx(xls_path: str, out_dir: str | None = None) -> str:
    lo = _find_libreoffice_executable()
    if not lo:
        raise RuntimeError(
            "LibreOffice required for XLS conversion. Install LibreOffice and ensure "
            "'soffice' or 'libreoffice' is on PATH."
        )
    out_dir = out_dir or tempfile.gettempdir()
    os.makedirs(out_dir, exist_ok=True)
    result = subprocess.run(
        [lo, "--headless", "--convert-to", "xlsx", "--outdir", out_dir, xls_path],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice conversion failed: {result.stderr or result.stdout}")
    base = os.path.basename(xls_path).replace(".xls", ".xlsx").replace(".XLS", ".xlsx")
    xlsx_path = os.path.join(out_dir, base)
    if not os.path.exists(xlsx_path):
        raise FileNotFoundError(f"Expected output not found: {xlsx_path}")
    return xlsx_path


def parse_bm_xlsx(xlsx_path: str) -> list[dict[str, Any]]:
    wb = load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb.active
    rows_out: list[dict[str, Any]] = []
    current_account: str | None = None
    current_class: str | None = None

    for row in ws.iter_rows(values_only=False):
        cells = {cell.column_letter: cell.value for cell in row if cell.value is not None}

        a = str(cells.get("A", "") or "").strip()
        g = str(cells.get("G", "") or "").strip()
        j = str(cells.get("J", "") or "").strip()
        m = cells.get("M")
        p = cells.get("P")
        r = cells.get("R")

        if _bm_skip_row_a(a):
            continue
        if j == "Sub Total":
            continue
        if a and re.match(r"^\d{4}$", a):
            current_account = a
            current_class = None
            continue
        if g:
            if _bm_is_g_column_label_not_class_code(g):
                continue
            current_class = g
            continue
        if current_account is None:
            continue
        weight = _bm_cell_float(m)
        if weight is None:
            continue
        pieces = _bm_cell_int(p)
        cost = _bm_cell_float(r) or 0.0
        rows_out.append(
            {
                "account_code": current_account,
                "mail_class": current_class or "UNKNOWN",
                "weight_oz": weight,
                "pieces": pieces,
                "total_cost": round(cost, 3),
            }
        )

    wb.close()
    return rows_out


def parse_bm_raw_csv(csv_path: str) -> list[dict[str, Any]]:
    """Parse Pitney Bowes raw BM export CSV (class G, subtotals J, weight M, pieces P, cost R).

    Pitney CSV uses the same layout as XLSX: weight is column M (index 12), not N (often empty).
    Numeric cells may include thousands separators (e.g. \"1,120.0\", \"5,786\").
    """
    rows_out: list[dict[str, Any]] = []
    current_account: str | None = None
    current_class: str | None = None

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            while len(row) < 18:
                row.append("")

            a = row[0].strip()
            g = row[6].strip()
            j = row[9].strip()
            m_raw = row[12]
            p_raw = row[15]
            r_raw = row[17]

            if _bm_skip_row_a(a):
                continue
            if j == "Sub Total":
                continue
            if a and re.match(r"^\d{4}$", a):
                current_account = a
                current_class = None
                continue
            if g:
                if _bm_is_g_column_label_not_class_code(g):
                    continue
                current_class = g
                continue
            if current_account is None:
                continue
            weight = _bm_cell_float(m_raw)
            if weight is None:
                continue
            pieces = _bm_cell_int(p_raw)
            cost = _bm_cell_float(r_raw) or 0.0
            rows_out.append(
                {
                    "account_code": current_account,
                    "mail_class": current_class or "UNKNOWN",
                    "weight_oz": weight,
                    "pieces": pieces,
                    "total_cost": round(cost, 3),
                }
            )

    return rows_out


def write_report_csv(rows: list[dict[str, Any]], source_path: str, out_dir: str) -> str:
    base = os.path.basename(source_path)
    csv_name = re.sub(r"\.(xlsx|xls|csv)$", "_report.csv", base, flags=re.IGNORECASE)
    if not csv_name.endswith("_report.csv"):
        csv_name = base + "_report.csv"
    out_path = os.path.join(out_dir, csv_name)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Account Code", "Class", "Weight  (oz.)", "Pieces", "Total Cost"])
        for row in rows:
            w.writerow(
                [
                    row["account_code"],
                    row["mail_class"],
                    row["weight_oz"],
                    row["pieces"],
                    f"{row['total_cost']:.3f}",
                ]
            )
    return out_path


def import_bm_csv(csv_path: str, db_path: Path | str) -> dict[str, Any]:
    file_name = os.path.basename(csv_path)
    file_date = parse_bm_date(file_name)

    data_rows: list[dict[str, str]] = []
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            data_rows.append(row)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    existing = cur.execute(
        "SELECT id FROM postage_imports WHERE file_name = ?", (file_name,)
    ).fetchone()
    if existing:
        cur.execute("DELETE FROM postage_imports WHERE file_name = ?", (file_name,))

    cur.execute(
        "INSERT INTO postage_imports (file_name, file_date, row_count) VALUES (?, ?, ?)",
        (file_name, file_date, len(data_rows)),
    )
    import_id = cur.lastrowid

    valid_accounts = {r[0] for r in cur.execute("SELECT customer_number FROM customers").fetchall()}

    unmatched: set[int] = set()
    inserted = 0
    for row in data_rows:
        account_code = strip_zeros(row.get("Account Code", ""))
        if account_code is None:
            continue
        mail_class = str(row.get("Class", "")).strip()
        try:
            weight_oz = float(row.get("Weight  (oz.)", 0) or 0)
            pieces = int(row.get("Pieces", 0) or 0)
            total_cost = float(row.get("Total Cost", 0) or 0)
        except (ValueError, TypeError):
            continue

        is_unmatched = 0 if account_code in valid_accounts else 1
        if is_unmatched:
            unmatched.add(account_code)

        try:
            cur.execute(
                """
                INSERT INTO postage_data
                    (import_id, file_date, account_code, mail_class,
                     weight_oz, pieces, total_cost, unmatched_account)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    import_id,
                    file_date,
                    account_code,
                    mail_class,
                    weight_oz,
                    pieces,
                    total_cost,
                    is_unmatched,
                ),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            pass

    conn.commit()
    conn.close()

    return {
        "file_name": file_name,
        "file_date": file_date,
        "rows_imported": inserted,
        "unmatched": sorted(unmatched),
    }


def process_bm_file(xls_path: str, db_path: Path | str, csv_out_dir: str | None = None) -> dict[str, Any]:
    csv_out_dir = csv_out_dir or os.path.dirname(xls_path)
    low = xls_path.lower()
    if low.endswith(".csv"):
        rows = parse_bm_raw_csv(xls_path)
    elif low.endswith(".xls") and not low.endswith(".xlsx"):
        xlsx_path = convert_xls_to_xlsx(xls_path)
        rows = parse_bm_xlsx(xlsx_path)
    else:
        rows = parse_bm_xlsx(xls_path)
    csv_path = write_report_csv(rows, xls_path, csv_out_dir)
    return import_bm_csv(csv_path, db_path)


def safe_real(value: Any) -> float | None:
    try:
        s = str(value).strip()
        return float(s) if s else None
    except (ValueError, TypeError):
        return None


def import_billing_csv(csv_path: str, db_path: Path | str) -> dict[str, Any]:
    file_name = os.path.basename(csv_path)

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [h.strip() for h in (reader.fieldnames or [])]
        rows = []
        for row in reader:
            rows.append({(k or "").strip(): (v or "").strip() if isinstance(v, str) else v for k, v in row.items()})

    if not rows:
        raise ValueError("CSV file is empty")

    billing_id = str(rows[0].get("BillingID", "") or "").strip()
    if not billing_id:
        raise ValueError("Cannot determine BillingID from file")

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    existing = cur.execute(
        "SELECT id FROM billing_imports WHERE billing_id = ?", (billing_id,)
    ).fetchone()
    if existing:
        cur.execute("DELETE FROM billing_imports WHERE billing_id = ?", (billing_id,))

    cur.execute(
        "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES (?, ?, ?)",
        (billing_id, file_name, len(rows)),
    )
    import_id = cur.lastrowid

    valid_customers = {r[0] for r in cur.execute("SELECT customer_number FROM customers").fetchall()}
    unmatched: set[int] = set()
    inserted = 0

    _cols = 97
    insert_sql = (
        """
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
        ) VALUES ("""
        + ",".join(["?"] * _cols)
        + ")"
    )

    for row in rows:
        cac = strip_zeros(row.get("Custom Account Code"))
        is_unmatched = 0 if (cac is not None and cac in valid_customers) else 1
        if is_unmatched and cac is not None:
            unmatched.add(cac)

        cur.execute(
            insert_sql,
            (
                import_id,
                row.get("Piece ID"),
                row.get("Machine Serial"),
                row.get("Time Stamp"),
                safe_real(row.get("Weight")),
                row.get("Handling Type"),
                row.get("USPS Mail Class"),
                row.get("USPS Mail Prep Type"),
                row.get("Routing Category"),
                row.get("Routing String"),
                row.get("Bundle Qualification"),
                row.get("Bundle Zip"),
                row.get("Account ID"),
                row.get("AccountName"),
                cac,
                row.get("Customer Barcode Symbology"),
                row.get("Customer Barcode"),
                row.get("Department ID"),
                row.get("Department Name"),
                row.get("Manifest ID"),
                safe_real(row.get("Piece Postage")),
                safe_real(row.get("LBS Postage")),
                safe_real(row.get("Final Postage")),
                safe_real(row.get("Fully Paid Postage")),
                safe_real(row.get("Billing Amount")),
                row.get("IMB Tracking Code"),
                row.get("SackLevel"),
                row.get("SackZip"),
                row.get("DestinationEntryLevel"),
                row.get("Zone"),
                row.get("Irregular"),
                row.get("Custom1"),
                row.get("Custom2"),
                row.get("Driver Route"),
                row.get("ADC"),
                row.get("Schemed 3D"),
                row.get("Schemed 5D"),
                row.get("Manifest Date"),
                safe_real(row.get("Length")),
                safe_real(row.get("Width")),
                safe_real(row.get("Height")),
                safe_real(row.get("Girth")),
                row.get("IsFlatRateConversion"),
                row.get("Nonrectangular"),
                row.get("SubType"),
                row.get("OCR"),
                row.get("BMC"),
                row.get("ASF"),
                row.get("SCF"),
                row.get("Master Mail Class"),
                row.get("EZConfirm PIC"),
                row.get("EZConfirm ProcessingType"),
                row.get("EZConfirm Name"),
                row.get("EZConfirm Company"),
                row.get("EZConfirm Address1"),
                row.get("EZConfirm Address2"),
                row.get("EZConfirm City"),
                row.get("EZConfirm State"),
                row.get("EZConfirm Zip"),
                row.get("EZConfirm Zip4"),
                row.get("EZConfirm RecordCaseNumber"),
                row.get("EZConfirm IsUploaded"),
                row.get("WABCR Symbology1"),
                row.get("WABCR Data1"),
                row.get("WABCR Symbology2"),
                row.get("WABCR Data2"),
                row.get("WABCR Symbology3"),
                row.get("WABCR Data3"),
                row.get("WABCR Symbology4"),
                row.get("WABCR Data4"),
                row.get("WABCR Symbology5"),
                row.get("WABCR Data5"),
                row.get("Job Name"),
                row.get("BillingID"),
                row.get("Permit Origin"),
                row.get("Permit Number"),
                row.get("Permit Name"),
                row.get("EZConfirm Special Services"),
                row.get("MailPieceTagData"),
                row.get("IsOpenAndDistribute"),
                row.get("Payment Method"),
                row.get("Premeter Qual Level"),
                row.get("KeyLine"),
                row.get("IMPB"),
                row.get("EFN"),
                safe_real(row.get("Surcharge Postage")),
                row.get("FSS"),
                row.get("TubNumber"),
                safe_real(row.get("Postal Discounts")),
                row.get("HRAddress"),
                row.get("HRCity"),
                row.get("HRState"),
                row.get("HRZip"),
                row.get("LabelListInstallerVersion"),
                row.get("Is Move"),
                row.get("Is Catalog"),
                is_unmatched,
            ),
        )
        inserted += 1

    conn.commit()
    conn.close()

    return {
        "billing_id": billing_id,
        "file_name": file_name,
        "rows_imported": inserted,
        "unmatched_accounts": sorted(unmatched),
    }


def import_customers_csv(csv_path: str, db_path: Path | str) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    # Parent account rows often appear after children in the source CSV; allow any order.
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = conn.cursor()
    cur.execute("DELETE FROM customers")

    warnings: list[str] = []
    count = 0
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("Customer") or row.get("customer") or "").strip()
            code = strip_zeros(row.get("Customer Code") or row.get("Customer Code "))
            parent_name = (row.get("Parent Co") or "").strip()
            parent_num_raw = row.get("Parent Co Number") or row.get("Parent Co Number ")
            parent_num = strip_zeros(parent_num_raw) if str(parent_num_raw or "").strip() else None

            if code is None:
                continue
            if not parent_name and not str(parent_num_raw or "").strip():
                pnum, pname = None, None
            elif parent_name and parent_num is not None:
                pname, pnum = parent_name, parent_num
            elif parent_name and parent_num is None:
                warnings.append(f"Parent name without number for customer {code}: {parent_name}")
                pname, pnum = parent_name, None
            else:
                pname, pnum = None, parent_num

            cur.execute(
                """
                INSERT INTO customers (customer_number, customer_name, parent_number, parent_name)
                VALUES (?, ?, ?, ?)
                """,
                (code, name or f"Account {code}", pnum, pname),
            )
            count += 1

    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.close()
    return {"rows_imported": count, "warnings": warnings}


def import_flat_rate_costs(csv_path: str, db_path: Path | str) -> dict[str, Any]:
    def parse_rate(val: Any) -> float | None:
        try:
            return float(str(val).replace("$", "").strip())
        except (ValueError, TypeError):
            return None

    rows: list[dict[str, Any]] = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fn = [h.strip().replace("\n", " ") for h in (reader.fieldnames or [])]
        reader.fieldnames = fn
        for row in reader:
            norm = {(k or "").strip().replace("\n", " "): v for k, v in row.items()}
            rows.append(
                {
                    "weight_not_over_oz": float(str(norm.get("Weight Not Over (oz.)", "0")).strip()),
                    "rate_5digit": parse_rate(norm.get("5-Digit")),
                    "rate_3digit": parse_rate(norm.get("3 digit")),
                    "rate_aadc": parse_rate(norm.get("AADC")),
                    "rate_mixed_adc": parse_rate(norm.get("Mixed ADC")),
                    "rate_machinable_pres": parse_rate(
                        norm.get("Machinable Presorted") or norm.get("Machinable\nPresorted")
                    ),
                    "rate_retail": parse_rate(norm.get("Retail Cost")),
                }
            )

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()
    cur.execute("DELETE FROM flat_rate_costs")
    cur.executemany(
        """
        INSERT INTO flat_rate_costs
            (weight_not_over_oz, rate_5digit, rate_3digit, rate_aadc,
             rate_mixed_adc, rate_machinable_pres, rate_retail)
        VALUES
            (:weight_not_over_oz, :rate_5digit, :rate_3digit, :rate_aadc,
             :rate_mixed_adc, :rate_machinable_pres, :rate_retail)
        """,
        rows,
    )
    conn.commit()
    conn.close()
    return {"rows_imported": len(rows)}
