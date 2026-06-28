"""Tests for EFD weekly bundle save-to-folder API."""

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


def _mock_temp_file(td: Path, name: str) -> Path:
    p = Path(td) / name
    p.write_bytes(b"data")
    return p


def test_efd_weekly_bundle_save_success(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        reports_root = td_path / "PostageReports"
        monkeypatch.setattr(exports, "POSTAGE_REPORTS_DIR", reports_root)

        client = _client(monkeypatch, td_path / "bundle.db")

        def _weekly(*args, **kwargs):
            label = kwargs.get("parent_number")
            suffix = "combined" if label is None else str(label)
            return _mock_temp_file(td_path, f"weekly_{suffix}.xlsx")

        def _postage(pn, start_date, end_date, **kwargs):
            return _mock_temp_file(td_path, f"postage_{pn}.xlsx")

        def _parcel(summary, **kwargs):
            pn = kwargs.get("parent_number")
            return _mock_temp_file(td_path, f"parcel_{pn}.xlsx")

        monkeypatch.setattr(exports, "export_efd_weekly_invoice_xlsx", _weekly)
        monkeypatch.setattr(exports, "export_postage_invoice", _postage)
        monkeypatch.setattr(exports, "export_parcel_zone_summary_xlsx", _parcel)
        monkeypatch.setattr(
            dbmod,
            "query_parcel_zone_summary",
            lambda *a, **k: {"title_name": "GEHA -EFD Mailing", "rows": []},
        )

        r = client.post(
            "/api/export/efd-weekly-bundle",
            json={
                "start_date": "2026-06-08",
                "end_date": "2026-06-12",
                "discount": 0.23,
                "efd_parcel_fee": 1.25,
                "parcel_discount": 0.25,
            },
        )
        assert r.status_code == 200
        data = r.get_json()
        assert data["folder_relative"] == "PostageReports/Weekly EFD 6-12-26"
        assert len(data["saved"]) == 10
        assert data["failed"] == []
        postage_saved = [n for n in data["saved"] if "Postage invoice" in n and n.endswith(".xlsx")]
        assert len(postage_saved) == 3

        out_dir = reports_root / "Weekly EFD 6-12-26"
        assert out_dir.is_dir()
        assert len(list(out_dir.iterdir())) == 10


def test_efd_weekly_bundle_partial_failure(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        reports_root = td_path / "PostageReports"
        monkeypatch.setattr(exports, "POSTAGE_REPORTS_DIR", reports_root)

        client = _client(monkeypatch, td_path / "bundle_partial.db")

        def _weekly(*args, **kwargs):
            if kwargs.get("parent_number") == 3899:
                raise ValueError("no data for GEHA")
            label = kwargs.get("parent_number")
            suffix = "combined" if label is None else str(label)
            return _mock_temp_file(td_path, f"weekly_{suffix}.xlsx")

        def _postage(pn, start_date, end_date, **kwargs):
            return _mock_temp_file(td_path, f"postage_{pn}.xlsx")

        def _parcel(summary, **kwargs):
            pn = kwargs.get("parent_number")
            return _mock_temp_file(td_path, f"parcel_{pn}.xlsx")

        monkeypatch.setattr(exports, "export_efd_weekly_invoice_xlsx", _weekly)
        monkeypatch.setattr(exports, "export_postage_invoice", _postage)
        monkeypatch.setattr(exports, "export_parcel_zone_summary_xlsx", _parcel)
        monkeypatch.setattr(
            dbmod,
            "query_parcel_zone_summary",
            lambda *a, **k: {"title_name": "Test -EFD", "rows": []},
        )

        r = client.post(
            "/api/export/efd-weekly-bundle",
            json={
                "start_date": "2026-06-08",
                "end_date": "2026-06-12",
            },
        )
        assert r.status_code == 200
        data = r.get_json()
        assert len(data["saved"]) == 9
        assert len(data["failed"]) == 1
        assert data["failed"][0]["label"] == "Weekly invoice (3899)"


def test_efd_weekly_bundle_all_failed(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        monkeypatch.setattr(exports, "POSTAGE_REPORTS_DIR", td_path / "PostageReports")
        client = _client(monkeypatch, td_path / "bundle_fail.db")

        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(exports, "export_efd_weekly_invoice_xlsx", _boom)
        monkeypatch.setattr(exports, "export_postage_invoice", _boom)
        monkeypatch.setattr(exports, "export_parcel_zone_summary_xlsx", _boom)
        monkeypatch.setattr(
            dbmod,
            "query_parcel_zone_summary",
            lambda *a, **k: {"title_name": "Test", "rows": []},
        )

        r = client.post(
            "/api/export/efd-weekly-bundle",
            json={"start_date": "2026-06-08", "end_date": "2026-06-12"},
        )
        assert r.status_code == 400
        data = r.get_json()
        assert "error" in data
        assert len(data.get("failed", [])) == 10
