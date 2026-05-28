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
        assert j["meta"]["efd_parcel_fee"] == pytest.approx(1.25, rel=1e-9)
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

        # Fee = qty * default parcel fee (1.25)
        assert j["computed"]["postage_fee"] == pytest.approx(2.50, rel=1e-9)

        # Confirmed formulas:
        # Lineage Revenue = billing_amount - final_postage + postage_fee
        assert j["computed"]["lineage_revenue"] == pytest.approx(7.00 - 2.00 + 2.50, rel=1e-9)
        # EFD Profit = fully_paid_postage - billing_amount - postage_fee
        assert j["computed"]["efd_profit"] == pytest.approx(3.00 - 7.00 - 2.50, rel=1e-9)
        assert j["meta"]["efd_parcel_fee"] == pytest.approx(1.25, rel=1e-9)


def test_api_profit_parcels_meta_splits_parcel_fee_and_efd_parcel_fee(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "parcel_meta_split.db"
        client = _client(monkeypatch, p)
        conn = dbmod.get_connection()
        try:
            conn.execute(
                "INSERT INTO customers (customer_number, customer_name, parent_number, parent_name) VALUES (904, 'T', NULL, NULL)"
            )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('b9', 'b.csv', 1)"
            )
            conn.execute(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class,
                    final_postage, fully_paid_postage, billing_amount,
                    zone
                ) VALUES (1, 904, 'T', '4/1/2026 10:00', 16.0, 'CLS', 1.0, 2.0, 3.0, '1')
                """
            )
            conn.commit()
        finally:
            conn.close()
        r = client.get(
            "/api/profit/parcels?start_date=2026-04-01&end_date=2026-04-30&customer_number=904"
            "&parcel_fee=0.50&efd_parcel_fee=3.75"
        )
        assert r.status_code == 200
        j = r.get_json()
        assert j["meta"]["parcel_fee"] == pytest.approx(0.50, rel=1e-9)
        assert j["meta"]["efd_parcel_fee"] == pytest.approx(3.75, rel=1e-9)
        assert j["computed"]["postage_fee"] == pytest.approx(0.50, rel=1e-9)


def test_api_profit_parcels_single_piece_full_rate_no_fee_or_spread(monkeypatch):
    """One parcel with billing == final == fully paid: no per-piece fee, no negative spread."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "parcel_profit_single_full.db"
        client = _client(monkeypatch, p)

        conn = dbmod.get_connection()
        try:
            conn.execute(
                "INSERT INTO customers (customer_number, customer_name, parent_number, parent_name) VALUES (901, 'Q', NULL, NULL)"
            )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('b2', 'b.csv', 1)"
            )
            conn.execute(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class,
                    final_postage, fully_paid_postage, billing_amount,
                    zone
                ) VALUES (1, 901, 'Q', '4/1/2026 10:00', 16.0, 'CLS', 12.34, 12.34, 12.34, '1')
                """
            )
            conn.commit()
        finally:
            conn.close()

        r = client.get("/api/profit/parcels?start_date=2026-04-01&end_date=2026-04-30&customer_number=901")
        assert r.status_code == 200
        j = r.get_json()

        assert j["raw"]["parcel_count"] == 1
        assert j["raw"]["total_final_postage"] == pytest.approx(12.34, rel=1e-9)
        assert j["raw"]["total_fully_paid_postage"] == pytest.approx(12.34, rel=1e-9)
        assert j["raw"]["total_billing_amount"] == pytest.approx(12.34, rel=1e-9)

        assert j["computed"]["single_full_rate_pass_through"] is True
        assert j["computed"]["postage_fee"] == 0.0
        assert j["computed"]["lineage_revenue"] == 0.0
        assert j["computed"]["efd_profit"] == 0.0


def test_api_profit_parcels_single_piece_not_full_rate_still_applies_fee(monkeypatch):
    """One parcel with mismatched amounts: standard fee and formulas apply."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "parcel_profit_single_discount.db"
        client = _client(monkeypatch, p)

        conn = dbmod.get_connection()
        try:
            conn.execute(
                "INSERT INTO customers (customer_number, customer_name, parent_number, parent_name) VALUES (902, 'R', NULL, NULL)"
            )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('b3', 'b.csv', 1)"
            )
            conn.execute(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class,
                    final_postage, fully_paid_postage, billing_amount,
                    zone
                ) VALUES (1, 902, 'R', '4/1/2026 10:00', 16.0, 'CLS', 5.00, 5.00, 8.00, '1')
                """
            )
            conn.commit()
        finally:
            conn.close()

        r = client.get("/api/profit/parcels?start_date=2026-04-01&end_date=2026-04-30&customer_number=902")
        assert r.status_code == 200
        j = r.get_json()

        assert j["raw"]["parcel_count"] == 1
        assert j["computed"]["single_full_rate_pass_through"] is False
        assert j["computed"]["postage_fee"] == pytest.approx(1.25, rel=1e-9)
        assert j["computed"]["lineage_revenue"] == pytest.approx(8.00 - 5.00 + 1.25, rel=1e-9)
        assert j["computed"]["efd_profit"] == pytest.approx(5.00 - 8.00 - 1.25, rel=1e-9)


def test_api_profit_parcels_custom_parcel_fee_and_line_label(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "parcel_profit_fee.db"
        client = _client(monkeypatch, p)

        conn = dbmod.get_connection()
        try:
            conn.execute(
                "INSERT INTO customers (customer_number, customer_name, parent_number, parent_name) VALUES (903, 'S', NULL, NULL)"
            )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('b4', 'b.csv', 2)"
            )
            conn.executemany(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class,
                    final_postage, fully_paid_postage, billing_amount,
                    zone
                ) VALUES (1, 903, 'S', '4/1/2026 10:00', 16.0, 'CLS', ?, ?, ?, '1')
                """,
                [
                    (1.0, 1.0, 2.0),
                    (1.0, 1.0, 2.0),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        r = client.get(
            "/api/profit/parcels?start_date=2026-04-01&end_date=2026-04-30&customer_number=903&parcel_fee=0.25"
        )
        assert r.status_code == 200
        j = r.get_json()
        assert j["meta"]["parcel_fee"] == pytest.approx(0.25, rel=1e-9)
        assert j["computed"]["postage_fee"] == pytest.approx(0.50, rel=1e-9)
        line6 = next(x for x in j["lines"] if x.get("line_no") == 6)
        assert "0.25" in line6["label"]
        assert line6["value"] == pytest.approx(0.50, rel=1e-9)


def test_api_profit_parcels_profit_accounts_multi_scope(monkeypatch):
    """Union billing rows when multiple customer/parent ids are selected."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "parcel_profit_multi.db"
        client = _client(monkeypatch, p)

        conn = dbmod.get_connection()
        try:
            conn.execute(
                "INSERT INTO customers (customer_number, customer_name, parent_number, parent_name) "
                "VALUES (100, 'P1', NULL, NULL)"
            )
            conn.execute(
                "INSERT INTO customers (customer_number, customer_name, parent_number, parent_name) "
                "VALUES (101, 'C1', 100, 'P1')"
            )
            conn.execute(
                "INSERT INTO customers (customer_number, customer_name, parent_number, parent_name) "
                "VALUES (200, 'P2', NULL, NULL)"
            )
            conn.execute(
                "INSERT INTO customers (customer_number, customer_name, parent_number, parent_name) "
                "VALUES (201, 'C2', 200, 'P2')"
            )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('bm', 'b.csv', 2)"
            )
            conn.execute(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class,
                    final_postage, fully_paid_postage, billing_amount,
                    zone
                ) VALUES (1, 101, 'C1', '4/10/2026 10:00', 16.0, 'CLS', 1, 1, 2, '1')
                """
            )
            conn.execute(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class,
                    final_postage, fully_paid_postage, billing_amount,
                    zone
                ) VALUES (1, 201, 'C2', '4/11/2026 10:00', 16.0, 'CLS', 1, 1, 2, '1')
                """
            )
            conn.commit()
        finally:
            conn.close()

        r = client.get(
            "/api/profit/parcels?start_date=2026-04-01&end_date=2026-04-30"
            "&profit_accounts=101,201&show_parents=true&show_main=true"
        )
        assert r.status_code == 200
        j = r.get_json()
        assert j["raw"]["parcel_count"] == 2
        assert j["computed"]["postage_fee"] == pytest.approx(2.50, rel=1e-9)


def test_api_profit_parcels_invalid_profit_accounts(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "parcel_profit_bad.db"
        client = _client(monkeypatch, p)
        r = client.get(
            "/api/profit/parcels?start_date=2026-04-01&end_date=2026-04-30&profit_accounts=abc"
        )
        assert r.status_code == 400


def test_api_profit_parcels_customer_number_includes_sibling_under_parent(monkeypatch):
    """Child toolbar scope rolls up to parent family (same as WS3 profit)."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "parcel_profit_sibling_api.db"
        client = _client(monkeypatch, p)

        conn = dbmod.get_connection()
        try:
            conn.executemany(
                """
                INSERT INTO customers (customer_number, customer_name, parent_number, parent_name)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (100, "P1", None, None),
                    (101, "C1", 100, "P1"),
                    (102, "C2", 100, "P1"),
                ],
            )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('bs', 'b.csv', 2)"
            )
            conn.executemany(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class,
                    final_postage, fully_paid_postage, billing_amount,
                    zone
                ) VALUES (1, ?, 'x', '4/10/2026 10:00', 16.0, 'CLS', 1, 1, 2, '1')
                """,
                [(101,), (102,)],
            )
            conn.commit()
        finally:
            conn.close()

        r = client.get(
            "/api/profit/parcels?start_date=2026-04-01&end_date=2026-04-30"
            "&customer_number=101&show_parents=true&show_main=true"
        )
        assert r.status_code == 200
        j = r.get_json()
        assert j["raw"]["parcel_count"] == 2
        assert j["raw"]["total_billing_amount"] == pytest.approx(4.0, rel=1e-9)


