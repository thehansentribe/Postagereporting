"""Tests for WS3 Customer Mail Detail parsing."""

import tempfile
from pathlib import Path

import pytest
from openpyxl import Workbook

import db as dbmod
import ws3_mail_detail as ws3


@pytest.mark.parametrize(
    "name,expected",
    [
        ("WS3_FCFL_CustomerMailDetail_4-6-26.xls", "2026-04-06"),
        ("WS3_FCFL_CustomerMailDetail 4-6-26.xls", "2026-04-06"),
        ("x/WS3_FCFL_CustomerMailDetail_12-1-26.xlsx", "2026-12-01"),
    ],
)
def test_parse_date_from_filename(name, expected):
    assert ws3.parse_date_from_filename(name) == expected


def test_parse_date_from_filename_bad():
    assert ws3.parse_date_from_filename("BM_1_2_26.xls") is None


def test_parse_ws3_xlsx_minimal(tmp_path):
    """Minimal sheet matching parser state machine: customer → profile → one rate row."""
    wb = Workbook()
    ws = wb.active
    # Customer header row: col1 name, col14 $..., col16 Metered
    ws.cell(row=1, column=2, value="Acme Corp")
    ws.cell(row=1, column=15, value="$ 0.970")
    ws.cell(row=1, column=17, value="Metered")
    # Profile line col H = col 8 -> index 7 in 0-based is column 8? openpyxl column 8 is H
    # Column index 7 in 0-based = Excel column 8 (H)
    ws.cell(row=2, column=8, value="301079 Acme Profile .970")
    # Data row: rate col 20 = T -> index 19
    ws.cell(row=3, column=20, value="ADC Auto")
    ws.cell(row=3, column=41, value="10.00")  # postage claimed col 41
    ws.cell(row=3, column=54, value="9.70")  # postage applied
    ws.cell(row=3, column=67, value="10")  # num pieces
    ws.cell(row=3, column=71, value="10")  # pcs accepted
    p = tmp_path / "t.xlsx"
    wb.save(p)

    mid, rdt, customers, rows = ws3.parse_ws3_xlsx(str(p))
    assert customers.get("301079") == "Acme Corp"
    assert len(rows) == 1
    assert rows[0]["customer_code"] == "301079"
    assert rows[0]["rate_type"] == "ADC Auto"
    assert rows[0]["num_pieces"] == 10
    assert rows[0]["pcs_rejected"] == 0


def test_parse_ws3_xlsx_rate_on_profile_row_is_captured(tmp_path):
    """This NetSort layout puts the first rate type on the profile-name row."""
    wb = Workbook()
    ws = wb.active
    ws.cell(row=1, column=2, value="Acme Corp")
    ws.cell(row=1, column=15, value="$ 0.970")
    ws.cell(row=1, column=17, value="Metered")
    # Profile name (col H = 8) AND first rate type (col T = 20) on the SAME row.
    ws.cell(row=2, column=8, value="301079 Acme Profile .970")
    ws.cell(row=2, column=20, value="ThreeDigitAuto")
    ws.cell(row=2, column=54, value="9.70")
    ws.cell(row=2, column=67, value="5")
    ws.cell(row=2, column=71, value="5")
    # Second rate type on its own row.
    ws.cell(row=3, column=20, value="ADC Auto")
    ws.cell(row=3, column=54, value="4.85")
    ws.cell(row=3, column=67, value="5")
    ws.cell(row=3, column=71, value="5")
    p = tmp_path / "t.xlsx"
    wb.save(p)
    wb.close()

    _, _, customers, rows = ws3.parse_ws3_xlsx(str(p))
    assert customers.get("301079") == "Acme Corp"
    assert len(rows) == 2
    assert {r["rate_type"] for r in rows} == {"ThreeDigitAuto", "ADC Auto"}
    assert all(r["customer_code"] == "301079" for r in rows)


