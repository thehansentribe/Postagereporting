"""Tests for POST /api/export/daily-reports (daily report set generation)."""

from __future__ import annotations

import importlib
import tempfile
from pathlib import Path

import pytest

import db as dbmod
import exports as exportsmod
import watcher as watchermod

pytest.importorskip("flask")


def _flask_client(monkeypatch, db_path: Path):
    monkeypatch.setattr(dbmod, "DB_PATH", db_path)
    dbmod.init_db()
    monkeypatch.setattr(watchermod, "ensure_dirs", lambda: None)
    import app as appmod

    appmod = importlib.reload(appmod)
    monkeypatch.setattr(appmod, "_ensure_watcher", lambda: None)
    appmod.app.config.update(TESTING=True)
    return appmod.app.test_client()


def _seed_customers_and_flats(conn, file_date: str) -> None:
    for cn, name in [
        (3906, "KC Presort LLC"),
        (3901, "BCBS"),
        (3899, "GEHA"),
        (3900, "Zinnia"),
    ]:
        conn.execute(
            "INSERT INTO customers (customer_number, customer_name) VALUES (?, ?)",
            (cn, name),
        )
    conn.execute(
        "INSERT INTO postage_imports (file_name, file_date, row_count) VALUES ('x.csv', ?, 1)",
        (file_date,),
    )
    for account in (3906, 3901, 3899, 3900):
        conn.execute(
            """
            INSERT INTO postage_data (
                import_id, file_date, account_code, mail_class,
                weight_oz, pieces, total_cost, unmatched_account
            ) VALUES (1, ?, ?, '1ClFlat', 2.0, 5, 10.0, 0)
            """,
            (file_date, account),
        )


def test_daily_reports_api_single_day_and_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(exportsmod, "POSTAGE_REPORTS_DIR", tmp_path / "PostageReports")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "api_daily.db"
        client = _flask_client(monkeypatch, p)
        conn = dbmod.get_connection()
        try:
            _seed_customers_and_flats(conn, "2026-07-08")
            conn.commit()
        finally:
            conn.close()

        r = client.post("/api/export/daily-reports", json={"report_date": "2026-07-08"})
        assert r.status_code == 200
        j = r.get_json()
        assert len(j["generated"]) == 1
        assert j["skipped"] == []
        summary = j["generated"][0]
        assert summary["report_date"] == "2026-07-08"
        assert summary["complete"] is True
        assert len(summary["saved"]) == 4

        # Idempotent: second call skips the already-complete day.
        r2 = client.post("/api/export/daily-reports", json={"report_date": "2026-07-08"})
        assert r2.status_code == 200
        j2 = r2.get_json()
        assert j2["generated"] == []
        assert j2["skipped"] == ["2026-07-08"]


def test_daily_reports_api_range(monkeypatch, tmp_path):
    monkeypatch.setattr(exportsmod, "POSTAGE_REPORTS_DIR", tmp_path / "PostageReports")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "api_range.db"
        client = _flask_client(monkeypatch, p)
        conn = dbmod.get_connection()
        try:
            _seed_customers_and_flats(conn, "2026-07-08")
            conn.commit()
        finally:
            conn.close()

        # Wed–Thu (2 business days); only Wed has flats data.
        r = client.post(
            "/api/export/daily-reports",
            json={"start_date": "2026-07-08", "end_date": "2026-07-09"},
        )
        assert r.status_code == 200
        j = r.get_json()
        assert len(j["generated"]) == 2
        dates = {g["report_date"] for g in j["generated"]}
        assert dates == {"2026-07-08", "2026-07-09"}
        by_date = {g["report_date"]: g for g in j["generated"]}
        assert by_date["2026-07-08"]["complete"] is True
        # Thu has no flats data -> flats report missing.
        assert by_date["2026-07-09"]["complete"] is False


def test_daily_reports_api_requires_dates(monkeypatch, tmp_path):
    monkeypatch.setattr(exportsmod, "POSTAGE_REPORTS_DIR", tmp_path / "PostageReports")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "api_bad.db"
        client = _flask_client(monkeypatch, p)
        r = client.post("/api/export/daily-reports", json={})
        assert r.status_code == 400
