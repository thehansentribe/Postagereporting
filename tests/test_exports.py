"""Tests for export helpers (parcel roll-up, etc.)."""

from __future__ import annotations

import exports
import exports_consolidated_volumes


def _parcel_row(
    *,
    date: str = "2026-04-03",
    parent_name: str = "P",
    parent_number: int = 1,
    child_name: str = "C",
    child_number: int = 10,
    lb_1: int = 0,
    lb_2: int = 0,
    total_qty: int = 0,
    total_billed: float = 0.0,
    total_retail: float = 0.0,
) -> dict:
    return {
        "date": date,
        "parent_name": parent_name,
        "parent_number": parent_number,
        "child_name": child_name,
        "child_number": child_number,
        "lb_1": lb_1,
        "lb_2": lb_2,
        "lb_3": 0,
        "lb_4": 0,
        "lb_5": 0,
        "lb_6": 0,
        "lb_7": 0,
        "lb_8": 0,
        "lb_9": 0,
        "lb_10": 0,
        "lb_10plus": 0,
        "total_qty": total_qty,
        "total_billed": total_billed,
        "total_retail": total_retail,
    }


def test_aggregate_parcel_count_rows_sums_costs_across_split_rows() -> None:
    """API returns one row per mail class × zone; roll-up must sum billed and retail."""
    rows = [
        _parcel_row(lb_1=1, total_qty=1, total_billed=5.0, total_retail=10.0),
        _parcel_row(lb_2=1, total_qty=1, total_billed=3.0, total_retail=7.0),
    ]
    agg = exports.aggregate_parcel_count_rows(rows)
    assert len(agg) == 1
    a = agg[0]
    assert a["lb_1"] == 1
    assert a["lb_2"] == 1
    assert a["total_qty"] == 2
    assert abs(a["total_billed"] - 8.0) < 1e-9
    assert abs(a["total_retail"] - 17.0) < 1e-9


def test_parcel_counts_download_name() -> None:
    assert "Parcel_Counts" in exports.parcel_counts_download_name("a", "b", None, None)
    assert exports.parcel_counts_download_name("2026-01-01", "2026-01-31", 12, None).endswith(
        ".xlsx"
    )


def test_consolidated_volumes_download_name() -> None:
    n = exports_consolidated_volumes.consolidated_volumes_download_name("2026-01-01", "2026-01-31")
    assert n.startswith("volumes_flats_parcels_")
    assert n.endswith(".xlsx")
