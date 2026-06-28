"""Tests for NOCLASS / OTHERCLS postage detection (db query + API route)."""

from __future__ import annotations

import importlib
import tempfile
from pathlib import Path

import pytest

import db as dbmod
import watcher as watchermod

pytest.importorskip("flask")


def _seed(conn) -> None:
    # Parent 500 with child 501; standalone account 600 (out of scope for 500).
    conn.executemany(
        "INSERT INTO customers (customer_number, customer_name, parent_number, parent_name) VALUES (?, ?, ?, ?)",
        [
            (500, "Parent Co", None, None),
            (501, "Child Co", 500, "Parent Co"),
            (600, "Other Co", None, None),
        ],
    )
    conn.execute(
        "INSERT INTO postage_imports (file_name, file_date, row_count) VALUES ('p.csv', '2026-06-11', 5)"
    )
    conn.executemany(
        """
        INSERT INTO postage_data (
            import_id, file_date, account_code, mail_class,
            weight_oz, pieces, total_cost, unmatched_account
        ) VALUES (1, ?, ?, ?, ?, ?, ?, 0)
        """,
        [
            # Child of parent 500: one NOCLASS, one OTHERCLS, plus a normal class.
            ("2026-06-11", 501, "NOCLASS", 2.0, 7, 1.0),
            ("2026-06-11", 501, "OtherCls", 3.0, 4, 2.0),
            ("2026-06-11", 501, "1ClFlat", 2.0, 10, 5.0),
            # Out-of-range NOCLASS row should be excluded.
            ("2026-06-20", 501, "NOCLASS", 1.0, 99, 1.0),
            # Unrelated account NOCLASS row should not appear for parent 500.
            ("2026-06-11", 600, "NOCLASS", 1.0, 50, 1.0),
        ],
    )
    conn.commit()


def _client(monkeypatch, db_path: Path):
    monkeypatch.setattr(dbmod, "DB_PATH", db_path)
    dbmod.init_db()
    monkeypatch.setattr(watchermod, "ensure_dirs", lambda: None)
    import app as appmod

    appmod = importlib.reload(appmod)
    monkeypatch.setattr(appmod, "_ensure_watcher", lambda: None)
    appmod.app.config.update(TESTING=True)
    return appmod.app.test_client()


def test_query_noclass_records_parent_scope(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "noclass_parent.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            _seed(conn)
            rows = dbmod.query_noclass_records(
                conn, "2026-06-08", "2026-06-12", parent_number=500
            )
        finally:
            conn.close()

    # Only the in-range NOCLASS + OTHERCLS rows for the parent's child.
    assert len(rows) == 2
    by_class = {r["mail_class"]: r for r in rows}
    assert set(by_class) == {"NOCLASS", "OTHERCLS"}
    assert by_class["NOCLASS"]["pieces"] == 7
    assert by_class["OTHERCLS"]["pieces"] == 4
    assert all(r["account_code"] == 501 for r in rows)
    assert all(r["customer_name"] == "Child Co" for r in rows)
    assert all(r["file_date"] == "2026-06-11" for r in rows)


def test_query_noclass_records_no_account_returns_empty(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "noclass_none.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            _seed(conn)
            rows = dbmod.query_noclass_records(conn, "2026-06-08", "2026-06-12")
        finally:
            conn.close()

    assert rows == []


def test_api_postage_noclass_endpoint(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "noclass_api.db"
        client = _client(monkeypatch, db_path)
        conn = dbmod.get_connection()
        try:
            _seed(conn)
        finally:
            conn.close()

        # Child scope (single account).
        r = client.get(
            "/api/postage/noclass?start_date=2026-06-08&end_date=2026-06-12&customer_number=501"
        )
        assert r.status_code == 200
        records = r.get_json()["records"]
        assert len(records) == 2
        assert {rec["mail_class"] for rec in records} == {"NOCLASS", "OTHERCLS"}

        # No account selected -> empty records.
        r2 = client.get(
            "/api/postage/noclass?start_date=2026-06-08&end_date=2026-06-12"
        )
        assert r2.status_code == 200
        assert r2.get_json()["records"] == []

        # Missing dates -> 400.
        r3 = client.get("/api/postage/noclass?customer_number=501")
        assert r3.status_code == 400
