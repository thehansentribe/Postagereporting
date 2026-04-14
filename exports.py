"""Excel exports: postage invoice and parcel (BC Priority) report."""

from __future__ import annotations

from collections import defaultdict
from functools import cmp_to_key
import os
import re
import sqlite3
import tempfile
from copy import copy
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import db

# USPS flats retail (1–13 oz) when flat_rate_costs is empty or a row has no rate.
# Sourced from FlatscostdataSavings.csv "Retail" column.
# Flats first-class mail classes included in invoice top table and cost-center totals (must stay in sync).
POSTAGE_INVOICE_FLAT_MAIL_CLASSES: tuple[str, ...] = (
    "1CA5DFlt",
    "1ClFlat",
    "1CSPiece",
    "1CNAPres",
    "1CAAADCL",
    "1CMAADCL",
    "1stClNMLtr",
)

POSTAGE_INVOICE_FLAT_MAIL_SQL_IN = (
    "(" + ",".join(f"'{c}'" for c in POSTAGE_INVOICE_FLAT_MAIL_CLASSES) + ")"
)

DEFAULT_FLAT_RETAIL_BY_OZ: dict[int, float] = {
    1: 1.63,
    2: 1.9,
    3: 2.17,
    4: 2.44,
    5: 2.72,
    6: 3.0,
    7: 3.28,
    8: 3.56,
    9: 3.84,
    10: 4.14,
    11: 4.44,
    12: 4.74,
    13: 5.04,
}


def _retail_rate(rates: dict[int, float], oz: int) -> float:
    v = float(rates.get(oz) or 0.0)
    if v > 0:
        return v
    return float(DEFAULT_FLAT_RETAIL_BY_OZ.get(oz, 0.0))


def _row_value(row: Any, key: str, default: str = "") -> str:
    """sqlite3.Row has no .get; read key safely for export rows."""
    try:
        v = row[key]
    except (KeyError, TypeError, IndexError):
        return default
    if v is None:
        return default
    s = str(v).strip()
    return s if s else default


def _apply_bold_preserve_font(cell) -> None:
    f = cell.font
    if f:
        nf = copy(f)
        nf.bold = True
        cell.font = nf
    else:
        cell.font = Font(bold=True)


CUSTOMER_CONTACTS: dict[int, dict[str, str]] = {
    3901: {
        "contact_name": "Chris Torrez",
        "address1": "1133 S.W. Topeka Blvd.",
        "city_state_zip": "Topeka, KS 66629-0001",
        "phone": "785-291-8681",
        "fax": "785-291-8548",
        "email": "Chris.Torrez@bcbsks.com",
        "customer_id": "            1st 0012",
    }
}


def _sheet_title_for_date(file_date: str) -> str:
    dt = datetime.strptime(file_date, "%Y-%m-%d")
    return f"{dt.strftime('%b')} {dt.day} {dt.year}"


def _invoice_range_sheet_title(start_date: str, end_date: str) -> str:
    """Workbook sheet name for a date range (max 31 chars for Excel)."""
    s = datetime.strptime(start_date, "%Y-%m-%d")
    e = datetime.strptime(end_date, "%Y-%m-%d")
    if start_date == end_date:
        return _sheet_title_for_date(start_date)[:31]
    label = f"{s.strftime('%b')} {s.day} – {e.strftime('%b')} {e.day} {e.year}"
    return label[:31]


def _efd_for_oz(rates: dict[int, float], oz: int, discount: float) -> float:
    return round(max(0.0, _retail_rate(rates, oz) - discount), 4)


def _cost_centers_flats_range(
    cur: sqlite3.Cursor,
    scope_range_sql: str,
    scope_range_params: list[Any],
    parent_number: int,
    customer_number: int | None,
    rates: dict[int, float],
    discount: float,
) -> list[dict[str, Any]]:
    """Per-customer flats (1–13 oz) in range: flat piece count, EFD charges, savings = pieces × discount."""
    flat_rows = cur.execute(
        f"""
        SELECT c.customer_number, c.customer_name,
               CAST(ROUND(p.weight_oz) AS INTEGER) AS woz,
               SUM(p.pieces) AS pieces
        FROM postage_data p
        JOIN customers c ON p.account_code = c.customer_number
        WHERE {scope_range_sql}
          AND p.weight_oz BETWEEN 1 AND 13
          AND p.mail_class IN {POSTAGE_INVOICE_FLAT_MAIL_SQL_IN}
        GROUP BY c.customer_number, c.customer_name, CAST(ROUND(p.weight_oz) AS INTEGER)
        """,
        scope_range_params,
    ).fetchall()

    agg: dict[int, dict[str, Any]] = defaultdict(
        lambda: {"customer_name": "", "by_oz": defaultdict(int)}
    )
    for row in flat_rows:
        cn = int(row["customer_number"])
        woz = int(row["woz"] or 0)
        if not (1 <= woz <= 13):
            continue
        pc = int(row["pieces"] or 0)
        agg[cn]["customer_name"] = _row_value(row, "customer_name")
        agg[cn]["by_oz"][woz] += pc

    cust_sql = """
        SELECT customer_number, customer_name FROM customers WHERE parent_number = ?
    """
    cust_params: list[Any] = [parent_number]
    if customer_number is not None:
        cust_sql += " AND customer_number = ?"
        cust_params.append(customer_number)
    cust_sql += """
        UNION ALL
        SELECT customer_number, customer_name FROM customers WHERE customer_number = ?
        ORDER BY customer_number
    """
    cust_params.append(parent_number)

    ordered = cur.execute(cust_sql, cust_params).fetchall()

    out: list[dict[str, Any]] = []
    for row in ordered:
        cn = int(row["customer_number"])
        name = _row_value(row, "customer_name")
        data = agg.get(cn)
        if not data:
            out.append(
                {
                    "customer_number": cn,
                    "customer_name": name,
                    "pieces": 0,
                    "cost": 0.0,
                    "savings": 0.0,
                }
            )
            continue
        by_oz = data["by_oz"]
        flat_pieces = sum(by_oz.values())
        efd_cost = 0.0
        for oz, pc in by_oz.items():
            efd_cost += _efd_for_oz(rates, int(oz), discount) * pc
        efd_cost = round(efd_cost, 2)
        savings = round(flat_pieces * discount, 2)
        out.append(
            {
                "customer_number": cn,
                "customer_name": data["customer_name"] or name,
                "pieces": flat_pieces,
                "cost": efd_cost,
                "savings": savings,
            }
        )
    return out