def test_api_profit_parcels_post_json_matches_get_profit_accounts(monkeypatch):
    """POST body carries profit_account_ids without query-string length limits."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "parcel_profit_post_multi.db"
        client = _client(monkeypatch, p)

        conn = dbmod.get_connection()
        try:
            conn.execute(
                "INSERT INTO customers (customer_number, customer_name, parent_number, parent_name) "
                "VALUES (100, 'P1', NULL, NULL)"
            )
            conn.execute(
                "INSERT INTO customers (customer_number, customer_name, parent_number, parent_name) "
                "VALUES (101, 'C1', 100, 'P1')"
            )
            conn.execute(
                "INSERT INTO customers (customer_number, customer_name, parent_number, parent_name) "
                "VALUES (200, 'P2', NULL, NULL)"
            )
            conn.execute(
                "INSERT INTO customers (customer_number, customer_name, parent_number, parent_name) "
                "VALUES (201, 'C2', 200, 'P2')"
            )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('bm', 'b.csv', 2)"
            )
            conn.execute(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class,
                    final_postage, fully_paid_postage, billing_amount,
                    zone
                ) VALUES (1, 101, 'C1', '4/10/2026 10:00', 16.0, 'CLS', 1, 1, 2, '1')
                """
            )
            conn.execute(
                """
                INSERT INTO billing_records (
                    billing_import_id, custom_account_code, account_name, time_stamp,
                    weight_oz, usps_mail_class,
                    final_postage, fully_paid_postage, billing_amount,
                    zone
                ) VALUES (1, 201, 'C2', '4/11/2026 10:00', 16.0, 'CLS', 1, 1, 2, '1')
                """
            )
            conn.commit()
        finally:
            conn.close()

        body = {
            "start_date": "2026-04-01",
            "end_date": "2026-04-30",
            "show_parents": True,
            "show_main": True,
            "profit_account_ids": [101, 201],
            "parcel_fee": 0.5,
        }
        r_post = client.post(
            "/api/profit/parcels",
            json=body,
            content_type="application/json",
        )
        assert r_post.status_code == 200
        j_post = r_post.get_json()

        r_get = client.get(
            "/api/profit/parcels?start_date=2026-04-01&end_date=2026-04-30"
            "&profit_accounts=101,201&show_parents=true&show_main=true&parcel_fee=0.5"
        )
        assert r_get.status_code == 200
        j_get = r_get.get_json()

        assert j_post["raw"] == j_get["raw"]
        assert j_post["computed"] == j_get["computed"]


def test_api_profit_parcels_post_rejects_too_many_profit_ids(monkeypatch):
    monkeypatch.setattr(dbmod, "MAX_PROFIT_ACCOUNT_IDS", 2)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "parcel_profit_post_cap.db"
        client = _client(monkeypatch, p)
        r = client.post(
            "/api/profit/parcels",
            json={
                "start_date": "2026-04-01",
                "end_date": "2026-04-30",
                "show_parents": True,
                "show_main": True,
                "profit_account_ids": [1, 2, 3],
            },
            content_type="application/json",
        )
        assert r.status_code == 400
        assert "at most 2" in r.get_json().get("error", "")

