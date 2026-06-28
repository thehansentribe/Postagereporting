"""Tests for EFD weekly bundle download naming on flats and parcel routes."""

from __future__ import annotations

import importlib
import tempfile
from pathlib import Path

import pytest

import db as dbmod
import exports
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


def test_flats_grid_csv_efd_weekly_bundle_filename(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "efd_bundle_flats.db"
        client = _client(monkeypatch, db_path)
        conn = dbmod.get_connection()
        conn.execute(
            "INSERT INTO customers (customer_number, customer_name) VALUES (3900, ?)",
            ("Security Benefit/Zinnia -EFD Mailing",),
        )
        conn.commit()
        conn.close()

        out = Path(td) / "mock_flats.csv"
        out.write_text("date,parent\n", encoding="utf-8")

        monkeypatch.setattr(exports, "export_flats_data_grid_csv", lambda *a, **k: out)

        r = client.get(
            "/api/export/flats-grid-csv?"
            "start_date=2026-06-08&end_date=2026-06-12&parent_number=3900&efd_weekly_bundle=true"
        )
        assert r.status_code == 200
        disp = r.headers.get("Content-Disposition") or ""
        assert "Security Benefit" in disp
        assert "3900" in disp
        assert "Flats Report" in disp
        assert "6-8 to 6-12" in disp


def test_parcel_zone_summary_efd_weekly_bundle_filename(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "efd_bundle_parcel.db"
        client = _client(monkeypatch, db_path)

        out = Path(td) / "mock_parcel.xlsx"
        out.write_bytes(b"pk")

        summary = {
            "title_name": "GEHA -EFD Mailing",
            "rows": [],
            "zone_pairs": [],
        }

        monkeypatch.setattr(
            dbmod,
            "query_parcel_zone_summary",
            lambda *a, **k: summary,
        )
        monkeypatch.setattr(
            exports,
            "export_parcel_zone_summary_xlsx",
            lambda *a, **k: out,
        )

        r = client.get(
            "/api/export/parcel-zone-summary?"
            "start_date=2026-06-08&end_date=2026-06-12&parent_number=3899&efd_weekly_bundle=true"
        )
        assert r.status_code == 200
        disp = r.headers.get("Content-Disposition") or ""
        assert "GEHA" in disp
        assert "3899" in disp
        assert "Parcel invoice" in disp
        assert "6-8 to 6-12" in disp


def test_flats_grid_csv_without_bundle_keeps_legacy_filename(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "efd_bundle_legacy.db"
        client = _client(monkeypatch, db_path)
        conn = dbmod.get_connection()
        conn.execute(
            "INSERT INTO customers (customer_number, customer_name) VALUES (3901, ?)",
            ("BCBS",),
        )
        conn.commit()
        conn.close()

        out = Path(td) / "mock_flats.csv"
        out.write_text("date,parent\n", encoding="utf-8")
        monkeypatch.setattr(exports, "export_flats_data_grid_csv", lambda *a, **k: out)

        r = client.get(
            "/api/export/flats-grid-csv?"
            "start_date=2026-06-08&end_date=2026-06-12&parent_number=3901"
        )
        assert r.status_code == 200
        disp = r.headers.get("Content-Disposition") or ""
        assert "BCBS (3901) Flats Report 6-12-2026" in disp
        assert "6-8 to 6-12" not in disp
