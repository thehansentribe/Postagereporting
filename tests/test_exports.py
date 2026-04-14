"""Tests for export helpers (parcel roll-up, etc.)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import db
import exports
import exports_consolidated_volumes
from openpyxl import load_workbook


def _parcel_row(
    *,
    date: str = "2026-04-03",
    parent_name: str = "P",
    parent_number: int = 1,
    child_name: str = "C",
    child_number: int = 10,
    lb_1: int = 0,
    lb_2: int = 0,
    total_qty: int = 0,
    total_billed: float = 0.0,
    total_retail: float = 0.0,
) -> dict:
    return {
        "date": date,
        "parent_name": parent_name,
        "parent_number": parent_number,
        "child_name": child_name,
        "child_number": child_number,
        "lb_1": lb_1,
        "lb_2": lb_2,
        "lb_3": 0,
        "lb_4": 0,
        "lb_5": 0,
        "lb_6": 0,
        "lb_7": 0,
        "lb_8": 0,
        "lb_9": 0,
        "lb_10": 0,
        "lb_10plus": 0,
        "total_qty": total_qty,
        "total_billed": total_billed,
        "total_retail": total_retail,
    }


def test_aggregate_parcel_count_rows_sums_costs_across_split_rows() -> None:
    """API returns one row per mail class × zone; roll-up must sum billed and retail."""
    rows = [
        _parcel_row(lb_1=1, total_qty=1, total_billed=5.0, total_retail=10.0),
        _parcel_row(lb_2=1, total_qty=1, total_billed=3.0, total_retail=7.0),
    ]
    agg = exports.aggregate_parcel_count_rows(rows)
    assert len(agg) == 1
    a = agg[0]
    assert a["lb_1"] == 1
    assert a["lb_2"] == 1
    assert a["total_qty"] == 2
    assert abs(a["total_billed"] - 8.0) < 1e-9
    assert abs(a["total_retail"] - 17.0) < 1e-9


def test_parcel_counts_download_name() -> None:
    assert "Parcel_Counts" in exports.parcel_counts_download_name("a", "b", None, None)
    assert exports.parcel_counts_download_name("2026-01-01", "2026-01-31", 12, None).endswith(
        ".xlsx"
    )


def test_parcel_invoice_download_name_uses_title_customer_and_end_date() -> None:
    n = exports.parcel_invoice_download_name(
        title_name="Security Benefit_Zinnia",
        parent_number=3900,
        end_date="2026-04-10",
    )
    assert n == "Security Benefit_Zinnia -EFD (3900) Parcel invoice 4-10-2026.xlsx"


def test_consolidated_volumes_download_name() -> None:
    n = exports_consolidated_volumes.consolidated_volumes_download_name("KC Presort (3906)", "2026-04-07")
    assert n.startswith("KC Presort (3906) 4-7-2026")
    assert n.endswith(".xlsx")


def test_export_flats_data_grid_xlsx_headers_and_piece_count(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "flats_grid.db"
        monkeypatch.setattr(db, "DB_PATH", p)
        db.init_db()
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO customers (customer_number, customer_name) VALUES (200, 'Acme')"
        )
        conn.execute(
            "INSERT INTO postage_imports (file_name, file_date, row_count) VALUES ('x.csv', '2026-03-01', 1)"
        )
        conn.execute(
            """
            INSERT INTO postage_data (
                import_id, file_date, account_code, mail_class,
                weight_oz, pieces, total_cost, unmatched_account
            ) VALUES (1, '2026-03-01', 200, '1ClFlat', 2.0, 5, 10.0, 0)
            """
        )
        conn.commit()
        conn.close()

        out = exports.export_flats_data_grid_xlsx(
            "2026-03-01",
            "2026-03-01",
            hide_costs=False,
            sort_key="date",
            sort_dir=1,
        )
        assert out.is_file()
        wb = load_workbook(out)
        ws = wb.active
        assert ws.title == "Flats"
        assert ws.cell(1, 1).value == "Date"
        assert ws.cell(1, 4).value == "Class"
        assert ws.cell(1, 20).value == "Total Qty"
        assert ws.cell(1, 21).value == "Total Cost"
        # Col 7 = oz_2 (date, parent, child, class, oz_0..oz_1, oz_2)
        assert ws.cell(2, 7).value == 5
        assert ws.cell(2, 20).value == 5
        assert ws.cell(2, 21).value == 10.0
        out.unlink(missing_ok=True)


def test_total_presort_reject_pieces_from_postage_rows() -> None:
    rows = [
        {"mail_class": "1ClFlat", "total_qty": 100},
        {"mail_class": db.WS3_REJECT_MAIL_CLASS, "total_qty": 5},
        {"mail_class": db.WS3_REJECT_MAIL_CLASS, "total_qty": 3},
    ]
    assert exports_consolidated_volumes.total_presort_reject_pieces_from_postage_rows(rows) == 8
    assert exports_consolidated_volumes.total_presort_reject_pieces_from_postage_rows([]) == 0
