"""Tests for importer helpers."""

from pathlib import Path

import pytest
from openpyxl import Workbook

import importer
import watcher


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0986", 986),
        ("0001", 1),
        ("88", 88),
        ("0", 0),
        ("", 0),
        (123, 123),
    ],
)
def test_strip_zeros(raw, expected):
    assert importer.strip_zeros(raw) == expected


def test_strip_zeros_invalid():
    assert importer.strip_zeros("abc") is None
    assert importer.strip_zeros(None) is None


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("BM_3_20_26_report.csv", "2026-03-20"),
        ("BM_11_5_26.xls", "2026-11-05"),
        ("BM 3.19.26.xls", "2026-03-19"),
        ("BM 3.19.26.csv", "2026-03-19"),
        ("BM_3_20_2026_report.csv", "2026-03-20"),
    ],
)
def test_parse_bm_date(filename, expected):
    assert importer.parse_bm_date(filename) == expected


def test_parse_bm_date_bad():
    with pytest.raises(ValueError, match="Cannot parse"):
        importer.parse_bm_date("not_a_bm_file.csv")


def test_parse_bm_raw_csv_skips_class_header_in_column_g(tmp_path):
    """Repeated sheet headers put the word 'Class' in column G; do not use as mail_class."""
    p = tmp_path / "BM_1_2_26.csv"
    lines = [
        "Pitney Bowes,,,,,,,,,,,,,,,,,,",
        "1234,,,,,,,,,,,,,,,,,,",
        ",,,,,,1CA5DFlt,,,,,,,,,,,,,",
        ",,,,,,Class,,,,,,,,,,,,,",
        ",,,,,,,,,,,,2.0,,,1,,1.0,,,,",
    ]
    p.write_text("\n".join(lines), encoding="utf-8-sig")
    rows = importer.parse_bm_raw_csv(str(p))
    assert len(rows) == 1
    assert rows[0]["mail_class"] == "1CA5DFlt"


def test_parse_bm_raw_csv_synthetic(tmp_path):
    """Minimal raw BM-style CSV: headers, account row, class row, one data row."""
    p = tmp_path / "BM_1_2_26.csv"
    lines = [
        "Pitney Bowes,,,,,,,,,,,,,,,,,,",
        "1234,,,,,,,,,,,,,,,,,,",
        ",,,,,,1CA,,,,,,,,,,,,,",
        ",,,,,,,,,,,,3.0,,,10,,25.50,,,,",
    ]
    p.write_text("\n".join(lines), encoding="utf-8-sig")
    rows = importer.parse_bm_raw_csv(str(p))
    assert len(rows) == 1
    assert rows[0] == {
        "account_code": "1234",
        "mail_class": "1CA",
        "weight_oz": 3.0,
        "pieces": 10,
        "total_cost": 25.5,
    }


def test_parse_bm_raw_csv_fixture_bm_3_19_26():
    root = Path(__file__).resolve().parent.parent
    fixture = root / "BM 3.19.26.csv"
    report = root / "BM_3_19_26_report.csv"
    if not fixture.is_file():
        pytest.skip("BM 3.19.26.csv not in project root")
    rows = importer.parse_bm_raw_csv(str(fixture))
    assert len(rows) == 3312
    assert rows[0]["account_code"] == "8393"
    assert rows[0]["mail_class"] == "NOCLASS"
    assert rows[0]["weight_oz"] == 0.0
    # Comma-formatted weight / pieces from raw export
    oc = next(r for r in rows if r["mail_class"] == "OtherCls" and r["weight_oz"] == 1120.0)
    assert oc["pieces"] == 0
    # First non-zero cost in file (1ClFlat 2 oz)
    cl = next(r for r in rows if r["mail_class"] == "1ClFlat" and r["weight_oz"] == 2.0)
    assert cl["pieces"] == 1
    assert cl["total_cost"] == 1.9
    if report.is_file():
        import csv

        def norm_cost(x: float) -> str:
            return f"{float(x):.3f}"

        with open(report, encoding="utf-8", newline="") as f:
            expected = [
                (
                    row["Account Code"],
                    row["Class"],
                    row["Weight  (oz.)"],
                    row["Pieces"],
                    norm_cost(row["Total Cost"]),
                )
                for row in csv.DictReader(f)
            ]
        got = [
            (
                r["account_code"],
                r["mail_class"],
                str(int(r["weight_oz"]) if r["weight_oz"] == int(r["weight_oz"]) else r["weight_oz"]),
                str(r["pieces"]),
                norm_cost(r["total_cost"]),
            )
            for r in rows
        ]
        assert got == expected


