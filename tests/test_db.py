"""Smoke tests for database init and summary query."""

import tempfile
from pathlib import Path

import pytest

import db as dbmod
import exports as exports_mod


def test_init_and_summary_empty(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "test.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            s = dbmod.query_summary(conn, "2026-01-01", "2026-01-31")
        finally:
            conn.close()

    assert s["date_range"] == {"start": "2026-01-01", "end": "2026-01-31"}
    assert s["postage"]["total_pieces"] == 0
    assert s["postage"]["total_cost"] == 0.0
    assert s["parcels"]["total_pieces"] == 0
    assert s["parcels"]["total_billed"] == 0.0
    assert s["imports"] == []
    assert s["postage"]["by_customer"] == []
    assert s["parcels"]["by_customer"] == []


def test_customer_hierarchy(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "hier.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            conn.executemany(
                """
                INSERT INTO customers (customer_number, customer_name, parent_number, parent_name)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (100, "Parent Co", None, None),
                    (101, "Child A", 100, "Parent Co"),
                    (102, "Child B", 100, "Parent Co"),
                    (200, "Solo Inc", None, None),
                ],
            )
            conn.commit()
            h = dbmod.query_customer_hierarchy(conn)
        finally:
            conn.close()

    assert len(h["parents"]) == 1
    assert h["parents"][0]["customer_number"] == 100
    assert h["parents"][0]["child_count"] == 2
    assert {c["customer_number"] for c in h["parents"][0]["children"]} == {101, 102}
    assert h["standalone"] == [{"customer_number": 200, "customer_name": "Solo Inc"}]


def test_query_summary_parcel_billing_timestamp(monkeypatch):
    """Parcel summary must parse M/D/YYYY HH:MM (year is not the last 4 chars of the string)."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "parcel_sum.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO customers (customer_number, customer_name, parent_number, parent_name)
                VALUES (9999, 'TestAcct', NULL, NULL)
                """
            )
            conn.execute(
                """
                INSERT INTO billing_imports (billing_id, file_name, row_count)
                VALUES ('t1', 'test.csv', 1)
                """
            )
            conn.execute(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class, billing_amount, unmatched_account
                ) VALUES (1, 9999, 'TestAcct', '4/1/2026 15:34', 1.0, 'USPS GROUND ADVANTAGE', 5.25, 0)
                """
            )
            conn.commit()
            s = dbmod.query_summary(conn, "2026-04-01", "2026-04-30")
        finally:
            conn.close()

    assert s["parcels"]["total_pieces"] == 1
    assert s["parcels"]["total_billed"] == 5.25
    by_class = {r["mail_class"]: r for r in s["parcels"]["by_class"]}
    assert by_class["USPS GROUND ADVANTAGE"]["pieces"] == 1
    cust = {r["customer_number"]: r for r in s["parcels"]["by_customer"] if r["customer_number"] is not None}
    assert cust[9999]["pieces"] == 1
    assert cust[9999]["cost"] == 5.25
    assert cust[9999]["unmatched"] is False


def test_query_summary_postage_unmatched_orphan(monkeypatch):
    """Postage for unknown account appears as Unmatched and rolls into totals."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "post_orphan.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO customers (customer_number, customer_name, parent_number, parent_name)
                VALUES (100, 'Known', NULL, NULL)
                """
            )
            conn.execute(
                "INSERT INTO postage_imports (file_name, file_date, row_count) VALUES ('r.csv', '2026-02-10', 2)"
            )
            conn.executemany(
                """
                INSERT INTO postage_data (
                    import_id, file_date, account_code, mail_class,
                    weight_oz, pieces, total_cost, unmatched_account
                ) VALUES (1, '2026-02-10', ?, 'C', 1.0, 3, 1.5, 0)
                """,
                [(100,), (7777,)],
            )
            conn.commit()
            s = dbmod.query_summary(conn, "2026-02-01", "2026-02-28")
        finally:
            conn.close()

    by_num = {r["customer_number"]: r for r in s["postage"]["by_customer"]}
    assert by_num[100]["unmatched"] is False
    assert by_num[100]["pieces"] == 3
    orphan = by_num[7777]
    assert orphan["customer_name"] == "Unmatched"
    assert orphan["unmatched"] is True
    assert orphan["pieces"] == 3
    assert s["postage"]["total_pieces"] == 6
    assert s["postage"]["total_cost"] == 3.0