# Flats data grid XLSX — column keys/labels aligned with `postageTableColumns` in static/app.js.
def _flats_grid_header_keys_and_labels(hide_costs: bool) -> tuple[list[str], list[str]]:
    oz_keys = [f"oz_{i}" for i in range(13)] + ["oz_13", "oz_13plus"]
    keys: list[str] = [
        "date",
        "parent_name",
        "child_name",
        "mail_class",
        *oz_keys,
        "total_qty",
    ]
    labels: list[str] = [
        "Date",
        "Parent Name",
        "Child Name",
        "Class",
        *[f"{i} oz" for i in range(13)],
        "13 oz",
        "13+ oz",
        "Total Qty",
    ]
    if not hide_costs:
        keys.append("total_cost")
        labels.append("Total Cost")
    return keys, labels


_FLATS_GRID_ALL_KEYS = set(_flats_grid_header_keys_and_labels(False)[0])


def _flats_grid_cell_value(row: dict[str, Any], k: str) -> Any:
    v = row.get(k)
    if k == "total_cost":
        if v is None or v == "":
            return None
        return round(float(v), 2)
    if k.startswith("oz_") or k == "total_qty":
        return int(v or 0)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    return v if v is not None else ""


def _flats_grid_cmp(key: str, mul: int):
    """Comparator aligned with `sortRows` in static/app.js (nulls last for mul=1)."""

    def cmp(a: dict[str, Any], b: dict[str, Any]) -> int:
        va, vb = a.get(key), b.get(key)
        if va is None and vb is None:
            return 0
        if va is None:
            return 1 * mul
        if vb is None:
            return -1 * mul
        if (
            isinstance(va, (int, float))
            and not isinstance(va, bool)
            and isinstance(vb, (int, float))
            and not isinstance(vb, bool)
        ):
            if va < vb:
                return -mul
            if va > vb:
                return mul
            return 0
        sa, sb = str(va), str(vb)
        try:
            na, nb = float(sa), float(sb)
            if na < nb:
                return -mul
            if na > nb:
                return mul
            return 0
        except ValueError:
            if sa < sb:
                return -mul
            if sa > sb:
                return mul
            return 0

    return cmp


def _sort_flats_grid_rows(
    rows: list[dict[str, Any]], sort_key: str, sort_dir: int
) -> list[dict[str, Any]]:
    key = sort_key if sort_key in _FLATS_GRID_ALL_KEYS else "date"
    mul = 1 if sort_dir >= 0 else -1
    return sorted(rows, key=cmp_to_key(_flats_grid_cmp(key, mul)))


def export_flats_data_grid_xlsx(
    start_date: str,
    end_date: str,
    parent_number: int | None = None,
    customer_number: int | None = None,
    show_parents: bool = True,
    show_main: bool = True,
    consolidate: bool = False,
    remove_zeros: bool = False,
    hide_costs: bool = False,
    sort_key: str = "date",
    sort_dir: int = 1,
) -> Path:
    """
    One-sheet workbook: postage dashboard rows (same columns as the former flats CSV export).
    """
    conn = db.get_connection()
    try:
        data = db.query_postage(
            conn,
            start_date,
            end_date,
            parent_number=parent_number,
            customer_number=customer_number,
            show_parents=show_parents,
            show_main=show_main,
            consolidate=consolidate,
            remove_zeros=remove_zeros,
            hide_costs=hide_costs,
        )
        rows_raw = list(data.get("rows") or [])
        rows = _sort_flats_grid_rows(rows_raw, sort_key, sort_dir)

        header_keys, headers = _flats_grid_header_keys_and_labels(hide_costs)
        wb = Workbook()
        ws = wb.active
        ws.title = "Flats"

        INT_FMT = "#,##0"
        CURR_FMT = "$#,##0.00"
        bold = Font(bold=True)

        for col, h in enumerate(headers, start=1):
            c = ws.cell(1, col, h)
            c.font = bold

        oz_key_set = {k for k in header_keys if k.startswith("oz_")}
        for r_idx, row in enumerate(rows, start=2):
            for c_idx, k in enumerate(header_keys, start=1):
                val = _flats_grid_cell_value(row, k)
                cell = ws.cell(r_idx, c_idx, val)
                if k == "total_cost" and val is not None:
                    cell.number_format = CURR_FMT
                elif k in oz_key_set or k == "total_qty":
                    cell.number_format = INT_FMT

        for col in range(1, len(headers) + 1):
            ws.column_dimensions[get_column_letter(col)].width = min(18, 12 + (2 if col <= 4 else 0))

        out = Path(
            tempfile.mkstemp(
                suffix=f"_Flats_Invoice_{start_date}_{end_date}.xlsx",
                prefix="flats_grid_",
            )[1]
        )
        wb.save(out)
        return out
    finally:
        conn.close()