def test_import_bm_csv_diverts_othercls_1120_to_presort_rejects(monkeypatch, tmp_path):
    import db as dbmod

    # Init an empty DB for import to write into.
    db_path = tmp_path / "imp.db"
    monkeypatch.setattr(dbmod, "DB_PATH", db_path)
    dbmod.init_db()

    conn = dbmod.get_connection()
    try:
        conn.execute(
            "INSERT INTO customers (customer_number, customer_name) VALUES (1234, 'Co')"
        )
        conn.commit()
    finally:
        conn.close()

    # Minimal BM report CSV with a single uplift row.
    p = tmp_path / "BM_4_23_26_report.csv"
    p.write_text(
        "\n".join(
            [
                "Account Code,Class,Weight  (oz.),Pieces,Total Cost",
                "1234,OtherCls,1120.0,7,0.66",
            ]
        ),
        encoding="utf-8",
    )

    out = importer.import_bm_csv(str(p), db_path, file_date_override="2026-04-23")
    assert out["rows_imported"] == 0
    assert out["diverted_presort_reject_pieces"] == 7

    conn = dbmod.get_connection()
    try:
        n_postage = conn.execute("SELECT COUNT(*) FROM postage_data").fetchone()[0]
        assert int(n_postage) == 0
        n_rej = conn.execute(
            "SELECT COALESCE(SUM(reject_count),0) FROM postage_presort_rejects"
        ).fetchone()[0]
        assert int(n_rej) == 7
    finally:
        conn.close()


def test_parse_bm_raw_csv_commas_in_numbers(tmp_path):
    """Thousands separators in weight and pieces must parse."""
    p = tmp_path / "BM_1_2_26.csv"
    lines = [
        "Pitney Bowes,,,,,,,,,,,,,,,,,,",
        "1234,,,,,,,,,,,,,,,,,,",
        ",,,,,,ZClass,,,,,,,,,,,,,",
        ',,,,,,,,,,,,"1,120.5",,,"5,786",,12.340,,,,',
    ]
    p.write_text("\n".join(lines), encoding="utf-8-sig")
    rows = importer.parse_bm_raw_csv(str(p))
    assert len(rows) == 1
    assert rows[0]["weight_oz"] == 1120.5
    assert rows[0]["pieces"] == 5786
    assert rows[0]["total_cost"] == 12.34


def test_watcher_bm_report_vs_raw_detection():
    assert watcher._is_bm_report_csv("BM 3.19.26_report.csv")
    assert watcher._is_bm_report_csv("BM_3_19_26_report.csv")
    assert not watcher._is_bm_raw_export("BM 3.19.26_report.csv")
    assert watcher._is_bm_raw_export("BM 3.19.26.csv")
    assert watcher._is_bm_raw_export(
        "DM Weight Break by Account-Carrier-Class 04242026 - 053056.7513322.xlsx"
    )


def test_write_report_csv_renames_csv_source(tmp_path):
    src = tmp_path / "BM_1_2_26.csv"
    src.write_text("", encoding="utf-8")
    out = importer.write_report_csv(
        [
            {
                "account_code": "1",
                "mail_class": "X",
                "weight_oz": 1.0,
                "pieces": 1,
                "total_cost": 0.1,
            }
        ],
        str(src),
        str(tmp_path),
    )
    assert Path(out).name == "BM_1_2_26_report.csv"


def test_read_bm_report_date_from_xlsx_happy_path(tmp_path):
    p = tmp_path / "DM Weight Break by Account-Carrier-Class 04242026.xlsx"
    wb = Workbook()
    ws = wb.active
    ws["P3"].value = "04/24/2026"
    ws["S3"].value = "04/24/2026"
    wb.save(p)

    assert importer.read_bm_report_date_from_xlsx(str(p)) == "2026-04-24"


def test_read_bm_report_date_from_xlsx_mismatch_raises(tmp_path):
    p = tmp_path / "DM Weight Break by Account-Carrier-Class 04242026.xlsx"
    wb = Workbook()
    ws = wb.active
    ws["P3"].value = "04/24/2026"
    ws["S3"].value = "04/25/2026"
    wb.save(p)

    with pytest.raises(ValueError, match=r"BM report date mismatch"):
        importer.read_bm_report_date_from_xlsx(str(p))