def test_query_summary_parcel_unmatched_orphan(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "par_orphan.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO customers (customer_number, customer_name, parent_number, parent_name)
                VALUES (100, 'Known', NULL, NULL)
                """
            )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('b1', 'b.csv', 3)"
            )
            conn.executemany(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class, billing_amount, unmatched_account
                ) VALUES (1, ?, 'x', '2/15/2026 10:00', 2.0, 'CLS', 2.0, 1)
                """,
                [(100,), (8888,), (None,)],
            )
            conn.commit()
            s = dbmod.query_summary(conn, "2026-02-01", "2026-02-28")
        finally:
            conn.close()

    rows = s["parcels"]["by_customer"]
    matched = next(r for r in rows if r["customer_number"] == 100)
    assert matched["unmatched"] is False
    assert matched["pieces"] == 1
    unk = next(r for r in rows if r["customer_number"] == 8888)
    assert unk["customer_name"] == "Unmatched"
    assert unk["unmatched"] is True
    assert unk["pieces"] == 1
    null_cac = next(r for r in rows if r["customer_number"] is None)
    assert null_cac["unmatched"] is True
    assert null_cac["pieces"] == 1
    assert s["parcels"]["total_pieces"] == 3
    assert s["parcels"]["total_billed"] == 6.0


def test_query_parcel_report_rows_parent_name_coalesce(monkeypatch):
    """Column C uses COALESCE(parent_name, customer_name) for matched accounts."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "parcel_export.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO customers (customer_number, customer_name, parent_number, parent_name)
                VALUES (200, 'Solo Inc', NULL, NULL)
                """
            )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('b1', 'b.csv', 1)"
            )
            conn.execute(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class, billing_amount, piece_id
                ) VALUES (1, 200, 'Solo Inc', '4/1/2026 10:00', 16.0, 'CLS', 1.0, 'P1')
                """
            )
            conn.commit()
            rows = dbmod.query_parcel_report_rows(
                conn, "2026-04-01", "2026-04-30", None, None, True, True
            )
        finally:
            conn.close()

    assert len(rows) == 1
    assert rows[0]["parent_name"] == "Solo Inc"


def test_query_parcel_report_rows_customer_number_filter(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "parcel_export2.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            conn.executemany(
                """
                INSERT INTO customers (customer_number, customer_name, parent_number, parent_name)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (100, "Parent Co", None, None),
                    (101, "Child A", 100, "Parent Co"),
                    (102, "Child B", 100, "Parent Co"),
                ],
            )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('b1', 'b.csv', 2)"
            )
            conn.executemany(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class, billing_amount, piece_id
                ) VALUES (1, ?, 'x', '4/2/2026 11:00', 8.0, 'CLS', 2.0, ?)
                """,
                [(101, "PA"), (102, "PB")],
            )
            conn.commit()
            rows = dbmod.query_parcel_report_rows(
                conn, "2026-04-01", "2026-04-30", 100, 101, True, True
            )
        finally:
            conn.close()

    assert len(rows) == 1
    assert rows[0]["custom_account_code"] == 101
    assert rows[0]["piece_id"] == "PA"


def test_export_parcel_report_workbook(monkeypatch):
    from openpyxl import load_workbook

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "parcel_xlsx.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            conn.executemany(
                """
                INSERT INTO customers (customer_number, customer_name, parent_number, parent_name)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (100, "Parent Co", None, None),
                    (101, "Child A", 100, "Parent Co"),
                ],
            )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('b1', 'b.csv', 1)"
            )
            conn.execute(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class, billing_amount, fully_paid_postage, piece_id
                ) VALUES (1, 101, 'Child A', '4/2/2026 11:00', 8.0, 'CLS', 2.0, 2.5, 'PX')
                """
            )
            conn.commit()
        finally:
            conn.close()

        out = exports_mod.export_parcel_report("2026-04-01", "2026-04-30", 100, None, True, True)
        try:
            wb = load_workbook(out)
            ws = wb.active
            assert ws.cell(1, 1).value == "Customer #"
            assert ws.cell(1, 3).value == "Parent Name"
            assert ws.cell(2, 1).value == 101
            assert ws.cell(2, 3).value == "Parent Co"
            assert ws.cell(2, 9).value == "=H2/16"
            assert ws.cell(1, 13).value == "IMPB"
            assert ws.cell(2, 13).value in (None, "")
            tot_row = 3
            assert ws.cell(tot_row, 1).value == "TOTALS"
            assert ws.cell(tot_row, 10).value == "=SUM(J2:J2)"
            assert ws.cell(15, 1).value == "Count"
            assert ws.cell(15, 8).value == "Customer #"
            assert ws.cell(16, 10).value == 1
        finally:
            out.unlink(missing_ok=True)

    assert exports_mod.parcel_report_download_name("2026-04-01", "2026-04-02", 100, 101) == (
        "Parcel_Report_100_c101_2026-04-01_2026-04-02.xlsx"
    )


def test_export_parcel_zone_summary_includes_af_hm_sections(monkeypatch):
    """Export Parcel Summary workbook includes 11–100 lb A–F and customer H–M below the zone grid."""
    from openpyxl import load_workbook

    def fake_af_hm(*_a, **_k):
        return {
            "heavy_rows": [
                {"count": 2, "lbs": 15, "zone": 2, "base": 20.5, "efd": 20.0, "savings": 0.5},
            ],
            "customers": [
                {
                    "customer_number": 101,
                    "name": "Child",
                    "qty": 3,
                    "cost": 10.0,
                    "savings": 1.0,
                }
            ],
            "grand_total_qty": 5,
        }

    monkeypatch.setattr(dbmod, "compute_parcel_report_af_hm_sections", fake_af_hm)

    def row_template(lb: int) -> dict:
        return {
            "weight_label": f"{lb} lb",
            "zone_a": {"priority": 1.0, "efd": 1.0, "count": 0},
            "zone_b": {"priority": 1.0, "efd": 1.0, "count": 0},
            "costs": None,
            "savings": None,
        }

    rows = [row_template(lb) for lb in range(1, 11)]
    blocks = [
        {"zone_a": za, "zone_b": zb, "rows": rows}
        for za, zb in ((1, 3), (2, 4), (5, 6), (7, 8))
    ]
    summary = {
        "report_date": "01-Apr-2026",
        "title_name": "Test Co",
        "total_pieces": 0,
        "total_cost": 0.0,
        "total_savings": 0.0,
        "blocks": blocks,
    }
    out = exports_mod.export_parcel_zone_summary_xlsx(
        summary,
        start_date="2026-04-01",
        end_date="2026-04-30",
        parent_number=None,
        customer_number=None,
        show_parents=True,
        show_main=True,
    )
    try:
        wb = load_workbook(out)
        ws = wb.active
        assert ws.cell(56, 1).value == "Count"
        assert ws.cell(56, 8).value == "Customer #"
        assert ws.cell(57, 1).value == 2
        assert ws.cell(57, 8).value == 101
        assert ws.cell(57, 10).value == 5
    finally:
        out.unlink(missing_ok=True)


def test_query_parcel_zone_summary_matrix_and_totals(monkeypatch):
    fake_rates = {(1, 1): (11.0, 10.0), (3, 2): (13.0, 12.0)}
    monkeypatch.setattr(dbmod, "get_parcel_summary_rates", lambda: fake_rates)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "zone_sum.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO customers (customer_number, customer_name, parent_number, parent_name)
                VALUES (9999, 'ZoneTest', NULL, NULL)
                """
            )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('z1', 'z.csv', 3)"
            )
            conn.executemany(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class, billing_amount, fully_paid_postage, zone
                ) VALUES (1, 9999, 'ZoneTest', '4/1/2026 10:00', ?, 'USPS GROUND ADVANTAGE', ?, ?, ?)
                """,
                [
                    (16.0, 10.80, 11.05, "1"),
                    (32.0, 12.20, 12.45, "3"),
                    (16.0, 5.0, 5.5, "9"),
                ],
            )
            conn.commit()
            s = dbmod.query_parcel_zone_summary(
                conn, "2026-04-01", "2026-04-30", None, None, True, True, False
            )
        finally:
            conn.close()

    assert s["total_pieces"] == 3
    assert s["total_cost"] == 22.0
    assert s["total_savings"] == 2.0
    b0 = s["blocks"][0]
    assert b0["rows"][0]["zone_a"]["count"] == 1
    assert b0["rows"][0]["zone_a"]["priority"] == 11.0
    assert b0["rows"][0]["zone_a"]["efd"] == 10.0
    assert b0["rows"][0]["zone_b"]["count"] == 0
    assert b0["rows"][1]["zone_a"]["count"] == 0
    assert b0["rows"][1]["zone_b"]["count"] == 1
    assert b0["rows"][1]["zone_b"]["priority"] == 13.0
    assert b0["rows"][1]["zone_b"]["efd"] == 12.0
    assert b0["rows"][0]["costs"] == 10.0
    assert b0["rows"][0]["savings"] == 1.0
    assert b0["rows"][1]["costs"] == 12.0
    assert b0["rows"][1]["savings"] == 1.0


def test_compute_parcel_report_af_hm_sections(monkeypatch):
    fake_rates = {(2, 13): (19.2, 18.95), (3, 16): (22.05, 21.8)}
    monkeypatch.setattr(dbmod, "get_heavy_parcel_rates", lambda: fake_rates)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "afhm.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO customers (customer_number, customer_name, parent_number, parent_name)
                VALUES (500, 'Heavy Co', NULL, NULL)
                """
            )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('b1', 'b.csv', 2)"
            )
            conn.executemany(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class, billing_amount, fully_paid_postage, zone
                ) VALUES (1, 500, 'Heavy Co', '4/1/2026 10:00', ?, 'CLS', 1.0, 1.0, ?)
                """,
                [
                    (200.0, "2"),
                    (256.0, "3"),
                ],
            )
            conn.commit()
            s = dbmod.compute_parcel_report_af_hm_sections(
                conn, "2026-04-01", "2026-04-30", None, None, True, True
            )
        finally:
            conn.close()

    assert s["grand_total_qty"] == 2
    assert len(s["heavy_rows"]) == 2
    assert len(s["customers"]) == 1
    assert s["customers"][0]["customer_number"] == 500
    assert s["customers"][0]["name"] == "Heavy Co"
    assert s["customers"][0]["qty"] == 2
    assert s["customers"][0]["cost"] == pytest.approx(18.95 + 21.8, rel=1e-6)
    assert s["customers"][0]["savings"] == pytest.approx(0.25 + 0.25, rel=1e-6)
    lbs_z = {(r["lbs"], r["zone"]): r["count"] for r in s["heavy_rows"]}
    assert lbs_z[(13, 2)] == 1
    assert lbs_z[(16, 3)] == 1


def test_compute_parcel_report_af_hm_uses_customer_master_name_not_billing(monkeypatch):
    """H–M Customer Name column uses customers.customer_name, not billing_records.account_name."""
    monkeypatch.setattr(dbmod, "get_heavy_parcel_rates", lambda: {})
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "afhm_name.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO customers (customer_number, customer_name, parent_number, parent_name)
                VALUES (600, 'Master Name From Customers', NULL, NULL)
                """
            )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('b1', 'b.csv', 1)"
            )
            conn.execute(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class, billing_amount, fully_paid_postage, zone
                ) VALUES (1, 600, 'Wrong Name On Billing Row', '4/1/2026 10:00', 8.0, 'CLS', 1.0, 1.0, '1')
                """
            )
            conn.commit()
            s = dbmod.compute_parcel_report_af_hm_sections(
                conn, "2026-04-01", "2026-04-30", None, None, True, True
            )
        finally:
            conn.close()

    assert len(s["customers"]) == 1
    assert s["customers"][0]["name"] == "Master Name From Customers"


def test_postage_row_edit_preview_and_apply_merge(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "postage_edit.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            conn.executemany(
                """
                INSERT INTO customers (customer_number, customer_name, parent_number, parent_name)
                VALUES (?, ?, NULL, NULL)
                """,
                [(100, "FromAcct"), (200, "ToAcct")],
            )
            conn.execute(
                "INSERT INTO postage_imports (file_name, file_date, row_count) VALUES ('r.csv', '2026-02-10', 3)"
            )
            # Source account rows (account_code=100) for one mail_class on one day.
            conn.executemany(
                """
                INSERT INTO postage_data (
                    import_id, file_date, account_code, mail_class, weight_oz, pieces, total_cost, unmatched_account
                ) VALUES (1, '2026-02-10', 100, 'C', ?, ?, ?, 0)
                """,
                [
                    (1.0, 10, 5.0),
                    (2.0, 4, 2.0),
                ],
            )
            # Destination already has weight_oz=1.0, so update must merge into it.
            conn.execute(
                """
                INSERT INTO postage_data (
                    import_id, file_date, account_code, mail_class, weight_oz, pieces, total_cost, unmatched_account
                ) VALUES (1, '2026-02-10', 200, 'C', 1.0, 3, 1.5, 0)
                """
            )
            conn.commit()

            src = conn.execute(
                """
                SELECT id, weight_oz, pieces, total_cost
                FROM postage_data
                WHERE file_date='2026-02-10' AND account_code=100 AND mail_class='C'
                ORDER BY weight_oz
                """
            ).fetchall()
            assert len(src) == 2
            id_w1 = int(src[0]["id"])
            id_w2 = int(src[1]["id"])
            assert float(src[0]["weight_oz"]) == 1.0
            assert float(src[1]["weight_oz"]) == 2.0

            pieces_by_id = {str(id_w1): 12, str(id_w2): 4}

            prev = dbmod.preview_postage_row_update(
                conn,
                file_date="2026-02-10",
                from_account_code=100,
                mail_class="C",
                to_account_code=200,
                pieces_by_id=pieces_by_id,
            )
            assert prev["ok"] is True
            assert prev["summary"]["source_rows"] == 2
            assert prev["summary"]["merged"] == 1
            assert prev["summary"]["updated"] == 1

            with conn:
                out = dbmod.apply_postage_row_update(
                    conn,
                    file_date="2026-02-10",
                    from_account_code=100,
                    mail_class="C",
                    to_account_code=200,
                    pieces_by_id=pieces_by_id,
                    reason="Fix wrong account",
                )
            assert out["ok"] is True
            assert out["summary"]["merged"] == 1
            assert out["summary"]["updated"] == 1

            # Source rows should be gone.
            left = conn.execute(
                "SELECT COUNT(*) AS n FROM postage_data WHERE file_date='2026-02-10' AND account_code=100 AND mail_class='C'"
            ).fetchone()
            assert int(left["n"]) == 0

            # Weight 1.0 should be merged into existing destination row:
            dest_w1 = conn.execute(
                """
                SELECT pieces, total_cost
                FROM postage_data
                WHERE file_date='2026-02-10' AND account_code=200 AND mail_class='C' AND weight_oz=1.0
                """
            ).fetchone()
            assert dest_w1 is not None
            assert int(dest_w1["pieces"]) == 3 + 12
            # Source cost 5.0 scaled from 10->12 pieces => 6.0, added to dest 1.5 => 7.5
            assert float(dest_w1["total_cost"]) == pytest.approx(7.5, rel=1e-9)

            # Weight 2.0 should now exist under destination.
            dest_w2 = conn.execute(
                """
                SELECT pieces, total_cost
                FROM postage_data
                WHERE file_date='2026-02-10' AND account_code=200 AND mail_class='C' AND weight_oz=2.0
                """
            ).fetchone()
            assert dest_w2 is not None
            assert int(dest_w2["pieces"]) == 4
            assert float(dest_w2["total_cost"]) == pytest.approx(2.0, rel=1e-9)

            # Audit tables should be populated.
            edits = conn.execute("SELECT * FROM postage_edits").fetchall()
            assert len(edits) == 1
            assert edits[0]["from_account_code"] == 100
            assert edits[0]["to_account_code"] == 200
            assert edits[0]["mail_class"] == "C"
            assert edits[0]["reason"] == "Fix wrong account"

            lines = conn.execute(
                "SELECT action, weight_oz, old_account_code, new_account_code FROM postage_edit_lines ORDER BY weight_oz"
            ).fetchall()
            assert len(lines) == 2
            assert {ln["action"] for ln in lines} == {"merged", "updated"}
            assert all(ln["old_account_code"] == 100 for ln in lines)
            assert all(ln["new_account_code"] == 200 for ln in lines)
        finally:
            conn.close()


def test_query_parcel_over_10lb_lines_filters_and_base(monkeypatch):
    fake_rates = {(2, 13): (19.2, 18.95), (3, 16): (22.05, 21.8)}
    monkeypatch.setattr(dbmod, "get_heavy_parcel_rates", lambda: fake_rates)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "o10lb.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO customers (customer_number, customer_name, parent_number, parent_name)
                VALUES (700, 'Co', NULL, NULL)
                """
            )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('b1', 'b.csv', 3)"
            )
            conn.executemany(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class, billing_amount, fully_paid_postage, zone
                ) VALUES (1, 700, 'Co', '4/1/2026 10:00', ?, 'CLS', 1.0, ?, ?)
                """,
                [
                    (16.0, 2.5, "2"),
                    (200.0, 19.2, "2"),
                    (256.0, 30.0, "3"),
                ],
            )
            conn.commit()
            rows = dbmod.query_parcel_over_10lb_lines(
                conn, "2026-04-01", "2026-04-30", None, None, True, True
            )
        finally:
            conn.close()

    assert len(rows) == 2
    by_lbs = {r["lbs"]: r for r in rows}
    assert by_lbs[13]["zone"] == 2
    assert by_lbs[13]["customer_number"] == 700
    assert by_lbs[13]["child_name"] == "Co"
    assert by_lbs[13]["base"] == pytest.approx(19.2, rel=1e-9)
    assert by_lbs[16]["zone"] == 3
    assert by_lbs[16]["base"] == pytest.approx(22.05, rel=1e-9)