def export_postage_invoice(
    parent_number: int,
    start_date: str,
    end_date: str,
    discount: float = 0.10,
    customer_number: int | None = None,
    show_parents: bool = True,
    show_main: bool = True,
    remove_zeros: bool = False,
    hide_costs: bool = False,
    hide_savings: bool = False,
) -> Path:
    conn = db.get_connection()
    try:
        cur = conn.cursor()

        rates: dict[int, float] = {}
        for row in cur.execute(
            "SELECT weight_not_over_oz, rate_retail FROM flat_rate_costs ORDER BY weight_not_over_oz"
        ):
            rates[int(row["weight_not_over_oz"])] = float(row["rate_retail"] or 0)

        scope_range_sql, scope_range_params = db.postage_scope_where_clause(
            start_date,
            end_date,
            parent_number,
            customer_number,
            show_parents,
            show_main,
        )

        has_postage = cur.execute(
            f"""
            SELECT 1 FROM postage_data p
            JOIN customers c ON p.account_code = c.customer_number
            WHERE {scope_range_sql}
            LIMIT 1
            """,
            scope_range_params,
        ).fetchone()

        parent = cur.execute(
            "SELECT customer_name FROM customers WHERE customer_number = ?",
            (parent_number,),
        ).fetchone()
        parent_name = parent["customer_name"] if parent else f"Account {parent_number}"
        contact = CUSTOMER_CONTACTS.get(parent_number, {})

        wb = Workbook()
        wb.remove(wb.active)

        if not has_postage:
            ws = wb.create_sheet(title="No Data")
            ws["A1"] = "No postage data in range for this parent."
        else:
            weight_data: dict[int, dict[str, Any]] = {}
            for row in cur.execute(
                f"""
                SELECT p.weight_oz, SUM(p.pieces) AS pieces, SUM(p.total_cost) AS cost
                FROM postage_data p
                JOIN customers c ON p.account_code = c.customer_number
                WHERE {scope_range_sql}
                  AND p.weight_oz BETWEEN 1 AND 13
                  AND p.mail_class IN {POSTAGE_INVOICE_FLAT_MAIL_SQL_IN}
                GROUP BY p.weight_oz
                """,
                scope_range_params,
            ):
                woz = int(round(float(row["weight_oz"])))
                if not (1 <= woz <= 13):
                    continue
                pc = int(row["pieces"] or 0)
                tc = float(row["cost"] or 0.0)
                if woz not in weight_data:
                    weight_data[woz] = {"pieces": 0, "cost": 0.0}
                weight_data[woz]["pieces"] += pc
                weight_data[woz]["cost"] += tc

            other_row = cur.execute(
                f"""
                SELECT COALESCE(SUM(p.pieces), 0) AS pieces,
                       COALESCE(SUM(p.total_cost), 0) AS cost
                FROM postage_data p
                JOIN customers c ON p.account_code = c.customer_number
                WHERE {scope_range_sql}
                  AND (
                    (p.mail_class IS NULL OR p.mail_class NOT IN {POSTAGE_INVOICE_FLAT_MAIL_SQL_IN})
                    OR (
                      p.mail_class IN {POSTAGE_INVOICE_FLAT_MAIL_SQL_IN}
                      AND (
                        p.weight_oz IS NULL OR p.weight_oz < 1 OR p.weight_oz > 13
                      )
                    )
                  )
                """,
                scope_range_params,
            ).fetchone()
            other_pieces = int(other_row["pieces"] or 0)
            other_cost = float(other_row["cost"] or 0.0)

            children = _cost_centers_flats_range(
                cur,
                scope_range_sql,
                list(scope_range_params),
                parent_number,
                customer_number,
                rates,
                discount,
            )

            range_totals = db.query_postage(
                conn,
                start_date,
                end_date,
                parent_number,
                customer_number,
                show_parents,
                show_main,
                consolidate=False,
                remove_zeros=remove_zeros,
                hide_costs=False,
            )
            range_total_pieces = int(range_totals["total_pieces"] or 0)
            range_total_cost = float(range_totals.get("total_cost") or 0.0)

            sheet_title = _invoice_range_sheet_title(start_date, end_date)
            ws = wb.create_sheet(title=sheet_title)
            period_end = datetime.strptime(end_date, "%Y-%m-%d")
            reject_unit = db.get_presort_reject_unit_cost(conn)
            reject_count = db.query_ws3_presort_reject_count_for_invoice(
                conn,
                start_date,
                end_date,
                parent_number,
                customer_number,
                show_parents,
                show_main,
            )
            reject_line_cost = round(float(reject_count) * float(reject_unit), 2)

            _write_invoice_sheet(
                ws,
                1,
                period_end,
                start_date,
                end_date,
                parent_number,
                parent_name,
                contact,
                rates,
                weight_data,
                children,
                discount,
                other_pieces,
                other_cost,
                range_total_pieces,
                range_total_cost,
                reject_count=reject_count,
                reject_unit_cost=reject_unit,
                reject_line_cost=reject_line_cost,
                hide_costs=hide_costs,
                hide_savings=hide_savings,
            )

        out = Path(
            tempfile.mkstemp(
                suffix=f"_Postage_{parent_number}_{start_date}_{end_date}.xlsx",
                prefix="postage_",
            )[1]
        )
        wb.save(out)
        return out
    finally:
        conn.close()


def _redact_postage_invoice_privacy(
    ws,
    *,
    hide_costs: bool,
    hide_savings: bool,
    last_data_row: int,
    totals_row: int | None,
) -> None:
    """Remove currency from Costs/Savings columns per dashboard hide flags."""
    if hide_costs:
        for r in range(16, 34):
            ws.cell(r, 12, None)
            ws.cell(r, 13, None)
        ws.cell(34, 13, None)
        ws.cell(35, 12, None)
        ws.cell(35, 13, None)
        ws["K13"] = None
        ws["M13"] = None
        if last_data_row >= 37:
            for r in range(37, last_data_row + 1):
                ws.cell(r, 4, None)
                ws.cell(r, 5, None)
        if totals_row is not None:
            ws.cell(totals_row, 4, None)
            ws.cell(totals_row, 5, None)
        return

    if hide_savings:
        for r in range(16, 34):
            ws.cell(r, 13, None)
        ws.cell(34, 13, None)
        ws.cell(35, 13, None)
        if last_data_row >= 37:
            for r in range(37, last_data_row + 1):
                ws.cell(r, 5, None)
        if totals_row is not None:
            ws.cell(totals_row, 5, None)


