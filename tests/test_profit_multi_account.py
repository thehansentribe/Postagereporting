"""Multi-account profit scope for parcel billing totals (``profit_account_ids``)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import db as dbmod


def test_query_parcel_profit_totals_union_parent_and_child_ids(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "profit_multi_parcel.db"
        monkeypatch.setattr(dbmod, "DB_PATH", Path(p))
        dbmod.init_db()
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
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('bx', 'b.csv', 2)"
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
                ) VALUES (1, 201, 'C2', '4/11/2026 10:00', 16.0, 'CLS', 3, 3, 4, '1')
                """
            )
            conn.commit()

            one = dbmod.query_parcel_profit_totals(
                conn,
                "2026-04-01",
                "2026-04-30",
                parent_number=None,
                customer_number=101,
                show_parents=True,
                show_main=True,
                profit_account_ids=None,
            )
            multi = dbmod.query_parcel_profit_totals(
                conn,
                "2026-04-01",
                "2026-04-30",
                parent_number=None,
                customer_number=None,
                show_parents=True,
                show_main=True,
                profit_account_ids=[101, 201],
            )
            assert one["parcel_count"] == 1
            assert multi["parcel_count"] == 2
            assert multi["total_final_postage"] == pytest.approx(4.0, rel=1e-9)
            assert multi["total_billing_amount"] == pytest.approx(6.0, rel=1e-9)
        finally:
            conn.close()


def test_query_parcel_profit_totals_child_scope_includes_sibling_billing(monkeypatch):
    """Toolbar child # scopes parcel billing to the whole effective-parent family (WS3 parity)."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "profit_child_rollup.db"
        monkeypatch.setattr(dbmod, "DB_PATH", Path(p))
        dbmod.init_db()
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
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('bx', 'b.csv', 2)"
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

            by_child = dbmod.query_parcel_profit_totals(
                conn,
                "2026-04-01",
                "2026-04-30",
                parent_number=None,
                customer_number=101,
                show_parents=True,
                show_main=True,
                profit_account_ids=None,
            )
            by_profit_child_only = dbmod.query_parcel_profit_totals(
                conn,
                "2026-04-01",
                "2026-04-30",
                parent_number=None,
                customer_number=None,
                show_parents=True,
                show_main=True,
                profit_account_ids=[101],
            )
            assert by_child["parcel_count"] == 2
            assert by_profit_child_only["parcel_count"] == 2
            assert by_child["total_billing_amount"] == pytest.approx(4.0, rel=1e-9)
            assert by_profit_child_only["total_billing_amount"] == pytest.approx(4.0, rel=1e-9)
        finally:
            conn.close()


def test_query_parcel_profit_totals_profit_ids_rolls_up_full_parent_family(monkeypatch):
    """Selecting children under one parent includes all sibling billing (WS3 / parcel parity)."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "profit_three_siblings.db"
        monkeypatch.setattr(dbmod, "DB_PATH", Path(p))
        dbmod.init_db()
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
                    (103, "C3", 100, "P1"),
                ],
            )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('bx', 'b.csv', 3)"
            )
            for acc in (101, 102, 103):
                conn.execute(
                    """
                    INSERT INTO billing_records (
                        billing_import_id, custom_account_code, account_name, time_stamp,
                        weight_oz, usps_mail_class,
                        final_postage, fully_paid_postage, billing_amount,
                        zone
                    ) VALUES (1, ?, 'x', '4/10/2026 10:00', 16.0, 'CLS', 1, 1, 1, '1')
                    """,
                    (acc,),
                )
            conn.commit()
            only_ab = dbmod.query_parcel_profit_totals(
                conn,
                "2026-04-01",
                "2026-04-30",
                parent_number=None,
                customer_number=None,
                show_parents=True,
                show_main=True,
                profit_account_ids=[101, 102],
            )
            assert only_ab["parcel_count"] == 3
            assert only_ab["total_billing_amount"] == pytest.approx(3.0, rel=1e-9)
        finally:
            conn.close()


def test_parse_profit_accounts_csv_rejects_too_many_distinct_ids(monkeypatch):
    monkeypatch.setattr(dbmod, "MAX_PROFIT_ACCOUNT_IDS", 2)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "profit_parse_cap.db"
        monkeypatch.setattr(dbmod, "DB_PATH", Path(p))
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            with pytest.raises(ValueError, match="at most 2"):
                dbmod.parse_profit_accounts_csv(conn, "10,20,30")
        finally:
            conn.close()


def test_parse_profit_account_ids_from_json_list_rejects_too_many_distinct(monkeypatch):
    monkeypatch.setattr(dbmod, "MAX_PROFIT_ACCOUNT_IDS", 2)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "profit_json_cap.db"
        monkeypatch.setattr(dbmod, "DB_PATH", Path(p))
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            with pytest.raises(ValueError, match="at most 2"):
                dbmod.parse_profit_account_ids_from_json_list(conn, [1, 2, 3])
        finally:
            conn.close()


def test_query_parcel_profit_totals_many_profit_ids_all_included(monkeypatch):
    """More than 40 profit ids are all applied (no silent prefix cap)."""
    n = 45
    base = 9000
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "profit_many_ids.db"
        monkeypatch.setattr(dbmod, "DB_PATH", Path(p))
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            for i in range(base, base + n):
                conn.execute(
                    "INSERT INTO customers (customer_number, customer_name, parent_number, parent_name) "
                    "VALUES (?, ?, NULL, NULL)",
                    (i, f"A{i}"),
                )
            conn.execute(
                "INSERT INTO billing_imports (billing_id, file_name, row_count) VALUES ('bx', 'b.csv', ?)",
                (n,),
            )
            for i in range(base, base + n):
                conn.execute(
                    """
                    INSERT INTO billing_records (
                        billing_import_id, custom_account_code, account_name, time_stamp,
                        weight_oz, usps_mail_class,
                        final_postage, fully_paid_postage, billing_amount,
                        zone
                    ) VALUES (1, ?, 'x', '4/10/2026 10:00', 16.0, 'CLS', 1, 1, 1, '1')
                    """,
                    (i,),
                )
            conn.commit()
            ids = list(range(base, base + n))
            out = dbmod.query_parcel_profit_totals(
                conn,
                "2026-04-01",
                "2026-04-30",
                parent_number=None,
                customer_number=None,
                show_parents=True,
                show_main=True,
                profit_account_ids=ids,
            )
            assert out["parcel_count"] == n
            assert out["total_billing_amount"] == pytest.approx(float(n), rel=1e-9)
        finally:
            conn.close()