def test_process_ws3_mislabeled_xls_imports_rows(monkeypatch, tmp_path):
    """An OOXML workbook saved with a .xls name must import without LibreOffice."""
    import db as dbmod
    import importer

    wb = Workbook()
    ws = wb.active
    ws["BI8"] = "040626_F"
    ws.cell(row=1, column=2, value="Acme Corp")
    ws.cell(row=1, column=15, value="$ 0.970")
    ws.cell(row=1, column=17, value="Metered")
    ws.cell(row=2, column=8, value="301079 Acme Profile .970")
    ws.cell(row=2, column=20, value="ADC Auto")
    ws.cell(row=2, column=54, value="9.70")
    ws.cell(row=2, column=67, value="10")
    ws.cell(row=2, column=71, value="10")
    src_xlsx = tmp_path / "src.xlsx"
    wb.save(src_xlsx)
    wb.close()

    db_path = tmp_path / "t.db"
    monkeypatch.setattr(dbmod, "DB_PATH", db_path)
    dbmod.init_db()

    # Fail loudly if conversion is attempted: a real OOXML file must not be converted.
    def _no_convert(*_a, **_k):
        raise AssertionError("convert_xls_to_xlsx should not be called for OOXML content")

    monkeypatch.setattr(importer, "convert_xls_to_xlsx", _no_convert)

    dest = tmp_path / "WS3_FCFL_CustomerMailDetail(9).xls"
    dest.write_bytes(src_xlsx.read_bytes())

    out = ws3.process_ws3_mail_detail_file(str(dest), db_path)
    assert out.get("skipped") is False
    assert out.get("mail_date") == "2026-04-06"
    assert out.get("rows_imported", 0) >= 1


def test_parse_ws3_xlsx_negative_rejected_clamped_to_zero(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.cell(row=1, column=2, value="Acme Corp")
    ws.cell(row=1, column=15, value="$ 0.970")
    ws.cell(row=1, column=17, value="Metered")
    ws.cell(row=2, column=8, value="301079 Acme Profile .970")
    ws.cell(row=3, column=20, value="ADC Auto")
    ws.cell(row=3, column=54, value="9.70")  # postage applied
    ws.cell(row=3, column=67, value="10")  # num pieces
    ws.cell(row=3, column=71, value="12")  # pcs accepted (bad data -> negative rejected)
    p = tmp_path / "t.xlsx"
    wb.save(p)

    _, _, _, rows = ws3.parse_ws3_xlsx(str(p))
    assert len(rows) == 1
    assert rows[0]["pcs_rejected"] == 0


def test_list_ws3_assignment_accounts_parent_vs_main(monkeypatch):
    """Parent = has children; main = no customers use this number as parent_number."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        conn.executemany(
            """
            INSERT INTO customers (customer_number, customer_name, parent_number, parent_name)
            VALUES (?, ?, ?, ?)
            """,
            [
                (100, "Parent Co", None, None),
                (101, "Child A", 100, "Parent Co"),
                (200, "Solo Inc", None, None),
            ],
        )
        conn.commit()
        accts = dbmod.list_ws3_assignment_accounts(conn)
        conn.close()
    kinds = {a["customer_number"]: a["kind"] for a in accts}
    assert kinds[100] == "parent"
    assert kinds[101] == "main"
    assert kinds[200] == "main"


def test_query_postage_includes_ws3_reject_row(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        conn.execute(
            "INSERT INTO customers (customer_number, customer_name) VALUES (100, 'Parent Co')"
        )
        conn.execute(
            """
            INSERT INTO ws3_parent_daily_rejects (mail_date, parent_customer_number, reject_count)
            VALUES ('2026-04-01', 100, 7)
            """
        )
        conn.commit()
        data = dbmod.query_postage(
            conn,
            "2026-04-01",
            "2026-04-30",
            None,
            None,
            True,
            True,
            False,
            False,
            False,
        )
        conn.close()
    rej = [r for r in data["rows"] if r.get("mail_class") == dbmod.WS3_REJECT_MAIL_CLASS]
    assert len(rej) == 1
    assert rej[0]["total_qty"] == 7
    assert rej[0]["child_number"] == 100