def _write_invoice_sheet(
    ws,
    sheet_idx: int,
    file_date: datetime,
    start_date: str,
    end_date: str,
    parent_number: int,
    parent_name: str,
    contact: dict[str, str],
    rates: dict[int, float],
    weight_data: dict[int, dict[str, Any]],
    children: list,
    discount: float,
    other_pieces: int = 0,
    other_cost: float = 0.0,
    range_total_pieces: int = 0,
    range_total_cost: float = 0.0,
    reject_count: int = 0,
    reject_unit_cost: float = 0.66,
    reject_line_cost: float = 0.0,
    hide_costs: bool = False,
    hide_savings: bool = False,
) -> None:
    BOLD = Font(bold=True)
    CURR = "$#,##0.00"
    INT_FMT = "#,##0"

    ws["A1"] = "INVOICE # "
    ws["A1"].font = BOLD
    ws["C1"] = sheet_idx
    ws["C1"].font = BOLD
    ws["M2"] = "Week ending"
    ws["M2"].font = BOLD

    ws["A3"] = "Bill to: "
    ws["A3"].font = BOLD
    ws["C3"] = parent_name
    ws["C3"].font = BOLD
    ws["L3"] = "Project Date:"
    ws["L3"].font = BOLD
    ws["M3"] = "=F14"
    ws["M3"].font = BOLD

    ws["A4"] = "Attn:"
    ws["A4"].font = BOLD
    ws["C4"] = contact.get("contact_name", "")
    ws["C4"].font = BOLD
    ws["L4"] = "Customer ID#"
    ws["L4"].font = BOLD
    ws["M4"] = contact.get("customer_id", f"            {parent_number}")
    ws["M4"].font = BOLD

    ws["C5"] = contact.get("address1", "")
    ws["C6"] = contact.get("city_state_zip", "")
    ws["A8"] = "Phone #"
    ws["C8"] = contact.get("phone", "")
    ws["A9"] = "Fax#"
    ws["C9"] = contact.get("fax", "")
    ws["A10"] = "email:"
    ws["C10"] = contact.get("email", "")

    if start_date == end_date:
        period_note = start_date
    else:
        period_note = f"{start_date} to {end_date}"
    ws.cell(11, 9, f"Account Summary for {parent_name} ({period_note})").font = BOLD

    ws["D12"] = "Previous Acct. Balance"
    ws["D12"].font = BOLD
    ws["G12"] = "Funds  "
    ws["H12"] = "Deposit"
    ws["J12"] = "Funds  "
    ws["K12"] = " Used"
    ws["M12"] = "New Balance"
    for c in ("D12", "G12", "H12", "J12", "K12", "M12"):
        ws[c].font = BOLD

    ws["E13"] = 0
    ws["E13"].font = BOLD
    ws["G13"] = 0
    ws["G13"].font = BOLD
    ws["J13"] = "$"
    ws["K13"] = "=L35"
    ws["K13"].font = BOLD
    ws["M13"] = "=(E13+G13)-K13"
    ws["M13"].font = BOLD

    ws["A14"] = "2820 Roe Lane Bldg U "
    c_f14 = ws.cell(14, 6, file_date)
    c_f14.number_format = "MM/DD/YYYY"

    ws["A15"] = "Kansas City, KS 66103"
    ws["A17"] = "phone"
    ws["B17"] = "913-671-0011"
    ws["A18"] = "fax "
    ws["B18"] = "913-403-9919"
    ws["A19"] = "email "
    ws["B19"] = "efdmailing@aol.com"

    for col, val in [
        (6, "Weight"),
        (7, "1st Class"),
        (8, "FC discount"),
        (9, "Total #'s"),
        (10, "IMB rejects"),
        (11, "Total #'s"),
        (12, "Costs"),
        (13, "Savings"),
    ]:
        c = ws.cell(15, col, val)
        c.font = BOLD

    for oz in range(1, 14):
        r = 15 + oz
        retail = _retail_rate(rates, oz)
        efd = round(max(0.0, retail - discount), 4)
        pieces = int(weight_data.get(oz, {}).get("pieces", 0))

        ws.cell(r, 6, f"{oz} oz").font = BOLD
        ws.cell(r, 7, retail).number_format = CURR
        ws.cell(r, 7).font = BOLD
        ws.cell(r, 8, efd).number_format = CURR
        ws.cell(r, 8).font = BOLD
        ws.cell(r, 9, pieces).number_format = INT_FMT
        ws.cell(r, 9).font = BOLD
        ws.cell(r, 10, retail).number_format = CURR
        ws.cell(r, 10).font = BOLD
        ws.cell(r, 11, 0).number_format = INT_FMT
        ws.cell(r, 11).font = BOLD
        ws.cell(r, 12, f"=H{r}*I{r}+J{r}*K{r}").number_format = CURR
        ws.cell(r, 12).font = BOLD
        ws.cell(r, 13, f"=G{r}*I{r}+G{r}*K{r}-L{r}").number_format = CURR
        ws.cell(r, 13).font = BOLD

    r_reject = 29
    ru = round(float(reject_unit_cost), 4)
    rc = int(reject_count or 0)
    ws.cell(r_reject, 6, "Presort rejects").font = BOLD
    ws.cell(r_reject, 7, 0).number_format = CURR
    ws.cell(r_reject, 7).font = BOLD
    ws.cell(r_reject, 8, 0).number_format = CURR
    ws.cell(r_reject, 8).font = BOLD
    ws.cell(r_reject, 9, 0).number_format = INT_FMT
    ws.cell(r_reject, 9).font = BOLD
    ws.cell(r_reject, 10, ru).number_format = CURR
    ws.cell(r_reject, 10).font = BOLD
    ws.cell(r_reject, 11, rc).number_format = INT_FMT
    ws.cell(r_reject, 11).font = BOLD
    ws.cell(r_reject, 12, f"=H{r_reject}*I{r_reject}+J{r_reject}*K{r_reject}").number_format = CURR
    ws.cell(r_reject, 12).font = BOLD
    ws.cell(r_reject, 13, f"=G{r_reject}*I{r_reject}+G{r_reject}*K{r_reject}-L{r_reject}").number_format = (
        CURR
    )
    ws.cell(r_reject, 13).font = BOLD

    # Mail not in the 1–13 oz flat grid: non-flat classes and flat mail at 0 oz / 13+ oz (postage only; no discount savings).
    r_other = 30
    ws.cell(r_other, 6, "Letter").font = BOLD
    ws.cell(r_other, 7, 0).number_format = CURR
    ws.cell(r_other, 7).font = BOLD
    ws.cell(r_other, 8, 0).number_format = CURR
    ws.cell(r_other, 8).font = BOLD
    ws.cell(r_other, 9, int(other_pieces)).number_format = INT_FMT
    ws.cell(r_other, 9).font = BOLD
    ws.cell(r_other, 10, 0).number_format = CURR
    ws.cell(r_other, 10).font = BOLD
    ws.cell(r_other, 11, 0).number_format = INT_FMT
    ws.cell(r_other, 11).font = BOLD
    oc = round(float(other_cost), 2)
    ws.cell(r_other, 12, oc).number_format = CURR
    ws.cell(r_other, 12).font = BOLD
    ws.cell(r_other, 13, 0).number_format = CURR
    ws.cell(r_other, 13).font = BOLD

    for r in (31, 32):
        ws.cell(r, 6, "").font = BOLD
        for col in (7, 8, 10, 12, 13):
            ws.cell(r, col, 0).number_format = CURR
            ws.cell(r, col).font = BOLD
        ws.cell(r, 9, 0).number_format = INT_FMT
        ws.cell(r, 9).font = BOLD
        ws.cell(r, 11, 0).number_format = INT_FMT
        ws.cell(r, 11).font = BOLD

    ws.cell(33, 6, "Foreign").font = BOLD
    for col in (7, 8, 10):
        ws.cell(33, col, 0).number_format = CURR
        ws.cell(33, col).font = BOLD
    ws.cell(33, 9, 0).font = BOLD
    ws.cell(33, 9).number_format = INT_FMT
    ws.cell(33, 11, 0).number_format = INT_FMT
    ws.cell(33, 11).font = BOLD
    ws.cell(33, 12, 0).number_format = CURR
    ws.cell(33, 12).font = BOLD
    ws.cell(33, 13, 0).number_format = CURR
    ws.cell(33, 13).font = BOLD

    ws.cell(34, 9, "=SUM(I16:I33)").font = BOLD
    ws.cell(34, 11, "=SUM(K16:K33)").font = BOLD
    ws.cell(34, 13, "Total Savings").font = BOLD

    # Match dashboard postage totals; add presort reject charges (not in postage_data costs).
    rlc = round(float(reject_line_cost or 0.0), 2)
    invoice_total_cost = round(float(range_total_cost or 0.0) + rlc, 2)
    flat_pieces_total = sum(
        int(weight_data.get(oz, {}).get("pieces", 0) or 0) for oz in range(1, 14)
    )
    flat_savings = round(flat_pieces_total * discount, 2)
    invoice_total_savings = round(flat_savings - rlc, 2)

    ws.cell(35, 8, "Total Pieces").font = BOLD
    ws.cell(35, 9, range_total_pieces).number_format = INT_FMT
    ws.cell(35, 9).font = BOLD
    ws.cell(35, 11, "Total Cost:").font = BOLD
    ws.cell(35, 12, invoice_total_cost).number_format = CURR
    ws.cell(35, 12).font = BOLD
    ws.cell(35, 13, invoice_total_savings).number_format = CURR
    ws.cell(35, 13).font = BOLD

    for col, val in [
        (1, "Cost Centers "),
        (2, "CUSTOMER NAME"),
        (3, "# Pieces "),
        (4, "Charges "),
        (5, "Savings "),
    ]:
        ws.cell(36, col, val).font = BOLD

    child_list = list(children)
    if rc > 0:
        child_list.append(
            {
                "customer_number": "",
                "customer_name": "Presort rejects",
                "pieces": rc,
                "cost": rlc,
                "savings": -rlc,
            }
        )
    if int(other_pieces or 0) > 0 or abs(float(other_cost or 0.0)) > 1e-9:
        child_list.append(
            {
                "customer_number": "",
                "customer_name": "Letter",
                "pieces": int(other_pieces or 0),
                "cost": round(float(other_cost or 0.0), 2),
                "savings": 0.0,
            }
        )
    for i, child in enumerate(child_list):
        row = 37 + i
        pieces = int(child["pieces"] or 0)
        cost = round(float(child["cost"] or 0.0), 2)
        savings = round(float(child.get("savings", pieces * discount) or 0.0), 2)
        cn = child.get("customer_number")
        ws.cell(row, 1, cn if cn not in (None, "") else "—").font = BOLD
        ws.cell(row, 2, _row_value(child, "customer_name")).font = BOLD
        ws.cell(row, 3, pieces).number_format = INT_FMT
        ws.cell(row, 3).font = BOLD
        ws.cell(row, 4, cost).number_format = CURR
        ws.cell(row, 4).font = BOLD
        ws.cell(row, 5, savings).number_format = CURR
        ws.cell(row, 5).font = BOLD

    last_data_row = 36 + len(child_list) if child_list else 36

    t = last_data_row + 1
    totals_row: int | None = None
    if last_data_row >= 37:
        totals_row = t
        ws.cell(t, 1, "Totals").font = BOLD
        c = ws.cell(t, 3, f"=SUM(C37:C{last_data_row})")
        c.number_format = INT_FMT
        c.font = BOLD
        c = ws.cell(t, 4, f"=SUM(D37:D{last_data_row})")
        c.number_format = CURR
        c.font = BOLD
        c = ws.cell(t, 5, f"=SUM(E37:E{last_data_row})")
        c.number_format = CURR
        c.font = BOLD

    _redact_postage_invoice_privacy(
        ws,
        hide_costs=hide_costs,
        hide_savings=hide_savings,
        last_data_row=last_data_row,
        totals_row=totals_row,
    )

    for r in range(1, 16):
        for cell in ws[r]:
            if cell.value is not None:
                _apply_bold_preserve_font(cell)
    for r in range(16, 36):
        for cell in ws[r]:
            if cell.value is not None:
                _apply_bold_preserve_font(cell)

    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 32
    ws.column_dimensions["C"].width = 30
    ws.column_dimensions["D"].width = 10
    ws.column_dimensions["E"].width = 12
    ws.column_dimensions["F"].width = 10
    ws.column_dimensions["G"].width = 12
    ws.column_dimensions["H"].width = 14
    ws.column_dimensions["I"].width = 10
    ws.column_dimensions["J"].width = 13
    ws.column_dimensions["K"].width = 10
    ws.column_dimensions["L"].width = 12
    ws.column_dimensions["M"].width = 14


