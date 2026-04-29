"""Tests for Profit Report (Flats) JSON endpoint.

These tests are skipped when Flask isn't installed in the test environment.
"""

from __future__ import annotations

import importlib
import tempfile
from pathlib import Path

import pytest

import db as dbmod
import watcher as watchermod

pytest.importorskip("flask")


def _client(monkeypatch, db_path: Path):
    """
    Build a Flask test client against an isolated temp DB.

    Notes:
    - app.py runs db.init_db() at import time; we set DB_PATH before reloading.
    - app.py also calls watcher.ensure_dirs() at import time; we patch it to no-op.
    - requests call a before_request hook that starts the watcher thread; patch that to no-op.
    """
    monkeypatch.setattr(dbmod, "DB_PATH", db_path)
    dbmod.init_db()

    monkeypatch.setattr(watchermod, "ensure_dirs", lambda: None)
    import app as appmod

    appmod = importlib.reload(appmod)
    monkeypatch.setattr(appmod, "_ensure_watcher", lambda: None)
    appmod.app.config.update(TESTING=True)
    return appmod.app.test_client()


def test_api_profit_flats_no_data_returns_404_with_export_message(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "profit_empty.db"
        client = _client(monkeypatch, p)
        r = client.get("/api/profit/flats?start_date=2026-04-01&end_date=2026-04-07")
        assert r.status_code == 404
        j = r.get_json()
        assert j["empty"] is True
        assert "No WS3 flats profit rows found for this date range/account scope." in j["error"]
        # Meta still returned for UI to render context.
        assert j["meta"]["start_date"] == "2026-04-01"
        assert j["meta"]["end_date"] == "2026-04-07"


def test_api_profit_flats_success_returns_totals_and_rows(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "profit_ok.db"
        client = _client(monkeypatch, p)

        conn = dbmod.get_connection()
        try:
            # Parent account used for WS3 profile scope.
            conn.execute(
                "INSERT INTO customers (customer_number, customer_name, parent_number, parent_name) VALUES (100, 'Parent Co', NULL, NULL)"
            )
            conn.execute(
                "INSERT INTO ws3_netsort_customers (customer_code, customer_name) VALUES ('301079', 'Acme Dept')"
            )
            conn.execute(
                "INSERT INTO ws3_mail_runs (mail_date, mail_id, run_datetime, source_file_name) VALUES ('2026-04-03', 'M1', '2026-04-03 12:00:00', 't.xlsx')"
            )
            run_id = conn.execute(
                "SELECT run_id FROM ws3_mail_runs WHERE mail_date='2026-04-03' AND mail_id='M1'"
            ).fetchone()["run_id"]
            conn.execute(
                "INSERT INTO ws3_profiles (profile_name, parent_customer_number, reject_fee) VALUES ('Profile 1', 100, NULL)"
            )
            profile_id = conn.execute(
                "SELECT id FROM ws3_profiles WHERE profile_name='Profile 1'"
            ).fetchone()["id"]
            # Minimal WS3 detail row with required fields (num_pieces>0, usps_cost_per_piece non-null).
            conn.execute(
                """
                INSERT INTO ws3_mail_detail (
                    run_id, profile_id, customer_code, rate_type,
                    postage_claimed, postage_applied, num_pieces, pcs_accepted, pcs_rejected,
                    cost_per_piece, usps_cost_per_piece
                ) VALUES (?, ?, '301079', 'ADC Auto', 10.00, 9.70, 10, 10, 0, 1.00, 1.0000)
                """,
                (run_id, profile_id),
            )
            conn.commit()
        finally:
            conn.close()

        r = client.get(
            "/api/profit/flats?start_date=2026-04-01&end_date=2026-04-07&parent_number=100&discount=0.10"
        )
        assert r.status_code == 200
        j = r.get_json()
        assert "meta" in j
        assert "totals" in j
        assert "rate_summary" in j
        assert "detail" in j
        assert j["meta"]["sell_to_rate"] == pytest.approx(1.53, rel=1e-9)
        assert j["totals"]["total_pieces"] == 10
        assert len(j["rate_summary"]) >= 1
        assert len(j["detail"]) >= 1

