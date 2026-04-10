"""Consolidated volumes Excel export (flats + parcels + over-10lb + zone summary)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import db
from exports import aggregate_parcel_count_rows


def consolidated_volumes_download_name(start_date: str, end_date: str) -> str:
    return f"volumes_flats_parcels_{start_date}_{end_date}.xlsx"


def _find_block_side_for_zone(blocks: list[dict[str, Any]], z: int) -> tuple[dict[str, Any], str] | None:
    for b in blocks:
        if b.get("zone_a") == z:
            return (b, "a")
        if b.get("zone_b") == z:
            return (b, "b")
    return None


def _zone_cell_priority_count(blocks: list[dict[str, Any]], z: int, ri: int) -> tuple[Any, int]:
    hit = _find_block_side_for_zone(blocks, z)
    if not hit:
        return None, 0
    block, side = hit
    rw = block["rows"][ri]
    cell = rw["zone_a"] if side == "a" else rw["zone_b"]
    pr = cell.get("priority")
    cnt = int(cell.get("count") or 0)
    return pr, cnt


def _flats_consolidated_column_plan() -> tuple[list[str], list[str]]:
    oz_keys = [f"oz_{i}" for i in range(13)] + ["oz_13", "oz_13plus"]
    headers = [
        "Date",
        "Parent Name",
        "Child Name",
        "Class",
        *[f"{i} oz" for i in range(13)],
        "13 oz",
        "13+ oz",
        "Total Qty",
        "Total Cost",
    ]
    keys = ["date", "parent_name", "child_name", "mail_class", *oz_keys, "total_qty", "total_cost"]
    return headers, keys


def _write_zone_summary_stacked_sheet(ws: Any, zone_data: dict[str, Any], *, start_row: int = 1) -> int:
    hide = zone_data.get("hide_costs") is True
    blocks = zone_data.get("blocks") or []
    if not blocks:
        c = ws.cell(start_row, 1, "No zone summary data.")
        c.font = Font(name="Calibri", size=11)
        return start_row + 1

    row_indices = [i for i in range(10) if i != 8]
    hdr_font = Font(name="Calibri", bold=True, size=11, color="FFFFFF")
    hdr_fill = PatternFill("solid", start_color="2E5090")
    sec_font = Font(name="Calibri", bold=True, size=12)
    body = Font(name="Calibri", size=11)
    thin = Side(style="thin", color="B4B4B4")
    grid = Border(left=thin, right=thin, top=thin, bottom=thin)
    money_fmt = "$#,##0.00"
    int_fmt = "#,##0"

    r = start_row
    for z in range(1, 9):
        if _find_block_side_for_zone(blocks, z) is None:
            continue
        if r > start_row:
            r += 1
        title = ws.cell(r, 1, f"Zone {z}")
        title.font = sec_font
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
        r += 1
        hdrs = ["Weight", f"Priority Z{z}", f"Count Z{z}", "Line total"]
        for c, h in enumerate(hdrs, start=1):
            cell = ws.cell(r, c, h)
            cell.font = hdr_font
            cell.fill = hdr_fill
            cell.border = grid
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        r += 1
        for ri in row_indices:
            wl = blocks[0]["rows"][ri]["weight_label"]
            pri, cnt = _zone_cell_priority_count(blocks, z, ri)
            line_val: float | None = None
            if not hide and pri is not None:
                line_val = round(float(pri) * cnt, 2)
            c1 = ws.cell(r, 1, wl)
            c1.font = body
            c1.border = grid
            c1.alignment = Alignment(horizontal="left", vertical="center")
            pcell = ws.cell(r, 2)
            if not hide and pri is not None:
                pcell.value = float(pri)
                pcell.number_format = money_fmt
            pcell.font = body
            pcell.border = grid
            pcell.alignment = Alignment(horizontal="right", vertical="center")
            ccell = ws.cell(r, 3, cnt)
            ccell.font = body
            ccell.border = grid
            ccell.number_format = int_fmt
            ccell.alignment = Alignment(horizontal="right", vertical="center")
            lcell = ws.cell(r, 4)
            if line_val is not None:
                lcell.value = line_val
                lcell.number_format = money_fmt
            lcell.font = body
            lcell.border = grid
            lcell.alignment = Alignment(horizontal="right", vertical="center")
            r += 1

    for col, w in enumerate([14.0, 14.0, 12.0, 14.0], start=1):
        letter = get_column_letter(col)
        ws.column_dimensions[letter].width = max(ws.column_dimensions[letter].width or 0, w)
    return r


def export_consolidated_volumes_xlsx(
    start_date: str,
    end_date: str,
    parent_number: int | None,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
    consolidate: bool,
    remove_zeros: bool,
    hide_costs_summary: bool,
    account_scope_label: str,
) -> Path:
    conn = db.get_connection()
    try:
        postage = db.query_postage(
            conn,
            start_date,
            end_date,
            parent_number,
            customer_number,
            show_parents,
            show_main,
            consolidate,
            remove_zeros,
            hide_costs=False,
        )
        parcels = db.query_parcels(
            conn,
            start_date,
            end_date,
            parent_number,
            customer_number,
            show_parents,
            show_main,
            consolidate,
            remove_zeros,
            hide_costs=False,
        )
        heavy = db.query_parcel_over_10lb_lines(
            conn,
            start_date,
            end_date,
            parent_number,
            customer_number,
            show_parents,
            show_main,
        )
        zone = db.query_parcel_zone_summary(
            conn,
            start_date,
            end_date,
            parent_number,
            customer_number,
            show_parents,
            show_main,
            hide_costs=False,
        )
    finally:
        conn.close()

    flat_rows = postage.get("rows") or []
    parcel_agg = aggregate_parcel_count_rows(parcels.get("rows") or [])
    zone_pieces = int(zone.get("total_pieces") or 0)

    if not flat_rows and not parcel_agg and not heavy and zone_pieces == 0:
        raise ValueError("No data for the selected date range and filters.")

    hdr_font = Font(name="Calibri", bold=True, size=11, color="FFFFFF")
    hdr_fill = PatternFill("solid", start_color="1F4E79")
    body_font = Font(name="Calibri", size=11)
    label_font = Font(name="Calibri", bold=True, size=11)
    thin = Side(style="thin", color="B4B4B4")
    grid = Border(left=thin, right=thin, top=thin, bottom=thin)
    money_fmt = "$#,##0.00"
    int_fmt = "#,##0"

    wb = Workbook()
    ws_sum = wb.active
    ws_sum.title = "Summary"

    ws_sum.merge_cells("A1:D1")
    t = ws_sum.cell(1, 1, "Consolidated volumes report")
    t.font = Font(name="Calibri", bold=True, size=16, color="FFFFFF")
    t.fill = PatternFill("solid", start_color="1F4E79")
    t.alignment = Alignment(horizontal="center", vertical="center")
    ws_sum.row_dimensions[1].height = 28

    def kv_row(row: int, label: str, value: Any, *, money: bool = False, is_int: bool = False) -> None:
        a = ws_sum.cell(row, 1, label)
        a.font = label_font
        a.alignment = Alignment(horizontal="left", vertical="center")
        b = ws_sum.cell(row, 2, value)
        b.font = body_font
        b.alignment = Alignment(horizontal="left", vertical="center")
        if money and value is not None:
            b.number_format = money_fmt
        if is_int:
            b.number_format = int_fmt

    r = 3
    kv_row(r, "Start date", start_date)
    r += 1
    kv_row(r, "End date", end_date)
    r += 1
    kv_row(r, "Account scope", account_scope_label)
    r += 1
    kv_row(r, "Total flats pieces (postage)", int(postage.get("total_pieces") or 0), is_int=True)
    r += 1
    kv_row(r, "Total parcel pieces", int(parcels.get("total_pieces") or 0), is_int=True)
    r += 1
    combined = int(postage.get("total_pieces") or 0) + int(parcels.get("total_pieces") or 0)
    kv_row(r, "Combined volume", combined, is_int=True)
    r += 1
    if not hide_costs_summary and postage.get("total_cost") is not None:
        kv_row(r, "Flats retail", round(float(postage["total_cost"]), 2), money=True)
        r += 1
    if parcels.get("total_retail") is not None:
        kv_row(r, "Total parcel retail", round(float(parcels["total_retail"]), 2), money=True)
        r += 1

    ws_sum.column_dimensions["A"].width = 28.0
    ws_sum.column_dimensions["B"].width = 36.0

    if flat_rows:
        ws_f = wb.create_sheet("FLATS (POSTAGE)")
        f_headers, f_keys = _flats_consolidated_column_plan()
        for col, h in enumerate(f_headers, start=1):
            c = ws_f.cell(1, col, h)
            c.font = hdr_font
            c.fill = hdr_fill
            c.border = grid
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for ri, row in enumerate(flat_rows, start=2):
            for col, k in enumerate(f_keys, start=1):
                v = row.get(k)
                cell = ws_f.cell(ri, col)
                cell.font = body_font
                cell.border = grid
                if k == "total_cost":
                    if v is not None:
                        cell.value = round(float(v), 2)
                        cell.number_format = money_fmt
                    cell.alignment = Alignment(horizontal="right", vertical="center")
                elif k.startswith("oz_") or k == "total_qty":
                    cell.value = int(v or 0)
                    cell.number_format = int_fmt
                    cell.alignment = Alignment(horizontal="right", vertical="center")
                else:
                    cell.value = v
                    cell.alignment = Alignment(horizontal="left", vertical="center")
        ws_f.freeze_panes = "E2"
        ws_f.row_dimensions[1].height = 26
        for i in range(1, len(f_headers) + 1):
            letter = get_column_letter(i)
            base = 10.0 if i > 4 else (14.0 if i == 1 else 22.0)
            ws_f.column_dimensions[letter].width = max(ws_f.column_dimensions[letter].width or 0, base)

    if parcel_agg:
        ws_p = wb.create_sheet("PARCELS (COUNTS)")
        p_headers = [
            "Date",
            "Parent Name",
            "Child Name",
            *[f"{i} lb" for i in range(1, 11)],
            "10+ lb",
            "Total Qty",
            "Retail Cost",
        ]
        for col, h in enumerate(p_headers, start=1):
            c = ws_p.cell(1, col, h)
            c.font = hdr_font
            c.fill = hdr_fill
            c.border = grid
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for ri, prow in enumerate(parcel_agg, start=2):
            vals: list[Any] = [
                prow.get("date"),
                prow.get("parent_name"),
                prow.get("child_name"),
                *[int(prow.get(f"lb_{i}") or 0) for i in range(1, 11)],
                int(prow.get("lb_10plus") or 0),
                int(prow.get("total_qty") or 0),
                round(float(prow.get("total_retail") or 0), 2),
            ]
            ncols = len(vals)
            for col, v in enumerate(vals, start=1):
                cell = ws_p.cell(ri, col, v)
                cell.font = body_font
                cell.border = grid
                if col <= 3:
                    cell.alignment = Alignment(horizontal="left", vertical="center")
                else:
                    cell.alignment = Alignment(horizontal="right", vertical="center")
                if col == ncols:
                    cell.number_format = money_fmt
                elif col >= 4:
                    cell.number_format = int_fmt
        ws_p.freeze_panes = "D2"
        ws_p.row_dimensions[1].height = 28
        col_widths = [12.0, 24.0, 30.0] + [9.0] * 10 + [9.0, 11.0, 14.0]
        for i, w in enumerate(col_widths, start=1):
            letter = get_column_letter(i)
            ws_p.column_dimensions[letter].width = max(ws_p.column_dimensions[letter].width or 0, w)

    if heavy:
        ws_h = wb.create_sheet("Over 10 lb")
        hz = Font(name="Calibri", bold=True, size=11, color="FFFFFF")
        hz_fill = PatternFill("solid", start_color="375623")
        sorted_h = sorted(
            heavy,
            key=lambda x: (
                int(x.get("zone") or 0),
                str(x.get("child_name") or "").lower(),
                int(x.get("lbs") or 0),
            ),
        )
        r = 1
        prev_z: int | None = None
        for row in sorted_h:
            z = row.get("zone")
            z_int = int(z) if z is not None else 0
            if z_int != prev_z:
                if prev_z is not None:
                    r += 1
                ws_h.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
                zt = ws_h.cell(r, 1, f"Zone {z}")
                zt.font = hz
                zt.fill = hz_fill
                zt.alignment = Alignment(horizontal="left", vertical="center")
                r += 1
                for c, h in enumerate(["Child name", "lbs", "Zone", "Cost (base)"], start=1):
                    hc = ws_h.cell(r, c, h)
                    hc.font = hdr_font
                    hc.fill = hdr_fill
                    hc.border = grid
                    hc.alignment = Alignment(horizontal="center", vertical="center")
                r += 1
                prev_z = z_int
            c1 = ws_h.cell(r, 1, row.get("child_name") or "")
            c1.font = body_font
            c1.border = grid
            c1.alignment = Alignment(horizontal="left", vertical="center")
            c2 = ws_h.cell(r, 2, int(row.get("lbs") or 0))
            c2.font = body_font
            c2.border = grid
            c2.number_format = int_fmt
            c2.alignment = Alignment(horizontal="right", vertical="center")
            c3 = ws_h.cell(r, 3, z)
            c3.font = body_font
            c3.border = grid
            c3.alignment = Alignment(horizontal="right", vertical="center")
            bc = ws_h.cell(r, 4, round(float(row.get("base") or 0), 2))
            bc.number_format = money_fmt
            bc.font = body_font
            bc.border = grid
            bc.alignment = Alignment(horizontal="right", vertical="center")
            r += 1
        for col, w in enumerate([32.0, 10.0, 10.0, 14.0], start=1):
            letter = get_column_letter(col)
            ws_h.column_dimensions[letter].width = max(ws_h.column_dimensions[letter].width or 0, w)

    if zone_pieces > 0 and (zone.get("blocks") or []):
        ws_z = wb.create_sheet("Zone summary")
        ws_z.merge_cells("A1:D1")
        zt = ws_z.cell(1, 1, "Parcel zone summary (Priority × count by zone)")
        zt.font = Font(name="Calibri", bold=True, size=13, color="FFFFFF")
        zt.fill = PatternFill("solid", start_color="2F5597")
        zt.alignment = Alignment(horizontal="center", vertical="center")
        ws_z.row_dimensions[1].height = 24
        _write_zone_summary_stacked_sheet(ws_z, zone, start_row=3)
        ws_z.freeze_panes = "A3"

    fd, tmp = tempfile.mkstemp(suffix="_consolidated_volumes.xlsx", prefix="volumes_")
    os.close(fd)
    out = Path(tmp)
    wb.save(out)
    return out