def parcel_report_scope_label(parent_number: int | None, customer_number: int | None) -> str:
    """Filename segment for parcel export: parent, optional customer, or ALL."""
    parts: list[str] = []
    if parent_number is not None:
        parts.append(str(parent_number))
    if customer_number is not None:
        parts.append(f"c{customer_number}")
    return "_".join(parts) if parts else "ALL"


def parcel_report_download_name(
    start_date: str,
    end_date: str,
    parent_number: int | None,
    customer_number: int | None,
) -> str:
    scope = parcel_report_scope_label(parent_number, customer_number)
    return f"Parcel_Report_{scope}_{start_date}_{end_date}.xlsx"


def parcel_counts_download_name(
    start_date: str,
    end_date: str,
    parent_number: int | None,
    customer_number: int | None,
) -> str:
    scope = parcel_report_scope_label(parent_number, customer_number)
    return f"Parcel_Counts_{scope}_{start_date}_{end_date}.xlsx"


_FILENAME_BAD_CHARS_RE = re.compile(r'[\\/:*?"<>|]+')


def _safe_filename_piece(s: str) -> str:
    # Keep spaces/parentheses/underscores for readability, but remove characters that break downloads.
    out = _FILENAME_BAD_CHARS_RE.sub("-", str(s))
    out = re.sub(r"\s+", " ", out).strip()
    return out or "Report"


def _short_mdy(date_str: str) -> str:
    """M-D-YYYY with no leading zeros; falls back to raw if parsing fails."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{d.month}-{d.day}-{d.year}"
    except ValueError:
        return str(date_str)


def parcel_invoice_download_name(*, title_name: str | None, parent_number: int | None, end_date: str) -> str:
    """
    Friendly Parcel Invoice download name.

    Example: "Security Benefit_Zinnia -EFD (3900) Parcel invoice 4-10-2026.xlsx"
    """
    raw_title = (title_name or "Parcel Invoice").strip()
    display_title = raw_title if raw_title.rstrip().endswith("-EFD") else f"{raw_title} -EFD"
    base = _safe_filename_piece(display_title)
    end_short = _safe_filename_piece(_short_mdy(end_date))
    if parent_number is None:
        return f"{base} Parcel invoice {end_short}.xlsx"
    return f"{base} ({int(parent_number)}) Parcel invoice {end_short}.xlsx"


def aggregate_parcel_count_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Roll up parcel grid rows to one line per date × parent × child (same as dashboard CSV)."""
    m: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in rows:
        key = (
            r.get("date"),
            r.get("parent_name"),
            r.get("parent_number"),
            r.get("child_name"),
            r.get("child_number"),
        )
        if key not in m:
            m[key] = {
                "date": key[0],
                "parent_name": key[1],
                "parent_number": key[2],
                "child_name": key[3],
                "child_number": key[4],
                **{f"lb_{i}": 0 for i in range(1, 11)},
                "lb_10plus": 0,
                "total_qty": 0,
                "total_billed": 0.0,
                "total_retail": 0.0,
            }
        a = m[key]
        for i in range(1, 11):
            a[f"lb_{i}"] += int(r.get(f"lb_{i}") or 0)
        a["lb_10plus"] += int(r.get("lb_10plus") or 0)
        a["total_qty"] += int(r.get("total_qty") or 0)
        a["total_billed"] += float(r.get("total_billed") or 0)
        a["total_retail"] += float(r.get("total_retail") or 0)

    def sort_key(x: dict[str, Any]) -> tuple[Any, ...]:
        def _n(v: Any) -> float:
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        return (
            str(x.get("date") or ""),
            str(x.get("parent_name") or "").lower(),
            _n(x.get("parent_number")),
            str(x.get("child_name") or "").lower(),
            _n(x.get("child_number")),
        )

    return sorted(m.values(), key=sort_key)


