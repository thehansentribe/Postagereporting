"""Tests for per-business-day report readiness (postage, parcel, WS3)."""

from __future__ import annotations

import importlib
import tempfile
from pathlib import Path

import pytest

import db as dbmod
import watcher as watchermod

pytest.importorskip("flask")


def _seed_all_three(conn, mail_date: str, *, time_stamp: str | None = None) -> None:
    ts = time_stamp or f"{int(mail_date[5:7])}/{int(mail_date[8:10])}/{mail_date[0:4]} 10:00"
    conn.execute(
        "INSERT INTO postage_imports (file_name, file_date, row_count) VALUES (?, ?, 1)",
        (f"bm_{mail_date}.csv", mail_date),
    )
    conn.execute(
        "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES (?, ?, 1)",
        (f"b_{mail_date}", f"billing_{mail_date}.csv"),
    )
    import_id = conn.execute("SELECT id FROM billing_imports").fetchone()[0]
    conn.execute(
        """
        INSERT INTO billing_records (
            billing_import_id, custom_account_code, account_name, time_stamp,
            weight_oz, usps_mail_class, billing_amount, unmatched_account
        ) VALUES (?, 1, 'Acct', ?, 1.0, 'USPS GROUND ADVANTAGE', 5.0, 0)
        """,
        (import_id, ts),
    )
    conn.execute(
        "INSERT INTO ws3_mail_runs (mail_date, mail_id) VALUES (?, '')",
        (mail_date,),
    )


def test_report_readiness_all_present(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "ready.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            _seed_all_three(conn, "2026-05-12")
            conn.commit()
            r = dbmod.query_report_readiness(conn, "2026-05-12", "2026-05-12")
        finally:
            conn.close()

    assert r["ready"] is True
    assert r["business_day_count"] == 1
    assert r["missing"]["postage"] == []
    assert r["missing"]["parcel"] == []
    assert r["missing"]["ws3_presort"] == []


def test_report_readiness_missing_ws3(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "no_ws3.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            conn.execute(
                "INSERT INTO postage_imports (file_name, file_date, row_count) VALUES ('a.csv', '2026-05-12', 1)"
            )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('b1', 'x.csv', 1)"
            )
            conn.execute(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class, billing_amount, unmatched_account
                ) VALUES (1, 1, 'Acct', '5/12/2026 10:00', 1.0, 'USPS GROUND ADVANTAGE', 5.0, 0)
                """
            )
            conn.commit()
            r = dbmod.query_report_readiness(conn, "2026-05-12", "2026-05-12")
        finally:
            conn.close()

    assert r["ready"] is False
    assert r["missing"]["ws3_presort"] == ["2026-05-12"]
    assert r["missing"]["postage"] == []
    assert r["missing"]["parcel"] == []


def test_report_readiness_multi_day_partial(monkeypatch):
    """Mon–Wed range with data only on Monday."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "partial.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            _seed_all_three(conn, "2026-05-11")
            conn.commit()
            r = dbmod.query_report_readiness(conn, "2026-05-11", "2026-05-13")
        finally:
            conn.close()

    assert r["ready"] is False
    assert r["business_day_count"] == 3
    assert r["missing"]["postage"] == ["2026-05-12", "2026-05-13"]
    assert r["missing"]["parcel"] == ["2026-05-12", "2026-05-13"]
    assert r["missing"]["ws3_presort"] == ["2026-05-12", "2026-05-13"]


def test_report_readiness_weekend_only(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "weekend.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            r = dbmod.query_report_readiness(conn, "2026-05-16", "2026-05-17")
        finally:
            conn.close()

    assert r["ready"] is False
    assert r["business_day_count"] == 0
    assert r["missing"]["postage"] == []
    assert r["missing"]["parcel"] == []
    assert r["missing"]["ws3_presort"] == []


def test_business_days_in_range(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "biz.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()

    days = dbmod._business_days_in_range("2026-05-11", "2026-05-17")
    assert days == [
        "2026-05-11",
        "2026-05-12",
        "2026-05-13",
        "2026-05-14",
        "2026-05-15",
    ]


def _flask_client(monkeypatch, db_path: Path):
    monkeypatch.setattr(dbmod, "DB_PATH", db_path)
    dbmod.init_db()
    monkeypatch.setattr(watchermod, "ensure_dirs", lambda: None)
    import app as appmod

    appmod = importlib.reload(appmod)
    monkeypatch.setattr(appmod, "_ensure_watcher", lambda: None)
    appmod.app.config.update(TESTING=True)
    return appmod.app.test_client()


def test_api_reports_readiness(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "api_ready.db"
        client = _flask_client(monkeypatch, p)
        conn = dbmod.get_connection()
        try:
            _seed_all_three(conn, "2026-05-12")
            conn.commit()
        finally:
            conn.close()

        r = client.get(
            "/api/reports/readiness?start_date=2026-05-12&end_date=2026-05-12"
        )
        assert r.status_code == 200
        j = r.get_json()
        assert j["ready"] is True
        assert "missing" in j
        assert "business_day_count" in j

        r2 = client.get("/api/reports/readiness")
        assert r2.status_code == 400
