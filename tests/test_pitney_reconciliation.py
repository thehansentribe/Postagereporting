"""Tests for Pitney reconciliation and the parcel-profit true-up."""

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


def _seed(conn):
    conn.execute("INSERT INTO customers (customer_number, customer_name) VALUES (910, 'Recon Co')")
    conn.execute(
        "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('r1', 'b.csv', 3)"
    )
    # Three parcels: one matched-exact, one matched-mismatch, one with no print.
    pieces = [
        ("42094538<FNC1>9400100000000000000001", 7.26, "5/10/2026 10:00"),
        ("42094538<FNC1>9400100000000000000002", 5.00, "5/11/2026 10:00"),
        ("42094538<FNC1>9400100000000000000003", 3.10, "5/12/2026 10:00"),
    ]
    for impb, final, ts in pieces:
        conn.execute(
            """
            INSERT INTO billing_records (
                billing_import_id, custom_account_code, account_name, time_stamp,
                weight_oz, usps_mail_class, final_postage, fully_paid_postage,
                billing_amount, zone, impb, impb_normalized
            ) VALUES (1, 910, 'Recon Co', ?, 16.0, 'USPS GROUND ADVANTAGE', ?, ?, ?, '4', ?, ?)
            """,
            (ts, final, final + 3.0, final + 1.0, impb, impb.split("<FNC1>")[-1]),
        )
    tx = [
        # Exact match to piece 1.
        ("Postage Print", "t1", "2026-05-10", "10:00", 7.26, "9400100000000000000001", "Delivered"),
        # Mismatched amount vs piece 2 (5.00 in billing).
        ("Postage Print", "t2", "2026-05-11", "10:00", 5.75, "9400100000000000000002", "Delivered"),
        # Print with no billing record.
        ("Postage Print", "t3", "2026-05-12", "10:00", 4.00, "9400109999999999999999", "Delivered"),
        # Refund attributed to piece 1, listed at two lifecycle stages: counts once.
        ("Postage Refund", "t4", "2026-05-18", "10:00", 1.50, "9400100000000000000001", "REQUESTED"),
        ("Postage Refund", "t4", "2026-05-20", "09:00", 1.50, "9400100000000000000001", "ACCEPTED"),
        ("Postage Adjustment (Underpaid)", "t5", "2026-05-21", "10:00", 0.80, "9400100000000000000001", ""),
        ("Postage Adjustment (Overpaid)", "t6", "2026-05-22", "10:00", 0.30, "9400100000000000000001", ""),
        # Accepted refund whose tracking matches nothing (unattributed money).
        ("Postage Refund", "t7", "2026-05-23", "10:00", 2.00, "9400108888888888888888", "ACCEPTED"),
        # Pending refund: no money returned yet.
        ("Postage Refund", "t9", "2026-05-24", "10:00", 0.99, "9400100000000000000001", "REQUESTED"),
        ("Postage Fund", "t8", "2026-05-01", "00:00", 5000.00, None, ""),
    ]
    for ttype, tid, tdate, hhmm, amount, tracking, status in tx:
        conn.execute(
            """
            INSERT INTO pitney_transactions (
                transaction_id, transaction_type, transaction_date,
                transaction_datetime, amount, tracking_number, tracking_normalized, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tid, ttype, tdate, f"{tdate} {hhmm}:00", amount, tracking, tracking, status),
        )
    conn.commit()


def test_query_pitney_reconciliation(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "recon.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            _seed(conn)
            out = dbmod.query_pitney_reconciliation(conn, "2026-05-01", "2026-05-31")
        finally:
            conn.close()

        assert out["has_data"] is True
        pr = out["prints"]
        assert pr["count"] == 3
        assert pr["matched_count"] == 2
        assert pr["exact_amount_matches"] == 1
        assert len(pr["amount_mismatches"]) == 1
        mm = pr["amount_mismatches"][0]
        assert mm["tracking"] == "9400100000000000000002"
        assert mm["delta"] == pytest.approx(0.75)
        assert mm["account_code"] == 910
        assert pr["prints_without_billing"] == 1

        # One billed parcel (piece 3) has no Pitney print.
        assert out["billing"]["count"] == 3
        assert out["billing"]["without_print"] == 1

        adj = out["adjustments"]
        # Accepted refunds only (t4 counted once despite two lifecycle rows, t7);
        # the pending t9 is reported separately.
        assert adj["refund_total"] == pytest.approx(3.50)
        assert adj["refund_pending_total"] == pytest.approx(0.99)
        assert adj["refund_denied_total"] == pytest.approx(0.0)
        assert adj["underpaid_total"] == pytest.approx(0.80)
        assert adj["overpaid_total"] == pytest.approx(0.30)
        refund_rows = [r for r in adj["rows"] if r["type"] == "Postage Refund"]
        assert len(refund_rows) == 3  # t4 (once), t7, t9
        by_status = {r["status"] for r in refund_rows}
        assert by_status == {"ACCEPTED", "REQUESTED"}
        matched_flags = {r["tracking"]: r["matched"] for r in adj["rows"]}
        assert matched_flags["9400100000000000000001"] is True
        assert matched_flags["9400108888888888888888"] is False

        assert out["funds"]["count"] == 1
        assert out["funds"]["total"] == pytest.approx(5000.00)


def test_pitney_true_up_flows_into_parcel_profit_api(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "recon_api.db"
        client = _client(monkeypatch, p)
        conn = dbmod.get_connection()
        try:
            _seed(conn)
        finally:
            conn.close()

        r = client.get("/api/profit/parcels?start_date=2026-05-01&end_date=2026-05-31")
        assert r.status_code == 200
        j = r.get_json()
        c = j["computed"]

        final_total = 7.26 + 5.00 + 3.10
        billing_total = 8.26 + 6.00 + 4.10
        fee = 3 * 1.25
        assert c["supplier_profit"] == pytest.approx(round(billing_total - final_total + fee, 2))

        # True-up (unscoped): accepted refunds 3.50 (t4 once + t7), underpaid
        # 0.80, overpaid 0.30; the pending refund t9 is not money yet.
        expected_actual_cost = round(final_total + 0.80 - 0.30 - 3.50, 2)
        assert c["actual_usps_cost"] == pytest.approx(expected_actual_cost)
        assert c["supplier_profit_actual"] == pytest.approx(
            round(billing_total + fee - expected_actual_cost, 2)
        )
        labels = [ln["label"] for ln in j["lines"]]
        assert "Supplier (Lineage) Profit — trued-up" in labels
        assert j["meta"]["pitney"]["has_data"] is True

        # Scoped to the account: the unattributed accepted refund (2.00) drops out.
        r = client.get(
            "/api/profit/parcels?start_date=2026-05-01&end_date=2026-05-31&profit_accounts=910"
        )
        assert r.status_code == 200
        j = r.get_json()
        c = j["computed"]
        scoped_actual_cost = round(final_total + 0.80 - 0.30 - 1.50, 2)
        assert c["actual_usps_cost"] == pytest.approx(scoped_actual_cost)
        assert j["meta"]["pitney"]["unattributed_count"] == 1


def test_pitney_true_up_absent_without_data_and_for_flats(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "recon_none.db"
        client = _client(monkeypatch, p)
        conn = dbmod.get_connection()
        try:
            conn.execute(
                "INSERT INTO customers (customer_number, customer_name) VALUES (911, 'NoPitney')"
            )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('r2', 'b.csv', 1)"
            )
            conn.execute(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class, final_postage, fully_paid_postage,
                    billing_amount, zone
                ) VALUES (1, 911, 'NoPitney', '5/10/2026 10:00', 16.0, 'CLS', 2.0, 4.0, 3.0, '1')
                """
            )
            conn.commit()
        finally:
            conn.close()

        r = client.get("/api/profit/parcels?start_date=2026-05-01&end_date=2026-05-31")
        assert r.status_code == 200
        j = r.get_json()
        assert j["computed"]["actual_usps_cost"] is None
        assert j["computed"]["supplier_profit_actual"] is None
        assert all("trued-up" not in ln["label"] for ln in j["lines"])
        assert j["meta"]["pitney"]["has_data"] is False

        # Flats profit output has no Pitney keys at all.
        r = client.get("/api/profit/flats?start_date=2026-05-01&end_date=2026-05-31")
        j = r.get_json()
        assert "pitney" not in (j.get("meta") or {})