def export_parcel_counts_report_xlsx(
    start_date: str,
    end_date: str,
    parent_number: int | None,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
    consolidate: bool,
    remove_zeros: bool,
    hide_costs: bool,
) -> Path:
    """Formatted workbook: PARCELS (COUNTS) with weight buckets; last column = Retail Cost (when costs on).

    Uses ``.xlsx`` (Office Open XML) for currency formatting; Excel opens as native format.
    """
    conn = db.get_connection()
    try:
        data = db.query_parcels(
            conn,
            start_date,
            end_date,
            parent_number,
            customer_number,
            show_parents,
            show_main,
            consolidate,
            remove_zeros,
            hide_costs,
        )
    finally:
        conn.close()

    agg = aggregate_parcel_count_rows(data.get("rows") or [])

    wb = Workbook()
    ws = wb.active
    ws.title = "PARCELS (COUNTS)"[:31]

    hdr_font = Font(name="Calibri", bold=True, size=11, color="FFFFFF")
    hdr_fill = PatternFill("solid", start_color="1F4E79")
    body_font = Font(name="Calibri", size=11)
    thin = Side(style="thin", color="B4B4B4")
    grid = Border(left=thin, right=thin, top=thin, bottom=thin)
    money_fmt = "$#,##0.00"
    int_fmt = "#,##0"

    headers_text = [
        "Date",
        "Parent Name",
        "Child Name",
        *[f"{i} lb" for i in range(1, 11)],
        "10+ lb",
        "Total Qty",
    ]
    if not hide_costs:
        headers_text.append("Retail Cost")

    for col, h in enumerate(headers_text, start=1):
        c = ws.cell(1, col, h)
        c.font = hdr_font
        c.fill = hdr_fill
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = grid

    for ri, row in enumerate(agg, start=2):
        values: list[Any] = [
            row.get("date"),
            row.get("parent_name"),
            row.get("child_name"),
            *[int(row.get(f"lb_{i}") or 0) for i in range(1, 11)],
            int(row.get("lb_10plus") or 0),
            int(row.get("total_qty") or 0),
        ]
        if not hide_costs:
            values.extend(
                [
                    round(float(row.get("total_billed") or 0), 2),
                    round(float(row.get("total_retail") or 0), 2),
                ]
            )
        ncols = len(values)
        for col, v in enumerate(values, start=1):
            cell = ws.cell(ri, col, v)
            cell.font = body_font
            cell.border = grid
            if col <= 3:
                cell.alignment = Alignment(horizontal="left", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="right", vertical="center")
            if not hide_costs and col >= ncols - 1:
                cell.number_format = money_fmt
            elif col >= 4:
                cell.number_format = int_fmt

    ws.freeze_panes = "D2"
    ws.row_dimensions[1].height = 28

    col_widths = [12.0, 24.0, 30.0] + [9.0] * 10 + [9.0, 11.0]
    if not hide_costs:
        col_widths.append(14.0)
    for i, w in enumerate(col_widths, start=1):
        letter = get_column_letter(i)
        ws.column_dimensions[letter].width = max(ws.column_dimensions[letter].width or 0, w)

    fd, tmp = tempfile.mkstemp(suffix="_parcel_counts.xlsx", prefix="parcel_counts_")
    os.close(fd)
    out = Path(tmp)
    wb.save(out)
    return out


# Min column widths for stacked over-10lb (A–G) + parcel invoice (A–E) blocks; merged with zone-summary widths.
_AF_HM_MIN_WIDTHS: dict[int, float] = {
    1: 30.0,
    2: 30.0,
    3: 12.0,
    4: 14.0,
    5: 14.0,
    6: 14.0,
    7: 14.0,
}


