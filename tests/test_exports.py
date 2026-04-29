"""Tests for export helpers (parcel roll-up, etc.)."""

from __future__ import annotations

import csv
import tempfile
from pathlib import Path

import pytest

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


def test_allocate_integer_proportional_exact_split() -> None:
    assert exports.allocate_integer_proportional(10, [1.0, 2.0, 3.0, 4.0]) == [1, 2, 3, 4]
    assert sum(exports.allocate_integer_proportional(10, [1.0, 2.0, 3.0, 4.0])) == 10


def test_allocate_integer_proportional_largest_remainder() -> None:
    out = exports.allocate_integer_proportional(7, [1.0, 1.0, 1.0])
    assert sum(out) == 7
    assert max(out) - min(out) <= 1


def test_allocate_integer_proportional_zero_total() -> None:
    assert exports.allocate_integer_proportional(0, [3.0, 5.0]) == [0, 0]


def test_allocate_integer_proportional_zero_weights_splits_evenly() -> None:
    out = exports.allocate_integer_proportional(5, [0.0, 0.0])
    assert out == [3, 2]
    assert sum(out) == 5


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


def test_export_flats_data_grid_csv_headers_and_hide_costs(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "flats_grid_csv.db"
        monkeypatch.setattr(db, "DB_PATH", p)
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO customers (customer_number, customer_name) VALUES (200, 'Acme')")
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

        out = exports.export_flats_data_grid_csv(
            "2026-03-01",
            "2026-03-01",
            hide_costs=False,
            sort_key="date",
            sort_dir=1,
        )
        assert out.is_file()
        with open(out, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader)
            row = next(reader)
        assert header[0:4] == ["Date", "Parent Name", "Child Name", "Class"]
        assert "Total Qty" in header
        assert header[-1] == "Total Cost"
        assert len(row) == len(header)
        out.unlink(missing_ok=True)

        out2 = exports.export_flats_data_grid_csv(
            "2026-03-01",
            "2026-03-01",
            hide_costs=True,
            sort_key="date",
            sort_dir=1,
        )
        with open(out2, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header2 = next(reader)
            row2 = next(reader)
        assert header2[0:4] == ["Date", "Parent Name", "Child Name", "Class"]
        assert "Total Qty" in header2
        assert "Total Cost" not in header2
        assert len(row2) == len(header2)
        out2.unlink(missing_ok=True)


def test_total_flats_retail_invoice_basis() -> None:
    rates = {2: 1.9, 3: 2.17}
    row_fl = {"mail_class": "1CA5DFlt", "oz_2": 10, "oz_3": 2}
    row_pr = {"mail_class": db.WS3_REJECT_MAIL_CLASS, "oz_2": 99}
    row_parcel = {"mail_class": "Priority", "oz_2": 5}
    total = exports_consolidated_volumes.total_flats_retail_invoice_basis(
        [row_fl, row_pr, row_parcel], rates
    )
    assert total == pytest.approx(10 * 1.9 + 2 * 2.17, rel=1e-9)


def test_total_flats_retail_invoice_basis() -> None:
    rates = {2: 1.9, 3: 2.17}
    row_fl = {"mail_class": "1CA5DFlt", "oz_2": 10, "oz_3": 2}
    row_pr = {"mail_class": db.WS3_REJECT_MAIL_CLASS, "oz_2": 99}
    row_parcel = {"mail_class": "Priority", "oz_2": 5}
    total = exports_consolidated_volumes.total_flats_retail_invoice_basis(
        [row_fl, row_pr, row_parcel], rates
    )
    assert total == pytest.approx(10 * 1.9 + 2 * 2.17, rel=1e-9)


def test_total_presort_reject_pieces_from_postage_rows() -> None:
    rows = [
        {"mail_class": "1ClFlat", "total_qty": 100},
        {"mail_class": db.WS3_REJECT_MAIL_CLASS, "total_qty": 5},
        {"mail_class": db.WS3_REJECT_MAIL_CLASS, "total_qty": 3},
    ]
    assert exports_consolidated_volumes.total_presort_reject_pieces_from_postage_rows(rows) == 8
    assert exports_consolidated_volumes.total_presort_reject_pieces_from_postage_rows([]) == 0


def test_export_parcel_billing_csv_writes_full_header(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "billing.db"
        monkeypatch.setattr(db, "DB_PATH", p)
        db.init_db()

        conn = db.get_connection()
        conn.execute(
            "INSERT INTO customers (customer_number, customer_name) VALUES (200, 'Acme')"
        )
        conn.execute(
            "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('b1', 'x.csv', 1)"
        )
        import_id = conn.execute(
            "SELECT id FROM billing_imports WHERE billing_id = 'b1'"
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO billing_records (billing_import_id, custom_account_code, time_stamp, weight_oz)
            VALUES (?, ?, ?, ?)
            """,
            (import_id, 200, "4/1/2026 15:34", 16.0),
        )
        conn.commit()
        conn.close()

        out = exports.export_parcel_billing_csv(
            "2026-04-01",
            "2026-04-01",
            200,
            None,
            show_parents=True,
            show_main=True,
        )
        assert out.is_file()
        with open(out, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader)
            row = next(reader)
        assert header[0:2] == ["parent_name", "parent_number"]
        assert "custom_account_code" in header
        assert "time_stamp" in header
        assert "weight_oz" in header
        assert "retail_cost" in header
        assert header.index("retail_cost") == header.index("billing_amount") + 1
        assert len(row) == len(header)
        out.unlink(missing_ok=True)


def test_efd_parcel_invoice_download_name() -> None:
    n = exports.efd_parcel_invoice_download_name(
        "Blue Cross -EFD",
        parent_number=3901,
        customer_number=None,
        end_date="2026-04-13",
    )
    assert n == "Blue Cross -EFD (3901) 4-13-2026.xlsx"
    n2 = exports.efd_parcel_invoice_download_name(
        "Child Co",
        parent_number=None,
        customer_number=2256,
        end_date="2026-04-01",
    )
    assert "(2256)" in n2 and n2.endswith(".xlsx")
    n3 = exports.efd_parcel_invoice_download_name(
        "All Accounts",
        parent_number=None,
        customer_number=None,
        end_date="2026-01-31",
    )
    assert "(ALL)" in n3


def test_export_efd_parcel_invoice_xlsx_layout_and_formulas(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "efd_invoice.db"
        monkeypatch.setattr(db, "DB_PATH", p)
        db.init_db()

        conn = db.get_connection()
        conn.execute(
            "INSERT INTO customers (customer_number, customer_name) VALUES (200, 'Acme')"
        )
        conn.execute(
            "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('b1', 'x.csv', 1)"
        )
        import_id = conn.execute(
            "SELECT id FROM billing_imports WHERE billing_id = 'b1'"
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO billing_records (
                billing_import_id, custom_account_code, time_stamp, weight_oz,
                billing_amount, zone, imb_tracking_code, impb
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (import_id, 200, "4/1/2026 15:34", 16.0, 5.22, "5", "trk", "impb1"),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(
            db,
            "compute_retail_cost_for_piece",
            lambda *a, **k: {"retail": 10.2, "zone": 5},
        )

        out, title = exports.export_efd_parcel_invoice_xlsx(
            "2026-04-01",
            "2026-04-30",
            200,
            None,
            show_parents=True,
            show_main=True,
        )
        assert title == "Acme"
        assert out.is_file()
        wb = load_workbook(out, data_only=False)
        ws = wb.active
        assert ws.cell(1, 1).value == "Acme"
        assert ws.cell(1, 2).value == "4-1-2026"
        assert ws.cell(1, 3).value == "4-30-2026"
        assert ws.cell(2, 1).value == "Parcel Cost"
        assert ws.cell(6, 1).value == "Total parcel quantities"
        assert ws.cell(9, 24).value == "billing_amount"
        assert ws.cell(9, 25).value == "Price to EFD"
        assert ws.cell(9, 26).value == "EFD Revenue"
        assert ws.cell(9, 27).value == "retail_cost"
        assert ws.cell(10, 24).value == 5.22
        assert ws.cell(10, 25).value == "=X10+0.5"
        assert ws.cell(10, 26).value == "=AA10-0.25-Y10"
        assert ws.cell(10, 27).value == 10.2
        b2 = ws.cell(2, 2).value
        assert isinstance(b2, str) and b2.startswith("=SUM(X10:")
        assert ws.cell(6, 2).value == "=COUNTA(A10:A10)"
        assert ws.cell(5, 2).value == "=B4-B3"
        wb.close()
        out.unlink(missing_ok=True)


def test_export_efd_parcel_invoice_xlsx_empty_rows(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "efd_empty.db"
        monkeypatch.setattr(db, "DB_PATH", p)
        db.init_db()
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO customers (customer_number, customer_name) VALUES (300, 'Solo')"
        )
        conn.commit()
        conn.close()

        out, _title = exports.export_efd_parcel_invoice_xlsx(
            "2026-04-01",
            "2026-04-30",
            300,
            None,
            show_parents=True,
            show_main=True,
        )
        wb = load_workbook(out, data_only=False)
        ws = wb.active
        assert ws.cell(1, 2).value == "4-1-2026"
        assert ws.cell(2, 1).value == "Parcel Cost"
        assert ws.cell(2, 2).value == 0
        assert ws.cell(3, 2).value == 0
        assert ws.cell(6, 1).value == "Total parcel quantities"
        assert ws.cell(6, 2).value == 0
        wb.close()
        out.unlink(missing_ok=True)
