"""Tests for Parcel Profit JSON endpoint."""

from __future__ import annotations

import importlib
import tempfile
from pathlib import Path

import pytest

import db as dbmod
import watcher as watchermod

pytest.importorskip("flask")


def _client(monkeypatch, db_path: Path):
    monkeypatch.setattr(dbmod, "DB_PATH", db_path)
    dbmod.init_db()

    monkeypatch.setattr(watchermod, "ensure_dirs", lambda: None)
    import app as appmod

    appmod = importlib.reload(appmod)
    monkeypatch.setattr(appmod, "_ensure_watcher", lambda: None)
    appmod.app.config.update(TESTING=True)
    return appmod.app.test_client()


def test_api_profit_parcels_zero_rows(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "parcel_profit_empty.db"
        client = _client(monkeypatch, p)
        r = client.get("/api/profit/parcels?start_date=2026-04-01&end_date=2026-04-07")
        assert r.status_code == 200
        j = r.get_json()
        assert j["raw"]["parcel_count"] == 0
        assert j["raw"]["total_final_postage"] == 0.0
        assert j["raw"]["total_fully_paid_postage"] == 0.0
        assert j["raw"]["total_billing_amount"] == 0.0
        assert j["computed"]["postage_fee"] == 0.0
        assert j["computed"]["lineage_revenue"] == 0.0
        assert j["computed"]["efd_profit"] == 0.0


def test_api_profit_parcels_formulas(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "parcel_profit_ok.db"
        client = _client(monkeypatch, p)

        conn = dbmod.get_connection()
        try:
            conn.execute(
                "INSERT INTO customers (customer_number, customer_name, parent_number, parent_name) VALUES (900, 'P', NULL, NULL)"
            )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('b1', 'b.csv', 2)"
            )
            # Two pieces in range.
            conn.executemany(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class,
                    final_postage, fully_paid_postage, billing_amount,
                    zone
                ) VALUES (1, 900, 'P', '4/1/2026 10:00', 16.0, 'CLS', ?, ?, ?, '1')
                """,
                [
                    (1.25, 2.00, 3.00),
                    (0.75, 1.00, 4.00),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        r = client.get("/api/profit/parcels?start_date=2026-04-01&end_date=2026-04-30&customer_number=900")
        assert r.status_code == 200
        j = r.get_json()

        # Raw totals.
        assert j["raw"]["parcel_count"] == 2
        assert j["raw"]["total_final_postage"] == pytest.approx(2.00, rel=1e-9)
        assert j["raw"]["total_fully_paid_postage"] == pytest.approx(3.00, rel=1e-9)
        assert j["raw"]["total_billing_amount"] == pytest.approx(7.00, rel=1e-9)

        # Fee = qty * 0.50
        assert j["computed"]["postage_fee"] == pytest.approx(1.00, rel=1e-9)

        # Confirmed formulas:
        # Lineage Revenue = billing_amount - final_postage + postage_fee
        assert j["computed"]["lineage_revenue"] == pytest.approx(7.00 - 2.00 + 1.00, rel=1e-9)
        # EFD Profit = fully_paid_postage - billing_amount - postage_fee
        assert j["computed"]["efd_profit"] == pytest.approx(3.00 - 7.00 - 1.00, rel=1e-9)