def write_parcel_af_hm_sections(ws, section_start: int, sections: dict[str, Any]) -> None:
    """Over-10lb block (customer name in column A), optional totals row, then per-customer invoice + total."""
    sec_hdr = Font(name="Arial", bold=True, size=10)
    sec_fill = PatternFill("solid", start_color="E7E6E6")
    tot_font = Font(name="Arial", bold=True, size=10)
    cur_fmt = "$#,##0.00"
    int_fmt = "0"
    data_font = Font(name="Arial", size=10)

    hrows = sections.get("heavy_rows") or []
    crows = sections.get("customers") or []

    hdr_af = [
        "Customer Name",
        "Count",
        "lbs.",
        "Zone",
        "Base",
        "EFD",
        "Savings",
    ]
    hdr_hm = [
        "Customer #",
        "Customer Name",
        "Items (customer)",
        "Total cost",
        "Savings",
    ]

    cur_row = section_start
    if hrows:
        for col, h in enumerate(hdr_af, 1):
            c = ws.cell(cur_row, col, h)
            c.font = sec_hdr
            c.fill = sec_fill
            c.alignment = Alignment(horizontal="center")
        for i, hr in enumerate(hrows):
            r = cur_row + 1 + i
            ws.cell(r, 1, hr.get("customer_name") or "(no name)")
            ws.cell(r, 2, hr["count"])
            ws.cell(r, 3, hr["lbs"])
            ws.cell(r, 4, hr["zone"])
            ws.cell(r, 5, hr["base"]).number_format = cur_fmt
            ws.cell(r, 6, hr["efd"]).number_format = cur_fmt
            ws.cell(r, 7, hr["savings"]).number_format = cur_fmt
            for col in range(1, 8):
                cell = ws.cell(r, col)
                cell.font = data_font
                if col == 1:
                    cell.alignment = Alignment(horizontal="left", vertical="center")
                else:
                    cell.alignment = Alignment(horizontal="right", vertical="center")
            for col in (2, 3, 4):
                ws.cell(r, col).number_format = int_fmt
        first_h = cur_row + 1
        last_h = cur_row + len(hrows)
        tot_h = cur_row + 1 + len(hrows)
        ht = ws.cell(tot_h, 1, "Total")
        ht.font = tot_font
        ht.fill = sec_fill
        ht.alignment = Alignment(horizontal="left", vertical="center")
        hb = ws.cell(tot_h, 2, f"=SUM(B{first_h}:B{last_h})")
        hb.font = tot_font
        hb.fill = sec_fill
        hb.number_format = int_fmt
        hb.alignment = Alignment(horizontal="right", vertical="center")
        for col in (3, 4, 5):
            ws.cell(tot_h, col).fill = sec_fill
        hf = ws.cell(
            tot_h,
            6,
            f"=SUMPRODUCT(B{first_h}:B{last_h},F{first_h}:F{last_h})",
        )
        hf.font = tot_font
        hf.fill = sec_fill
        hf.number_format = cur_fmt
        hf.alignment = Alignment(horizontal="right", vertical="center")
        hg = ws.cell(tot_h, 7, f"=SUM(G{first_h}:G{last_h})")
        hg.font = tot_font
        hg.fill = sec_fill
        hg.number_format = cur_fmt
        hg.alignment = Alignment(horizontal="right", vertical="center")
        cur_row = tot_h + 2

    for col, h in enumerate(hdr_hm, 1):
        c = ws.cell(cur_row, col, h)
        c.font = sec_hdr
        c.fill = sec_fill
        c.alignment = Alignment(horizontal="center")

    for i, cr in enumerate(crows):
        r = cur_row + 1 + i
        ws.cell(r, 1, cr["customer_number"])
        ws.cell(r, 2, cr["name"])
        ws.cell(r, 3, cr["qty"])
        ws.cell(r, 4, cr["cost"]).number_format = cur_fmt
        ws.cell(r, 5, cr["savings"]).number_format = cur_fmt
        for col in range(1, 6):
            cell = ws.cell(r, col)
            cell.font = data_font
            if col in (1, 2):
                cell.alignment = Alignment(horizontal="left", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="right", vertical="center")
        ws.cell(r, 3).number_format = int_fmt

    tot_r = cur_row + 1 + len(crows)
    t = ws.cell(tot_r, 1, "Total")
    t.font = tot_font
    t.fill = sec_fill
    if crows:
        d1 = cur_row + 1
        d2 = cur_row + len(crows)
        c3 = ws.cell(tot_r, 3, f"=SUM(C{d1}:C{d2})")
        c3.font = tot_font
        c3.number_format = int_fmt
        c3.fill = sec_fill
        c4 = ws.cell(tot_r, 4, f"=SUM(D{d1}:D{d2})")
        c4.font = tot_font
        c4.number_format = cur_fmt
        c4.fill = sec_fill
        c5 = ws.cell(tot_r, 5, f"=SUM(E{d1}:E{d2})")
        c5.font = tot_font
        c5.number_format = cur_fmt
        c5.fill = sec_fill
    else:
        c3z = ws.cell(tot_r, 3, 0)
        c3z.font = tot_font
        c3z.number_format = int_fmt
        c3z.fill = sec_fill
        z4 = ws.cell(tot_r, 4, 0.0)
        z4.font = tot_font
        z4.number_format = cur_fmt
        z4.fill = sec_fill
        z5 = ws.cell(tot_r, 5, 0.0)
        z5.font = tot_font
        z5.number_format = cur_fmt
        z5.fill = sec_fill


def fill_parcel_report_worksheet(
    ws,
    start_date: str,
    end_date: str,
    parent_number: int | None,
    customer_number: int | None = None,
    show_parents: bool = True,
    show_main: bool = True,
) -> None:
    """Write parcel piece line-item table and a count total row only (no over-10lb / invoice blocks)."""
    conn = db.get_connection()
    try:
        filtered = db.query_parcel_report_rows(
            conn,
            start_date,
            end_date,
            parent_number,
            customer_number,
            show_parents,
            show_main,
        )
    finally:
        conn.close()

    # Last column is IMPB (billing CSV column CE / index 82); postage currency cols removed per spec.
    headers = [
        "Customer #",
        "Customer Name",
        "Parent Name",
        "Piece ID",
        "Time Stamp",
        "Mail Class",
        "Zone",
        "Weight (oz)",
        "Weight (lbs)",
        "Count",
        "Department",
        "Handling Type",
        "IMPB",
    ]
    header_fill = PatternFill("solid", start_color="BDD7EE")
    header_font = Font(name="Arial", bold=True, size=11)

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    data_font = Font(name="Arial", size=10)
    for i, row in enumerate(filtered, 2):
        ws.cell(i, 1, row["custom_account_code"])
        ws.cell(i, 2, row["account_name"])
        ws.cell(i, 3, row["parent_name"])
        ws.cell(i, 4, row["piece_id"])
        ws.cell(i, 5, row["time_stamp"])
        ws.cell(i, 6, row["usps_mail_class"])
        ws.cell(i, 7, row["zone"])
        ws.cell(i, 8, row["weight_oz"])
        ws.cell(i, 9, f"=H{i}/16")
        ws.cell(i, 10, 1)
        ws.cell(i, 11, row["department_name"])
        ws.cell(i, 12, row["handling_type"])
        ws.cell(i, 13, row["impb"] if row["impb"] is not None else "")
        ws.cell(i, 9).number_format = "0.000"
        for col in range(1, 14):
            ws.cell(i, col).font = data_font

    n = max(len(filtered) + 1, 1)
    tot = n + 1
    bold = Font(name="Arial", bold=True, size=11)
    top_border = Border(top=Side(style="thin"))
    ws.cell(tot, 1, "TOTALS").font = bold
    if len(filtered) == 0:
        c = ws.cell(tot, 10, 0)
        c.font = bold
        c.border = top_border
    else:
        c = ws.cell(tot, 10, f"=SUM(J2:J{n})")
        c.font = bold
        c.border = top_border

    widths = [12, 30, 30, 28, 18, 25, 6, 12, 12, 8, 25, 20, 42]
    for idx, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(idx)].width = w

    ws.freeze_panes = "A2"


def export_parcel_report(
    start_date: str,
    end_date: str,
    parent_number: int | None,
    customer_number: int | None = None,
    show_parents: bool = True,
    show_main: bool = True,
) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = f"{start_date} to {end_date}"[:31]
    fill_parcel_report_worksheet(
        ws,
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
    )
    scope = parcel_report_scope_label(parent_number, customer_number)
    out = Path(
        tempfile.mkstemp(
            suffix=f"_Parcel_{scope}_{start_date}_{end_date}.xlsx",
            prefix="parcel_",
        )[1]
    )
    wb.save(out)
    return out


# Stacked zone block uses columns A–I (Parcel Summary Final example.xlsx).
_PARCEL_SUMMARY_STACK_COL_WIDTHS: list[float] = [
    12.0,
    30.0,
    13.0,
    6.6640625,
    18.0,
    25.0,
    13.0,
    13.0,
    12.0,
]


