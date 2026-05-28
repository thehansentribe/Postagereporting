"""Tests for EFD daily consolidated volumes export API."""

from __future__ import annotations

import importlib
import tempfile
from pathlib import Path

import pytest

import db as dbmod
import exports
import exports_consolidated_volumes
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


def test_efd_report_scope_label() -> None:
    assert exports.efd_report_scope_label(3901) == "BCBS (3901)"
    assert exports.efd_report_scope_label(3899) == "GEHA (3899)"


def test_efd_daily_volumes_requires_parent_number(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        client = _client(monkeypatch, Path(td) / "efd_daily_req.db")
        r = client.get(
            "/api/export/efd-daily-volumes-xlsx?start_date=2026-04-01&end_date=2026-04-01"
        )
        assert r.status_code == 400
        assert "parent_number" in r.get_json().get("error", "").lower()


def test_efd_daily_volumes_invalid_parent_number(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        client = _client(monkeypatch, Path(td) / "efd_daily_inv.db")
        r = client.get(
            "/api/export/efd-daily-volumes-xlsx?"
            "start_date=2026-04-01&end_date=2026-04-01&parent_number=9999"
        )
        assert r.status_code == 400
        assert "3901" in r.get_json().get("error", "")


def test_efd_daily_volumes_no_data(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        client = _client(monkeypatch, Path(td) / "efd_daily_empty.db")
        r = client.get(
            "/api/export/efd-daily-volumes-xlsx?"
            "start_date=2026-04-01&end_date=2026-04-01&parent_number=3901"
        )
        assert r.status_code == 400
        assert "no data" in r.get_json().get("error", "").lower()


def test_efd_daily_volumes_success(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        client = _client(monkeypatch, Path(td) / "efd_daily_ok.db")
        out = Path(td) / "mock_volumes.xlsx"
        out.write_bytes(b"pk")

        def _fake_export(*args, **kwargs):
            assert args[2] == 3901
            assert args[3] is None
            assert kwargs["account_scope_label"] == "BCBS (3901)"
            return out

        monkeypatch.setattr(
            exports_consolidated_volumes,
            "export_consolidated_volumes_xlsx",
            _fake_export,
        )
        r = client.get(
            "/api/export/efd-daily-volumes-xlsx?"
            "start_date=2026-04-01&end_date=2026-04-01&parent_number=3901"
        )
        assert r.status_code == 200
        assert (
            r.headers.get("Content-Type")
            == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        disp = r.headers.get("Content-Disposition") or ""
        assert "BCBS" in disp and "3901" in disp
