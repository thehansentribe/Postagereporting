"""Tests for the Pitney Detail Transactions importer and watcher routing."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from openpyxl import Workbook

import db as dbmod
import importer
import watcher

_HEADERS = [
    "transactionType",
    "amount",
    "transactionId",
    "revenueAssuranceID",
    "transactionDateTime",
    "parcelTrackingNumber",
    "status",
    "service",
    "zone",
    "weightInOunces",
    "postageBalance",
]


def _write_fixture_xlsx(path: Path, rows: list[list]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.append(_HEADERS)
    for r in rows:
        ws.append(r)
    wb.save(path)


def _fixture_rows() -> list[list]:
    return [
        [
            "Postage Print", 7.26, "45522977_4209260529-19480958", "",
            "2026-05-30 00:48:40", "`9434640109627025830821", "Delivered",
            "USPS Ground Advantage", "4", 35, 3664.41,
        ],
        # Same refund listed at two lifecycle stages (REQUESTED then ACCEPTED):
        # both rows are kept, but the refund counts once, at ACCEPTED.
        [
            "Postage Refund", 4.10, "45522977_refund-1", "",
            "2026-05-15 10:00:00", "`9434640109627025839999", "REQUESTED",
            "USPS Ground Advantage", "2", 12, None,
        ],
        [
            "Postage Refund", 4.10, "45522977_refund-1", "",
            "2026-05-20 09:00:00", "`9434640109627025839999", "ACCEPTED",
            "USPS Ground Advantage", "2", 12, 3600.00,
        ],
        # A print sharing its transactionId with an adjustment (real-file shape).
        [
            "Postage Adjustment (Underpaid)", 2.24, "45522977_adj-1", "10880195748",
            "2026-05-30 22:04:56", "`9400140109627781116380", "",
            "Priority Mail", "4", 15, 3655.05,
        ],
        [
            "Postage Print", 6.98, "45522977_adj-1", "",
            "2026-05-06 12:00:00", "`9400140109627781116380", "Delivered",
            "Priority Mail", "4", 15, 3650.00,
        ],
        [
            "Postage Fund", 5000.00, "45522977_fund-1", "",
            "2026-05-01 00:00:16", None, "",
            None, None, None, 5000.00,
        ],
    ]


def test_import_pitney_detail_transactions_and_dedup(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "pitney.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        xlsx = Path(td) / "05.31.26_Pitney Detail Transactions.xlsx"
        _write_fixture_xlsx(xlsx, _fixture_rows())

        result = importer.import_pitney_detail_transactions(str(xlsx), p)
        assert result["row_count"] == 6
        assert result["rows_imported"] == 6
        assert result["skipped_duplicates"] == 0

        # Re-import: everything is a duplicate.
        result2 = importer.import_pitney_detail_transactions(str(xlsx), p)
        assert result2["rows_imported"] == 0
        assert result2["skipped_duplicates"] == 6

        conn = sqlite3.connect(p)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM pitney_transactions ORDER BY transaction_id, transaction_type"
        ).fetchall()
        assert len(rows) == 6
        printed = next(
            r
            for r in rows
            if r["transaction_id"] == "45522977_4209260529-19480958"
        )
        # Leading backtick stripped; ISO date derived from the datetime.
        assert printed["tracking_normalized"] == "9434640109627025830821"
        assert printed["transaction_date"] == "2026-05-30"
        assert printed["amount"] == 7.26
        # Shared transactionId keeps both the print and the adjustment.
        shared = [r for r in rows if r["transaction_id"] == "45522977_adj-1"]
        assert sorted(r["transaction_type"] for r in shared) == [
            "Postage Adjustment (Underpaid)",
            "Postage Print",
        ]
        fund = next(r for r in rows if r["transaction_type"] == "Postage Fund")
        assert fund["tracking_normalized"] is None

        # Funds excluded; refund counted once (ACCEPTED); underpaid once.
        adj = dbmod.query_pitney_cost_adjustments(conn, "2026-05-01", "2026-05-31")
        assert adj["has_data"] is True
        assert adj["refund_total"] == 4.10
        assert adj["underpaid_total"] == 2.24
        assert adj["overpaid_total"] == 0.0
        conn.close()


def test_import_pitney_rejects_wrong_file(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "pitney_bad.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        xlsx = Path(td) / "random.xlsx"
        wb = Workbook()
        wb.active.append(["colA", "colB"])
        wb.save(xlsx)
        try:
            importer.import_pitney_detail_transactions(str(xlsx), p)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "Pitney" in str(e)


def test_watcher_routes_pitney_transactions_file():
    assert watcher._is_pitney_detail_transactions("05.31.26_Pitney Detail Transactions.xlsx")
    assert watcher._is_pitney_detail_transactions("pitney transactions june.XLSX")
    assert not watcher._is_pitney_detail_transactions("05.31.26_Pitney Detail Transactions.csv")
    assert not watcher._is_pitney_detail_transactions("BM_3_20_26.xlsx")
    assert not watcher._is_pitney_detail_transactions("WS3_FCFL_CustomerMailDetail.xlsx")


def test_pitney_dedup_key_migration_preserves_rows(monkeypatch):
    """Old single-UNIQUE tables are rebuilt with the composite key, keeping rows."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "pitney_migrate.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        # Swap in the legacy single-UNIQUE table shape with one row.
        raw = sqlite3.connect(p)
        raw.execute("DROP TABLE pitney_transactions")
        raw.execute(
            """
            CREATE TABLE pitney_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id INTEGER REFERENCES pitney_imports(id) ON DELETE SET NULL,
                transaction_id TEXT NOT NULL UNIQUE,
                transaction_type TEXT NOT NULL, transaction_date TEXT,
                transaction_datetime TEXT, amount REAL, tracking_number TEXT,
                tracking_normalized TEXT, service TEXT, zone TEXT,
                weight_oz REAL, status TEXT, postage_balance REAL)
            """
        )
        raw.execute(
            """
            INSERT INTO pitney_transactions
                (transaction_id, transaction_type, transaction_date, transaction_datetime, amount)
            VALUES ('x1', 'Postage Print', '2026-05-01', '2026-05-01 10:00:00', 5.0)
            """
        )
        raw.commit()
        raw.close()

        conn = dbmod.get_connection()
        conn.close()  # closing without commit must not lose the rebuild

        raw = sqlite3.connect(p)
        raw.row_factory = sqlite3.Row
        ddl = raw.execute(
            "SELECT sql FROM sqlite_master WHERE name='pitney_transactions'"
        ).fetchone()["sql"]
        assert "UNIQUE (transaction_id, transaction_type, transaction_datetime)" in ddl
        rows = raw.execute("SELECT * FROM pitney_transactions").fetchall()
        assert len(rows) == 1 and rows[0]["transaction_id"] == "x1"
        assert (
            raw.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='pitney_transactions_old'"
            ).fetchone()[0]
            == 0
        )
        raw.close()
