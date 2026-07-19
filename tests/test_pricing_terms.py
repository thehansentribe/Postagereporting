"""Tests for stored pricing terms (supplier -> EFD -> customer) and knob resolution."""

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


def test_pricing_terms_seeded_with_defaults(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "terms.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            terms = dbmod.get_pricing_terms(conn)
            assert terms["effective_date"] == dbmod.FLAT_RATE_BASELINE_DATE
            assert terms["flats_customer_discount"] == 0.10
            assert terms["flats_efd_discount"] == 0.23
            assert terms["parcel_customer_discount"] == 0.25
            assert terms["parcel_fee_per_piece"] == 1.25
        finally:
            conn.close()


def test_pricing_terms_as_of_resolution(monkeypatch):
    """Latest revision on-or-before as_of wins; earlier dates fall back to earliest row."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "terms_asof.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            dbmod.upsert_pricing_terms(
                conn,
                "2026-07-01",
                flats_customer_discount=0.12,
                flats_efd_discount=0.26,
                parcel_customer_discount=0.30,
                parcel_fee_per_piece=1.50,
                notes="new agreement",
            )
            conn.commit()
            before = dbmod.get_pricing_terms(conn, as_of_date="2026-06-30")
            assert before["effective_date"] == dbmod.FLAT_RATE_BASELINE_DATE
            assert before["flats_customer_discount"] == 0.10
            after = dbmod.get_pricing_terms(conn, as_of_date="2026-07-01")
            assert after["effective_date"] == "2026-07-01"
            assert after["flats_customer_discount"] == 0.12
            assert after["parcel_fee_per_piece"] == 1.50
            latest = dbmod.get_pricing_terms(conn)
            assert latest["effective_date"] == "2026-07-01"
        finally:
            conn.close()


def test_pricing_terms_upsert_validation(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "terms_valid.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            with pytest.raises(ValueError, match="effective_date"):
                dbmod.upsert_pricing_terms(
                    conn,
                    "07/01/2026",
                    flats_customer_discount=0.1,
                    flats_efd_discount=0.2,
                    parcel_customer_discount=0.25,
                    parcel_fee_per_piece=1.25,
                )
            with pytest.raises(ValueError, match="non-negative"):
                dbmod.upsert_pricing_terms(
                    conn,
                    "2026-07-01",
                    flats_customer_discount=-0.1,
                    flats_efd_discount=0.2,
                    parcel_customer_discount=0.25,
                    parcel_fee_per_piece=1.25,
                )
            with pytest.raises(ValueError, match="must be a number"):
                dbmod.upsert_pricing_terms(
                    conn,
                    "2026-07-01",
                    flats_customer_discount="abc",
                    flats_efd_discount=0.2,
                    parcel_customer_discount=0.25,
                    parcel_fee_per_piece=1.25,
                )
            with pytest.raises(ValueError, match="baseline"):
                dbmod.delete_pricing_terms(conn, dbmod.FLAT_RATE_BASELINE_DATE)
        finally:
            conn.close()


def test_pricing_terms_api_get_put_delete(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "terms_api.db"
        client = _client(monkeypatch, p)

        r = client.get("/api/system/pricing-terms")
        assert r.status_code == 200
        j = r.get_json()
        assert j["current"]["flats_efd_discount"] == 0.23
        assert len(j["revisions"]) == 1

        r = client.put(
            "/api/system/pricing-terms",
            json={
                "effective_date": "2026-08-01",
                "flats_customer_discount": 0.11,
                "flats_efd_discount": 0.24,
                "parcel_customer_discount": 0.26,
                "parcel_fee_per_piece": 1.35,
                "notes": "test revision",
            },
        )
        assert r.status_code == 200
        j = r.get_json()
        assert len(j["revisions"]) == 2

        r = client.put(
            "/api/system/pricing-terms",
            json={"effective_date": "2026-08-01", "flats_customer_discount": -1,
                  "flats_efd_discount": 0.2, "parcel_customer_discount": 0.2,
                  "parcel_fee_per_piece": 1.0},
        )
        assert r.status_code == 400

        r = client.delete("/api/system/pricing-terms?effective_date=2026-08-01")
        assert r.status_code == 200
        assert len(r.get_json()["revisions"]) == 1

        r = client.delete("/api/system/pricing-terms?effective_date=2026-08-01")
        assert r.status_code == 404


def test_profit_endpoints_default_from_stored_terms(monkeypatch):
    """Knobs absent from the request resolve from the stored revision as-of end_date."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "terms_resolve.db"
        client = _client(monkeypatch, p)

        r = client.put(
            "/api/system/pricing-terms",
            json={
                "effective_date": "2026-05-01",
                "flats_customer_discount": 0.15,
                "flats_efd_discount": 0.28,
                "parcel_customer_discount": 0.35,
                "parcel_fee_per_piece": 2.00,
            },
        )
        assert r.status_code == 200

        # Parcels: no knobs -> stored terms for the in-effect revision.
        r = client.get("/api/profit/parcels?start_date=2026-05-01&end_date=2026-05-31")
        assert r.status_code == 200
        meta = r.get_json()["meta"]
        assert meta["parcel_fee"] == 2.00
        assert meta["efd_parcel_fee"] == 2.00
        assert meta["terms_effective_date"] == "2026-05-01"
        assert meta["terms_source"]["parcel_fee"] == "stored"

        # End date before the revision -> baseline terms.
        r = client.get("/api/profit/parcels?start_date=2026-04-01&end_date=2026-04-30")
        assert r.status_code == 200
        meta = r.get_json()["meta"]
        assert meta["parcel_fee"] == 1.25
        assert meta["terms_effective_date"] == dbmod.FLAT_RATE_BASELINE_DATE

        # Explicit request knob always wins over stored terms.
        r = client.get(
            "/api/profit/parcels?start_date=2026-05-01&end_date=2026-05-31&parcel_fee=0.75"
        )
        assert r.status_code == 200
        meta = r.get_json()["meta"]
        assert meta["parcel_fee"] == 0.75
        assert meta["terms_source"]["parcel_fee"] == "request"

        # Flats meta reflects stored discount (404 empty is fine - meta still present).
        r = client.get("/api/profit/flats?start_date=2026-05-01&end_date=2026-05-31")
        assert r.status_code == 404
        meta = r.get_json()["meta"]
        assert meta["discount"] == 0.15
        assert meta["sell_to_rate"] == round(1.63 - 0.15, 4)
        assert meta["terms_source"]["discount"] == "stored"