def export_parcel_zone_summary_xlsx(
    summary: dict[str, Any],
    *,
    start_date: str,
    end_date: str,
    parent_number: int | None,
    customer_number: int | None = None,
    show_parents: bool = True,
    show_main: bool = True,
) -> Path:
    """Zone × weight workbook from `query_parcel_zone_summary` (CSV retail/EFD rates × DB quantities).

    **Sheet 1 — Parcel Invoice:** four zone pairs stacked vertically in columns **A–I**, then total
    cost/savings in **H–I**, then **over-10lb** and **per-customer invoice** blocks below.

    **Sheet 2 — Parcel Report:** line-item detail only (same as standalone parcel report export);
    tab title is the date range.
    """
    blocks = summary["blocks"]
    num_blocks = len(blocks)

    wb = Workbook()
    ws = wb.active
    ws.title = "Parcel Invoice"

    fill_prior = PatternFill("solid", start_color="D9E2F3")
    fill_efd = PatternFill("solid", start_color="D6F5F5")
    fill_cost = PatternFill("solid", start_color="F8CBAD")
    fill_save = PatternFill("solid", start_color="C6EFCE")
    fill_title = PatternFill("solid", start_color="2F5597")
    font_hdr = Font(name="Arial", bold=True, size=10)
    font_title = Font(name="Arial", bold=True, size=14, color="FFFFFF")
    font_tot_w = Font(name="Arial", bold=True, size=11, color="FFFFFF")
    font_date = Font(name="Calibri", size=11)
    font_body = Font(name="Calibri", size=11)
    font_tot_label = Font(name="Arial", bold=True, size=11)
    side = Side(style="thin", color="000000")
    grid = Border(left=side, right=side, top=side, bottom=side)
    cur_fmt = "$#,##0.00"
    int_fmt = "#,##0"

    raw_title = (summary.get("title_name") or "Parcel Invoice").strip()
    display_title = (
        raw_title if raw_title.rstrip().endswith("-EFD") else f"{raw_title} -EFD"
    )

    a1 = ws.cell(1, 1, summary.get("report_date") or "")
    a1.font = font_date
    ws.merge_cells("B1:I1")
    tcell = ws.cell(1, 2, display_title)
    tcell.font = font_title
    tcell.fill = fill_title
    tcell.alignment = Alignment(horizontal="center", vertical="center")

    ws.row_dimensions[1].height = 18.0

    hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    data_align = Alignment(horizontal="right", vertical="center")
    center_vert = Alignment(vertical="center")

    current_row = 2
    for bi, block in enumerate(blocks):
        ws.row_dimensions[current_row].height = 29.0
        za, zb = block["zone_a"], block["zone_b"]
        hdrs = [
            "Weight",
            f"Priority Zone {za}",
            f"EFD Price Zone {za}",
            "Count",
            f"Priority Zone {zb}",
            f"EFD Price Zone {zb}",
            "Count",
            "Costs",
            "Savings",
        ]
        for col_1, h in enumerate(hdrs, 1):
            cell = ws.cell(current_row, col_1, h)
            cell.font = font_hdr
            cell.border = grid
            cell.alignment = hdr_align
            rel = col_1 - 1
            if rel in (1, 4):
                cell.fill = fill_prior
            elif rel in (2, 5):
                cell.fill = fill_efd
            elif rel == 7:
                cell.fill = fill_cost
            elif rel == 8:
                cell.fill = fill_save

        for ri in range(10):
            r = current_row + 1 + ri
            ws.row_dimensions[r].height = 16.0
            rowrec = block["rows"][ri]
            a = rowrec["zone_a"]
            b = rowrec["zone_b"]
            vals: list[Any] = [
                rowrec["weight_label"],
                a.get("priority"),
                a.get("efd"),
                a.get("count"),
                b.get("priority"),
                b.get("efd"),
                b.get("count"),
                rowrec.get("costs"),
                rowrec.get("savings"),
            ]
            for col_1, v in enumerate(vals, 1):
                cell = ws.cell(r, col_1, v if v is not None else "")
                cell.font = font_body
                cell.border = grid
                rel = col_1 - 1
                if rel == 0:
                    cell.alignment = center_vert
                else:
                    cell.alignment = data_align
                if rel in (1, 4):
                    cell.fill = fill_prior
                elif rel in (2, 5):
                    cell.fill = fill_efd
                elif rel == 7:
                    cell.fill = fill_cost
                elif rel == 8:
                    cell.fill = fill_save
                if rel in (1, 2, 4, 5, 7, 8) and v is not None and v != "":
                    cell.number_format = cur_fmt
                if rel in (3, 6) and v is not None and v != "":
                    cell.number_format = int_fmt

        current_row += 11
        if bi < num_blocks - 1:
            current_row += 2

    tot_row = current_row + 1
    tp = summary.get("total_pieces", 0)
    m = ws.cell(tot_row, 2, f"Total Pieces: {tp:,}")
    m.alignment = Alignment(horizontal="center", vertical="center")
    m.font = font_tot_label
    m.border = grid

    tc = summary.get("total_cost")
    ts = summary.get("total_savings")
    cell_cost = ws.cell(tot_row, 8, tc if tc is not None else "")
    cell_cost.font = font_tot_w
    cell_cost.fill = PatternFill("solid", start_color="C00000")
    cell_cost.number_format = cur_fmt
    cell_cost.border = grid
    cell_cost.alignment = Alignment(horizontal="right", vertical="center")
    cell_save = ws.cell(tot_row, 9, ts if ts is not None else "")
    cell_save.font = font_tot_w
    cell_save.fill = PatternFill("solid", start_color="000000")
    cell_save.number_format = cur_fmt
    cell_save.border = grid
    cell_save.alignment = Alignment(horizontal="right", vertical="center")

    conn = db.get_connection()
    try:
        sections = db.compute_parcel_report_af_hm_sections(
            conn,
            start_date,
            end_date,
            parent_number,
            customer_number,
            show_parents,
            show_main,
        )
    finally:
        conn.close()
    section_start = tot_row + 3
    write_parcel_af_hm_sections(ws, section_start, sections)

    for col_1, w in enumerate(_PARCEL_SUMMARY_STACK_COL_WIDTHS, 1):
        letter = get_column_letter(col_1)
        cur = ws.column_dimensions[letter].width or 0
        ws.column_dimensions[letter].width = max(cur, w)
    for col in range(1, 10):
        letter = get_column_letter(col)
        w = ws.column_dimensions[letter].width or 0
        if col in _AF_HM_MIN_WIDTHS:
            ws.column_dimensions[letter].width = max(w, _AF_HM_MIN_WIDTHS[col])

    ws_report = wb.create_sheet(title=f"{start_date} to {end_date}"[:31])
    fill_parcel_report_worksheet(
        ws_report,
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
    )

    out = Path(
        tempfile.mkstemp(
            suffix="_parcel_zone_summary.xlsx",
            prefix="parcel_",
        )[1]
    )
    wb.save(out)
    return out
