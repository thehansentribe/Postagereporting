"""SQLite database: init, connection helpers, and reporting queries."""

from __future__ import annotations

import csv
import math
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "postage.db"
PARCEL_SUMMARY_RATES_CSV = ROOT / "parcel summary.csv"
HEAVY_PARCEL_RATES_CSV = ROOT / "heavy_parcel_rates.csv"

# Default 2026 flat retail (single piece) rates by weight tier (oz).
DEFAULT_FLATS_RETAIL_RATES: list[dict[str, float]] = [
    {"weight_not_over_oz": 1.0, "rate_retail": 1.63},
    {"weight_not_over_oz": 2.0, "rate_retail": 1.90},
    {"weight_not_over_oz": 3.0, "rate_retail": 2.17},
    {"weight_not_over_oz": 4.0, "rate_retail": 2.44},
    {"weight_not_over_oz": 5.0, "rate_retail": 2.72},
    {"weight_not_over_oz": 6.0, "rate_retail": 3.00},
    {"weight_not_over_oz": 7.0, "rate_retail": 3.28},
    {"weight_not_over_oz": 8.0, "rate_retail": 3.56},
    {"weight_not_over_oz": 9.0, "rate_retail": 3.84},
    {"weight_not_over_oz": 10.0, "rate_retail": 4.14},
    {"weight_not_over_oz": 11.0, "rate_retail": 4.44},
    {"weight_not_over_oz": 12.0, "rate_retail": 4.74},
    {"weight_not_over_oz": 13.0, "rate_retail": 5.04},
]

# Flats first-class mail classes (invoice + dashboard). Any postage_data row with weight > 13 oz
# (all mail classes, including BM imports) is excluded from query_postage totals and merged into
# parcel reporting at DEFAULT_PARCEL_ZONE_FOR_POSTAGE_OVER_13_OZ.
POSTAGE_INVOICE_FLAT_MAIL_CLASSES: tuple[str, ...] = (
    "1CA5DFlt",
    "1ClFlat",
    "1CSPiece",
    "1CNAPres",
    "1CAAADCL",
    "1CMAADCL",
    "1stClNMLtr",
)

POSTAGE_INVOICE_FLAT_MAIL_SQL_IN = (
    "(" + ",".join(f"'{c}'" for c in POSTAGE_INVOICE_FLAT_MAIL_CLASSES) + ")"
)

# Only this flat class uses EFD (discounted) pricing on the postage invoice; all other
# POSTAGE_INVOICE_FLAT_MAIL_CLASSES are billed at full retail in the IMB-reject column (1–13 oz).
INVOICE_EFD_FLAT_MAIL_CLASS = "1CA5DFlt"

# KC presort spec: do not shift these "non-class" metered items into parcels, even if >13 oz.
KC_PRESORT_METERED_MAIL_CLASSES_UPPER: tuple[str, ...] = ("NOCLASS", "OTHERCLS")

# Postage-derived parcel pricing uses this USPS zone when postage_data has no zone (BM / Pitney).
DEFAULT_PARCEL_ZONE_FOR_POSTAGE_OVER_13_OZ = 2
# Back-compat alias (older name).
DEFAULT_PARCEL_ZONE_FOR_POSTAGE_FLAT_OVER_13 = DEFAULT_PARCEL_ZONE_FOR_POSTAGE_OVER_13_OZ

# mtime -> rates map; invalidated when CSV changes on disk
_parcel_summary_rates_cache: tuple[float, dict[tuple[int, int], tuple[float, float]]] | None = None
_heavy_parcel_rates_cache: tuple[float, dict[tuple[int, int], tuple[float, float]]] | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_number  INTEGER PRIMARY KEY,
    customer_name    TEXT    NOT NULL,
    parent_number    INTEGER,
    parent_name      TEXT,
    FOREIGN KEY (parent_number) REFERENCES customers(customer_number)
);
CREATE INDEX IF NOT EXISTS idx_customers_parent ON customers(parent_number);

CREATE TABLE IF NOT EXISTS postage_imports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name    TEXT    NOT NULL UNIQUE,
    file_date    TEXT    NOT NULL,
    imported_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    row_count    INTEGER
);

CREATE TABLE IF NOT EXISTS postage_data (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id          INTEGER NOT NULL,
    file_date          TEXT    NOT NULL,
    account_code       INTEGER NOT NULL,
    mail_class         TEXT    NOT NULL,
    weight_oz          REAL    NOT NULL,
    pieces             INTEGER NOT NULL,
    total_cost         REAL    NOT NULL,
    unmatched_account  INTEGER DEFAULT 0,
    FOREIGN KEY (import_id) REFERENCES postage_imports(id) ON DELETE CASCADE,
    UNIQUE (import_id, account_code, mail_class, weight_oz)
);
CREATE INDEX IF NOT EXISTS idx_postage_date    ON postage_data(file_date);
CREATE INDEX IF NOT EXISTS idx_postage_account ON postage_data(account_code);
CREATE INDEX IF NOT EXISTS idx_postage_import  ON postage_data(import_id);
CREATE INDEX IF NOT EXISTS idx_postage_class   ON postage_data(mail_class);

-- Postage-side presort rejects (e.g. BM/DM uplift artifacts).
CREATE TABLE IF NOT EXISTS postage_presort_rejects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_date    TEXT    NOT NULL,
    account_code INTEGER NOT NULL,
    reject_count INTEGER NOT NULL,
    source       TEXT    NOT NULL,
    import_id    INTEGER,
    FOREIGN KEY (import_id) REFERENCES postage_imports(id) ON DELETE SET NULL,
    UNIQUE (file_date, account_code, source)
);
CREATE INDEX IF NOT EXISTS idx_postage_presort_rejects_date ON postage_presort_rejects(file_date);
CREATE INDEX IF NOT EXISTS idx_postage_presort_rejects_account ON postage_presort_rejects(account_code);

CREATE TABLE IF NOT EXISTS postage_edits (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    file_date         TEXT    NOT NULL,
    from_account_code INTEGER NOT NULL,
    to_account_code   INTEGER NOT NULL,
    mail_class        TEXT    NOT NULL,
    reason            TEXT,
    merged_rows       INTEGER DEFAULT 0,
    updated_rows      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_postage_edits_date ON postage_edits(file_date);
CREATE INDEX IF NOT EXISTS idx_postage_edits_from ON postage_edits(from_account_code);
CREATE INDEX IF NOT EXISTS idx_postage_edits_to   ON postage_edits(to_account_code);

CREATE TABLE IF NOT EXISTS postage_edit_lines (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    edit_id           INTEGER NOT NULL,
    source_postage_id INTEGER NOT NULL,
    dest_postage_id   INTEGER,
    action            TEXT    NOT NULL, -- updated | merged
    weight_oz         REAL    NOT NULL,
    old_account_code  INTEGER NOT NULL,
    new_account_code  INTEGER NOT NULL,
    old_pieces        INTEGER NOT NULL,
    new_pieces        INTEGER NOT NULL,
    old_total_cost    REAL    NOT NULL,
    new_total_cost    REAL    NOT NULL,
    FOREIGN KEY (edit_id) REFERENCES postage_edits(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_postage_edit_lines_edit ON postage_edit_lines(edit_id);

CREATE TABLE IF NOT EXISTS flat_rate_costs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    weight_not_over_oz   REAL    NOT NULL UNIQUE,
    rate_5digit          REAL,
    rate_3digit          REAL,
    rate_aadc            REAL,
    rate_mixed_adc       REAL,
    rate_machinable_pres REAL,
    rate_retail          REAL,
    effective_date       TEXT,
    notes                TEXT
);

CREATE TABLE IF NOT EXISTS parcel_costs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    service_type         TEXT    NOT NULL,
    weight_min_oz        REAL    NOT NULL,
    weight_max_oz        REAL    NOT NULL,
    zone                 TEXT,
    rate                 REAL,
    effective_date       TEXT,
    notes                TEXT,
    UNIQUE (service_type, weight_min_oz, weight_max_oz, zone)
);
CREATE INDEX IF NOT EXISTS idx_parcel_costs_lookup
    ON parcel_costs(service_type, weight_min_oz, weight_max_oz, zone);

CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY NOT NULL,
    value_real  REAL
);

CREATE TABLE IF NOT EXISTS billing_imports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    billing_id  TEXT    NOT NULL UNIQUE,
    file_name   TEXT    NOT NULL,
    imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    row_count   INTEGER
);

CREATE TABLE IF NOT EXISTS billing_records (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    billing_import_id           INTEGER NOT NULL,
    custom_account_code         INTEGER,
    account_name                TEXT,
    piece_id                    TEXT,
    machine_serial              TEXT,
    time_stamp                  TEXT,
    weight_oz                   REAL,
    handling_type               TEXT,
    usps_mail_class             TEXT,
    usps_mail_prep_type         TEXT,
    routing_category            TEXT,
    routing_string              TEXT,
    bundle_qualification        TEXT,
    bundle_zip                  TEXT,
    account_id                  TEXT,
    customer_barcode_symbology  TEXT,
    customer_barcode            TEXT,
    department_id               TEXT,
    department_name             TEXT,
    manifest_id                 TEXT,
    piece_postage               REAL,
    lbs_postage                 REAL,
    final_postage               REAL,
    fully_paid_postage          REAL,
    billing_amount              REAL,
    imb_tracking_code           TEXT,
    sack_level                  TEXT,
    sack_zip                    TEXT,
    destination_entry_level     TEXT,
    zone                        TEXT,
    irregular                   TEXT,
    custom1                     TEXT,
    custom2                     TEXT,
    driver_route                TEXT,
    adc                         TEXT,
    schemed_3d                  TEXT,
    schemed_5d                  TEXT,
    manifest_date               TEXT,
    length_in                   REAL,
    width_in                    REAL,
    height_in                   REAL,
    girth_in                    REAL,
    is_flat_rate_conversion     TEXT,
    nonrectangular              TEXT,
    sub_type                    TEXT,
    ocr                         TEXT,
    bmc                         TEXT,
    asf                         TEXT,
    scf                         TEXT,
    master_mail_class           TEXT,
    ezconfirm_pic               TEXT,
    ezconfirm_processing_type   TEXT,
    ezconfirm_name              TEXT,
    ezconfirm_company           TEXT,
    ezconfirm_address1          TEXT,
    ezconfirm_address2          TEXT,
    ezconfirm_city              TEXT,
    ezconfirm_state             TEXT,
    ezconfirm_zip               TEXT,
    ezconfirm_zip4              TEXT,
    ezconfirm_record_case_number TEXT,
    ezconfirm_is_uploaded       TEXT,
    wabcr_symbology1            TEXT,
    wabcr_data1                 TEXT,
    wabcr_symbology2            TEXT,
    wabcr_data2                 TEXT,
    wabcr_symbology3            TEXT,
    wabcr_data3                 TEXT,
    wabcr_symbology4            TEXT,
    wabcr_data4                 TEXT,
    wabcr_symbology5            TEXT,
    wabcr_data5                 TEXT,
    job_name                    TEXT,
    billing_id_ref              TEXT,
    permit_origin               TEXT,
    permit_number               TEXT,
    permit_name                 TEXT,
    ezconfirm_special_services  TEXT,
    mail_piece_tag_data         TEXT,
    is_open_and_distribute      TEXT,
    payment_method              TEXT,
    premeter_qual_level         TEXT,
    key_line                    TEXT,
    impb                        TEXT,
    efn                         TEXT,
    surcharge_postage           REAL,
    fss                         TEXT,
    tub_number                  TEXT,
    postal_discounts            REAL,
    hr_address                  TEXT,
    hr_city                     TEXT,
    hr_state                    TEXT,
    hr_zip                      TEXT,
    label_list_installer_version TEXT,
    is_move                     TEXT,
    is_catalog                  TEXT,
    unmatched_account           INTEGER DEFAULT 0,
    FOREIGN KEY (billing_import_id) REFERENCES billing_imports(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_billing_import    ON billing_records(billing_import_id);
CREATE INDEX IF NOT EXISTS idx_billing_account   ON billing_records(custom_account_code);
CREATE INDEX IF NOT EXISTS idx_billing_timestamp ON billing_records(time_stamp);
CREATE INDEX IF NOT EXISTS idx_billing_zone      ON billing_records(zone);
CREATE INDEX IF NOT EXISTS idx_billing_class     ON billing_records(usps_mail_class);

CREATE TABLE IF NOT EXISTS ws3_netsort_customers (
    customer_code TEXT PRIMARY KEY,
    customer_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ws3_mail_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    mail_date    TEXT NOT NULL,
    mail_id      TEXT NOT NULL DEFAULT '',
    run_datetime TEXT,
    source_file_name TEXT,
    UNIQUE (mail_date, mail_id)
);

CREATE TABLE IF NOT EXISTS ws3_profiles (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name            TEXT NOT NULL UNIQUE,
    parent_customer_number  INTEGER REFERENCES customers (customer_number),
    reject_fee              REAL
);

CREATE TABLE IF NOT EXISTS ws3_mail_detail (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES ws3_mail_runs (run_id) ON DELETE CASCADE,
    profile_id       INTEGER NOT NULL REFERENCES ws3_profiles (id),
    customer_code    TEXT NOT NULL REFERENCES ws3_netsort_customers (customer_code),
    rate_type        TEXT NOT NULL,
    postage_claimed  REAL,
    postage_applied  REAL,
    num_pieces       INTEGER,
    pcs_accepted     INTEGER,
    pcs_rejected     INTEGER,
    cost_per_piece   REAL,
    usps_cost_per_piece REAL
);
CREATE INDEX IF NOT EXISTS idx_ws3_detail_run ON ws3_mail_detail (run_id);
CREATE INDEX IF NOT EXISTS idx_ws3_detail_profile ON ws3_mail_detail (profile_id);

CREATE TABLE IF NOT EXISTS ws3_imports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name    TEXT NOT NULL UNIQUE,
    mail_date    TEXT NOT NULL,
    run_id       INTEGER NOT NULL REFERENCES ws3_mail_runs (run_id) ON DELETE CASCADE,
    row_count    INTEGER,
    imported_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ws3_parent_daily_rejects (
    mail_date                TEXT    NOT NULL,
    parent_customer_number   INTEGER NOT NULL REFERENCES customers (customer_number),
    reject_count             INTEGER NOT NULL,
    updated_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (mail_date, parent_customer_number)
);
CREATE INDEX IF NOT EXISTS idx_ws3_rejects_date ON ws3_parent_daily_rejects (mail_date);

-- Retail rate reference tables (full replace imports via watcher + importer).
CREATE TABLE IF NOT EXISTS priority_mail_retail (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    effective_date   TEXT,
    source_file_name TEXT,
    imported_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    row_type         TEXT    NOT NULL, -- matrix | flat_rate_item | fee | note
    label            TEXT,
    zone             INTEGER,
    weight_unit      TEXT, -- lb
    weight_max       REAL,
    price            REAL,
    sort_group       INTEGER,
    sort_order       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_priority_mail_retail_lookup
    ON priority_mail_retail(row_type, zone, weight_unit, weight_max);
CREATE INDEX IF NOT EXISTS idx_priority_mail_retail_effective_date
    ON priority_mail_retail(effective_date);

CREATE TABLE IF NOT EXISTS ground_advantage_retail (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    effective_date   TEXT,
    source_file_name TEXT,
    imported_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    row_type         TEXT    NOT NULL, -- matrix | fee | note
    label            TEXT,
    zone             INTEGER,
    weight_unit      TEXT, -- oz | lb
    weight_max       REAL,
    price            REAL,
    sort_group       INTEGER,
    sort_order       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ground_advantage_retail_lookup
    ON ground_advantage_retail(row_type, zone, weight_unit, weight_max);
CREATE INDEX IF NOT EXISTS idx_ground_advantage_retail_effective_date
    ON ground_advantage_retail(effective_date);
"""


# Sentinel mail_class for WS3 presort reject totals on the postage dashboard.
WS3_REJECT_MAIL_CLASS = "Presort rejects"

# Invoice / billing: per-piece charge for WS3 presort rejects (editable on System page).
PRESORT_REJECT_UNIT_COST_KEY = "presort_reject_unit_cost"
DEFAULT_PRESORT_REJECT_UNIT_COST = 0.66


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_app_settings(conn)
    return conn


def _migrate_ws3_profiles_reject_fee(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ALTER TABLE ws3_profiles ADD COLUMN reject_fee REAL")
    except sqlite3.OperationalError:
        pass


def _migrate_ws3_mail_detail_usps_cost_per_piece(conn: sqlite3.Connection) -> None:
    """
    One-time migration for WS3 profit report.

    Adds `ws3_mail_detail.usps_cost_per_piece` and backfills it from:
      postage_claimed / num_pieces
    """
    try:
        conn.execute(
            "ALTER TABLE ws3_mail_detail ADD COLUMN usps_cost_per_piece REAL"
        )
    except sqlite3.OperationalError:
        # Column already exists (or table missing during early init)
        return
    conn.execute(
        """
        UPDATE ws3_mail_detail
        SET usps_cost_per_piece = ROUND(CAST(postage_claimed AS REAL) / num_pieces, 4)
        WHERE num_pieces IS NOT NULL
          AND num_pieces > 0
          AND postage_claimed IS NOT NULL
        """
    )


def _ensure_app_settings(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key         TEXT PRIMARY KEY NOT NULL,
            value_real  REAL
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO app_settings (key, value_real)
        VALUES (?, ?)
        """,
        (PRESORT_REJECT_UNIT_COST_KEY, DEFAULT_PRESORT_REJECT_UNIT_COST),
    )


def clamp_negative_ws3_reject_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """
    Data repair: WS3 reject counts must never be negative.

    Older imports could produce negative values when pcs_accepted > num_pieces.
    """
    cur = conn.cursor()
    cur.execute("UPDATE ws3_mail_detail SET pcs_rejected = 0 WHERE pcs_rejected < 0")
    detail_updated = int(cur.rowcount or 0)
    cur.execute(
        "UPDATE ws3_parent_daily_rejects SET reject_count = 0 WHERE reject_count < 0"
    )
    parent_updated = int(cur.rowcount or 0)
    return {"ws3_mail_detail": detail_updated, "ws3_parent_daily_rejects": parent_updated}


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate_ws3_profiles_reject_fee(conn)
    _migrate_ws3_mail_detail_usps_cost_per_piece(conn)
    _ensure_app_settings(conn)
    clamp_negative_ws3_reject_counts(conn)
    conn.commit()
    conn.close()


def list_flat_retail_rates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT weight_not_over_oz, rate_retail
        FROM flat_rate_costs
        ORDER BY weight_not_over_oz ASC
        """
    )
    return [
        {
            "weight_not_over_oz": float(r["weight_not_over_oz"]),
            "rate_retail": float(r["rate_retail"]) if r["rate_retail"] is not None else None,
        }
        for r in cur.fetchall()
    ]


def get_presort_reject_unit_cost(conn: sqlite3.Connection) -> float:
    """Per-piece presort reject charge used on the postage invoice (default $0.66)."""
    row = conn.execute(
        "SELECT value_real FROM app_settings WHERE key = ?",
        (PRESORT_REJECT_UNIT_COST_KEY,),
    ).fetchone()
    if row is None or row["value_real"] is None:
        return float(DEFAULT_PRESORT_REJECT_UNIT_COST)
    return float(row["value_real"])


def set_presort_reject_unit_cost(conn: sqlite3.Connection, cost_per_piece: float) -> None:
    if cost_per_piece < 0:
        raise ValueError("presort reject unit cost must be non-negative")
    conn.execute(
        """
        INSERT INTO app_settings (key, value_real) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value_real = excluded.value_real
        """,
        (PRESORT_REJECT_UNIT_COST_KEY, float(cost_per_piece)),
    )


def query_ws3_presort_reject_count_for_invoice(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    parent_number: int,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
) -> int:
    """
    Total WS3 presort reject pieces in range for the same scope as the postage invoice
    (parent / optional child, show_parents / show_main).
    """
    par = "par"
    conditions: list[str] = ["r.mail_date BETWEEN ? AND ?"]
    params: list[Any] = [start_date, end_date]

    conditions.append(f"({par}.parent_number = ? OR {par}.customer_number = ?)")
    params.extend([parent_number, parent_number])

    if customer_number is not None:
        ep = effective_parent_account_for_ws3(conn, customer_number)
        conditions.append("r.parent_customer_number = ?")
        params.append(ep)

    if not show_parents:
        conditions.append(
            f"""{par}.customer_number NOT IN (
            SELECT DISTINCT parent_number FROM customers WHERE parent_number IS NOT NULL
        )"""
        )
    if not show_main:
        conditions.append(
            f"NOT ({par}.parent_number IS NULL AND {par}.parent_name IS NULL)"
        )

    where_sql = " AND ".join(conditions)
    sql = f"""
        SELECT COALESCE(SUM(r.reject_count), 0) AS cnt
        FROM ws3_parent_daily_rejects r
        JOIN customers {par} ON {par}.customer_number = r.parent_customer_number
        WHERE {where_sql}
    """
    row = conn.execute(sql, params).fetchone()
    return int(row["cnt"] or 0)


def record_postage_presort_rejects(
    conn: sqlite3.Connection,
    *,
    file_date: str,
    account_code: int,
    reject_count: int,
    source: str,
    import_id: int | None = None,
) -> None:
    """Upsert and accumulate postage-derived presort reject counts."""
    rc = int(reject_count or 0)
    if rc <= 0:
        return
    conn.execute(
        """
        INSERT INTO postage_presort_rejects (file_date, account_code, reject_count, source, import_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(file_date, account_code, source) DO UPDATE SET
            reject_count = postage_presort_rejects.reject_count + excluded.reject_count,
            import_id = COALESCE(postage_presort_rejects.import_id, excluded.import_id)
        """,
        (str(file_date), int(account_code), rc, str(source), int(import_id) if import_id is not None else None),
    )


def query_postage_presort_reject_count_for_invoice(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    parent_number: int,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
) -> int:
    """
    Total postage-derived presort rejects in range for the same scope as the postage invoice
    (parent / optional child, show_parents / show_main).

    These rejects are stored at the (file_date, account_code) level and rolled up to the
    effective parent account (COALESCE(parent_number, customer_number)).
    """
    par = "par"
    conditions: list[str] = ["r.file_date BETWEEN ? AND ?"]
    params: list[Any] = [start_date, end_date]

    conditions.append(f"({par}.parent_number = ? OR {par}.customer_number = ?)")
    params.extend([parent_number, parent_number])

    if customer_number is not None:
        ep = effective_parent_account_for_ws3(conn, int(customer_number))
        conditions.append("ep.customer_number = ?")
        params.append(ep)

    if not show_parents:
        conditions.append(
            f"""{par}.customer_number NOT IN (
            SELECT DISTINCT parent_number FROM customers WHERE parent_number IS NOT NULL
        )"""
        )
    if not show_main:
        conditions.append(f"NOT ({par}.parent_number IS NULL AND {par}.parent_name IS NULL)")

    where_sql = " AND ".join(conditions)
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(r.reject_count), 0) AS cnt
        FROM postage_presort_rejects r
        JOIN customers c ON c.customer_number = r.account_code
        JOIN customers ep
          ON ep.customer_number = COALESCE(c.parent_number, c.customer_number)
        JOIN customers {par}
          ON {par}.customer_number = ep.customer_number
        WHERE {where_sql}
        """,
        params,
    ).fetchone()
    return int((row["cnt"] if row else 0) or 0)


def query_total_presort_reject_count_for_invoice(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    parent_number: int,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
) -> int:
    """Combined presort reject pieces (WS3 + postage-derived)."""
    ws3 = query_ws3_presort_reject_count_for_invoice(
        conn,
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
    )
    post = query_postage_presort_reject_count_for_invoice(
        conn,
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
    )
    return int(ws3) + int(post)


def seed_flat_retail_rates_if_empty(
    conn: sqlite3.Connection, rows: list[dict[str, float]] | None = None
) -> dict[str, Any]:
    """Insert default flat retail tiers if `flat_rate_costs` is empty."""
    if rows is None:
        rows = DEFAULT_FLATS_RETAIL_RATES
    exists = conn.execute("SELECT 1 FROM flat_rate_costs LIMIT 1").fetchone()
    if exists:
        return {"seeded": False, "rows_inserted": 0}

    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO flat_rate_costs (weight_not_over_oz, rate_retail)
        VALUES (:weight_not_over_oz, :rate_retail)
        """,
        rows,
    )
    return {"seeded": True, "rows_inserted": cur.rowcount}


def upsert_flat_retail_rates(
    conn: sqlite3.Connection, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """
    Upsert retail rates by `weight_not_over_oz` without touching other rate columns.
    """
    cleaned: list[dict[str, float | None]] = []
    for r in rows:
        w = r.get("weight_not_over_oz")
        if w is None:
            continue
        try:
            wv = float(w)
        except (TypeError, ValueError):
            continue
        rv_raw = r.get("rate_retail")
        if rv_raw is None or rv_raw == "":
            rv = None
        else:
            try:
                rv = float(rv_raw)
            except (TypeError, ValueError):
                continue
        cleaned.append({"weight_not_over_oz": wv, "rate_retail": rv})

    if not cleaned:
        return {"rows_upserted": 0}

    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO flat_rate_costs (weight_not_over_oz, rate_retail)
        VALUES (:weight_not_over_oz, :rate_retail)
        ON CONFLICT(weight_not_over_oz) DO UPDATE SET
            rate_retail = excluded.rate_retail
        """,
        cleaned,
    )
    return {"rows_upserted": len(cleaned)}

def _billing_ts_date_sql(expr: str) -> str:
    """SQLite expression: M/D/YYYY HH:MM -> YYYY-MM-DD for range compare."""
    # Year is after the second '/'; do not use substr(..., -4, 4) (that reads the time suffix).
    return f"""date(
    substr({expr}, instr({expr}, '/') + instr(substr({expr}, instr({expr}, '/')+1), '/') + 1, 4) || '-' ||
    printf('%02d', CAST(substr({expr}, 1, instr({expr},'/')-1) AS INT)) || '-' ||
    printf('%02d', CAST(substr(substr({expr}, instr({expr},'/')+1), 1,
        instr(substr({expr}, instr({expr},'/')+1),'/')-1) AS INT))
)"""


def list_parent_customers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT c.customer_number,
               c.customer_name,
               (SELECT COUNT(*) FROM customers ch WHERE ch.parent_number = c.customer_number) AS child_count
        FROM customers c
        WHERE c.customer_number IN (
            SELECT DISTINCT parent_number FROM customers WHERE parent_number IS NOT NULL
        )
        ORDER BY c.customer_name COLLATE NOCASE
        """
    )
    return [
        {
            "customer_number": r["customer_number"],
            "customer_name": r["customer_name"],
            "child_count": r["child_count"],
        }
        for r in cur.fetchall()
    ]


def list_customers_dropdown(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """All customers with kind for account dropdown: parent | child | standalone."""
    cur = conn.execute(
        "SELECT DISTINCT parent_number FROM customers WHERE parent_number IS NOT NULL"
    )
    parent_ids = {int(r[0]) for r in cur.fetchall() if r[0] is not None}
    cur = conn.execute(
        """
        SELECT customer_number, customer_name, parent_number, parent_name
        FROM customers
        ORDER BY customer_name COLLATE NOCASE
        """
    )
    out: list[dict[str, Any]] = []
    for r in cur.fetchall():
        cn = int(r["customer_number"])
        if cn in parent_ids:
            kind = "parent"
        elif r["parent_number"] is not None:
            kind = "child"
        else:
            kind = "standalone"
        out.append(
            {
                "customer_number": cn,
                "customer_name": r["customer_name"] or "",
                "kind": kind,
            }
        )
    return out


def query_customer_hierarchy(conn: sqlite3.Connection) -> dict[str, Any]:
    """Parents (referenced as parent_number) with children; standalone = no parent."""
    cur = conn.execute(
        """
        SELECT customer_number, customer_name, parent_number, parent_name
        FROM customers
        """
    )
    rows = [dict(r) for r in cur.fetchall()]
    parent_ids = {r["parent_number"] for r in rows if r["parent_number"] is not None}

    children_by_parent: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        pn = r["parent_number"]
        if pn is not None:
            children_by_parent.setdefault(int(pn), []).append(
                {
                    "customer_number": int(r["customer_number"]),
                    "customer_name": r["customer_name"] or "",
                }
            )
    for lst in children_by_parent.values():
        lst.sort(key=lambda x: (x["customer_name"] or "").casefold())

    parents_out: list[dict[str, Any]] = []
    for r in rows:
        cn = r["customer_number"]
        if cn not in parent_ids:
            continue
        cn = int(cn)
        kids = children_by_parent.get(cn, [])
        parents_out.append(
            {
                "customer_number": cn,
                "customer_name": r["customer_name"] or "",
                "child_count": len(kids),
                "children": kids,
            }
        )
    parents_out.sort(key=lambda x: (x["customer_name"] or "").casefold())

    # True standalone: no parent row, and not listed as a parent (avoid duplicate Hallmark-style rows).
    standalone = [
        {"customer_number": int(r["customer_number"]), "customer_name": r["customer_name"] or ""}
        for r in rows
        if r["parent_number"] is None
        and r["parent_name"] is None
        and r["customer_number"] not in parent_ids
    ]
    standalone.sort(key=lambda x: (x["customer_name"] or "").casefold())

    return {"parents": parents_out, "standalone": standalone}


def list_unmatched_accounts_all_time(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """
    List account numbers that appear in imported data but do not exist in `customers`.

    Sources:
    - Postage: `postage_data.account_code`
    - Parcels: `billing_records.custom_account_code` (non-null)

    Returns one row per account_code with basic usage totals so the System UI can prioritize.
    """
    # Postage-side unmatched
    cur = conn.execute(
        """
        SELECT p.account_code AS account_code,
               COALESCE(SUM(p.pieces), 0) AS postage_pieces,
               COALESCE(SUM(p.total_cost), 0) AS postage_cost
        FROM postage_data p
        WHERE NOT EXISTS (
            SELECT 1 FROM customers c WHERE c.customer_number = p.account_code
        )
        GROUP BY p.account_code
        """,
    )
    postage = {
        int(r["account_code"]): {
            "postage_pieces": int(r["postage_pieces"] or 0),
            "postage_cost": round(float(r["postage_cost"] or 0.0), 2),
        }
        for r in cur.fetchall()
        if r["account_code"] is not None
    }

    # Parcel-side unmatched
    cur = conn.execute(
        """
        SELECT br.custom_account_code AS account_code,
               COUNT(br.id) AS parcel_pieces,
               COALESCE(SUM(br.billing_amount), 0) AS parcel_cost
        FROM billing_records br
        WHERE br.custom_account_code IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM customers c WHERE c.customer_number = br.custom_account_code
          )
        GROUP BY br.custom_account_code
        """,
    )
    parcels = {
        int(r["account_code"]): {
            "parcel_pieces": int(r["parcel_pieces"] or 0),
            "parcel_cost": round(float(r["parcel_cost"] or 0.0), 2),
        }
        for r in cur.fetchall()
        if r["account_code"] is not None
    }

    all_codes = sorted(set(postage.keys()) | set(parcels.keys()))
    out: list[dict[str, Any]] = []
    for code in all_codes:
        p = postage.get(code) or {}
        pr = parcels.get(code) or {}
        has_postage = (p.get("postage_pieces") or 0) > 0 or (p.get("postage_cost") or 0.0) > 0.0
        has_parcel = (pr.get("parcel_pieces") or 0) > 0 or (pr.get("parcel_cost") or 0.0) > 0.0
        sources = "both" if (has_postage and has_parcel) else ("postage" if has_postage else "parcels")
        out.append(
            {
                "account_code": int(code),
                "sources": sources,
                "postage_pieces": int(p.get("postage_pieces") or 0),
                "postage_cost": round(float(p.get("postage_cost") or 0.0), 2),
                "parcel_pieces": int(pr.get("parcel_pieces") or 0),
                "parcel_cost": round(float(pr.get("parcel_cost") or 0.0), 2),
            }
        )
    return out


def upsert_customer(
    conn: sqlite3.Connection,
    customer_number: int,
    customer_name: str,
    parent_number: int | None = None,
) -> dict[str, Any]:
    """
    Create or update a customer row.

    - `customer_name` is required (non-empty after trim)
    - `parent_number` is optional; when present it must exist and cannot equal `customer_number`
    - `parent_name` is normalized from the selected parent row
    """
    try:
        cn = int(customer_number)
    except (TypeError, ValueError):
        raise ValueError("customer_number must be an integer")

    name = (customer_name or "").strip()
    if not name:
        raise ValueError("customer_name is required")

    pn: int | None
    if parent_number is None or parent_number == "":
        pn = None
    else:
        try:
            pn = int(parent_number)
        except (TypeError, ValueError):
            raise ValueError("parent_number must be an integer or null")

    if pn is not None and pn == cn:
        raise ValueError("parent_number cannot equal customer_number")

    pname: str | None = None
    if pn is not None:
        prow = conn.execute(
            "SELECT customer_name FROM customers WHERE customer_number = ?",
            (int(pn),),
        ).fetchone()
        if not prow:
            raise ValueError(f"Parent account {pn} does not exist")
        pname = (prow["customer_name"] or "").strip() or f"Account {pn}"

    conn.execute(
        """
        INSERT INTO customers (customer_number, customer_name, parent_number, parent_name)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(customer_number) DO UPDATE SET
            customer_name = excluded.customer_name,
            parent_number = excluded.parent_number,
            parent_name = excluded.parent_name
        """,
        (int(cn), name, pn, pname),
    )
    return {"ok": True, "customer_number": int(cn)}


def postage_scope_where_clause(
    start_date: str,
    end_date: str,
    parent_number: int | None,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
    postage_alias: str = "p",
    customer_alias: str = "c",
) -> tuple[str, list[Any]]:
    """Shared `postage_data` + `customers` filter for dashboard and postage invoice export."""
    pa = postage_alias
    ca = customer_alias
    conditions: list[str] = [f"{pa}.file_date BETWEEN ? AND ?"]
    params: list[Any] = [start_date, end_date]

    if parent_number is not None:
        conditions.append(
            f"({ca}.parent_number = ? OR {ca}.customer_number = ?)"
        )
        params.extend([parent_number, parent_number])
    if customer_number is not None:
        conditions.append(f"{ca}.customer_number = ?")
        params.append(customer_number)

    if not show_parents:
        conditions.append(
            f"""{ca}.customer_number NOT IN (
            SELECT DISTINCT parent_number FROM customers WHERE parent_number IS NOT NULL
        )"""
        )
    if not show_main:
        conditions.append(
            f"NOT ({ca}.parent_number IS NULL AND {ca}.parent_name IS NULL)"
        )

    return " AND ".join(conditions), params


def effective_parent_account_for_ws3(conn: sqlite3.Connection, customer_number: int) -> int:
    """Parent account used for WS3 presort reject rollup when filtering by a child or parent."""
    r = conn.execute(
        "SELECT parent_number, customer_number FROM customers WHERE customer_number = ?",
        (int(customer_number),),
    ).fetchone()
    if not r:
        return int(customer_number)
    if r["parent_number"] is not None:
        return int(r["parent_number"])
    return int(r["customer_number"])


def _ws3_scope_where_clause(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    parent_number: int | None,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
    *,
    run_alias: str = "r",
    profile_alias: str = "p",
    parent_customer_alias: str = "par",
) -> tuple[str, list[Any]]:
    """
    Shared WHERE clause for WS3 exports/reports (no leading WHERE).

    Scope rules match `query_ws3_presort_reject_count_for_invoice`:
    - Date range filters by `ws3_mail_runs.mail_date`
    - Account scope uses `ws3_profiles.parent_customer_number` joined to `customers` as `par`
    - When `customer_number` is provided, we constrain to the effective parent account.
    - `show_parents` / `show_main` filters apply to the `customers` row representing the parent account.
    """
    ra = run_alias
    pa = profile_alias
    par = parent_customer_alias

    conditions: list[str] = [f"{ra}.mail_date BETWEEN ? AND ?"]
    params: list[Any] = [start_date, end_date]

    # Parent scope mirrors postage_scope_where_clause (on the parent account row).
    if parent_number is not None:
        conditions.append(f"({par}.parent_number = ? OR {par}.customer_number = ?)")
        params.extend([parent_number, parent_number])

    # Child scope: constrain to the effective WS3 parent account.
    if customer_number is not None:
        ep = effective_parent_account_for_ws3(conn, int(customer_number))
        conditions.append(f"{pa}.parent_customer_number = ?")
        params.append(ep)

    if not show_parents:
        conditions.append(
            f"""{par}.customer_number NOT IN (
            SELECT DISTINCT parent_number FROM customers WHERE parent_number IS NOT NULL
        )"""
        )
    if not show_main:
        conditions.append(f"NOT ({par}.parent_number IS NULL AND {par}.parent_name IS NULL)")

    return " AND ".join(conditions), params


def query_ws3_flats_profit_detail(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    *,
    parent_number: int | None,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
    sell_to_rate: float,
) -> list[dict[str, Any]]:
    """
    Detail-level WS3 flats profit rows over the date range and account scope.

    Profit is computed at the WS3 sort (rate_type) level:
      profit_per_piece = sell_to_rate - usps_cost_per_piece
      total_profit = profit_per_piece * num_pieces
    """
    where_sql, params = _ws3_scope_where_clause(
        conn,
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
        run_alias="r",
        profile_alias="p",
        parent_customer_alias="par",
    )
    cur = conn.execute(
        f"""
        SELECT
            r.mail_date,
            r.mail_id,
            p.profile_name,
            p.parent_customer_number,
            COALESCE(par.parent_name, par.customer_name) AS parent_customer_name,
            d.customer_code,
            nc.customer_name,
            d.rate_type,
            d.num_pieces,
            d.pcs_rejected,
            d.postage_claimed,
            d.usps_cost_per_piece,
            ? AS sell_to_rate,
            ROUND(? - d.usps_cost_per_piece, 4) AS profit_per_piece,
            ROUND((? - d.usps_cost_per_piece) * d.num_pieces, 2) AS total_profit
        FROM ws3_mail_detail d
        JOIN ws3_mail_runs r ON r.run_id = d.run_id
        JOIN ws3_profiles p ON p.id = d.profile_id
        JOIN customers par ON par.customer_number = p.parent_customer_number
        JOIN ws3_netsort_customers nc ON nc.customer_code = d.customer_code
        WHERE {where_sql}
          AND d.usps_cost_per_piece IS NOT NULL
          AND d.num_pieces IS NOT NULL
          AND d.num_pieces > 0
        ORDER BY r.mail_date, nc.customer_name COLLATE NOCASE, d.customer_code, d.id
        """,
        [float(sell_to_rate), float(sell_to_rate), float(sell_to_rate), *params],
    )
    out: list[dict[str, Any]] = []
    for r in cur.fetchall():
        out.append(
            {
                "mail_date": str(r["mail_date"] or ""),
                "mail_id": str(r["mail_id"] or ""),
                "profile_name": str(r["profile_name"] or ""),
                "parent_customer_number": (
                    int(r["parent_customer_number"])
                    if r["parent_customer_number"] is not None
                    else None
                ),
                "parent_customer_name": str(r["parent_customer_name"] or ""),
                "customer_code": str(r["customer_code"] or ""),
                "customer_name": str(r["customer_name"] or ""),
                "rate_type": str(r["rate_type"] or ""),
                "num_pieces": int(r["num_pieces"] or 0),
                "pcs_rejected": int(r["pcs_rejected"] or 0),
                "postage_claimed": round(float(r["postage_claimed"] or 0.0), 2),
                "usps_cost_per_piece": round(float(r["usps_cost_per_piece"] or 0.0), 4),
                "sell_to_rate": round(float(r["sell_to_rate"] or 0.0), 4),
                "profit_per_piece": round(float(r["profit_per_piece"] or 0.0), 4),
                "total_profit": round(float(r["total_profit"] or 0.0), 2),
            }
        )
    return out


def query_ws3_flats_profit_rate_type_summary(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    *,
    parent_number: int | None,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
    sell_to_rate: float,
) -> list[dict[str, Any]]:
    """
    WS3 flats profit summary grouped by `rate_type` (sort level) over the range.

    Uses weighted average USPS cost per piece (weighted by num_pieces).
    """
    where_sql, params = _ws3_scope_where_clause(
        conn,
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
        run_alias="r",
        profile_alias="p",
        parent_customer_alias="par",
    )
    cur = conn.execute(
        f"""
        SELECT
            d.rate_type,
            SUM(d.num_pieces) AS total_pieces,
            ROUND(SUM(d.usps_cost_per_piece * d.num_pieces) / NULLIF(SUM(d.num_pieces), 0), 4)
              AS avg_usps_cost_per_piece,
            ? AS sell_to_rate,
            ROUND(? - (SUM(d.usps_cost_per_piece * d.num_pieces) / NULLIF(SUM(d.num_pieces), 0)), 4)
              AS avg_profit_per_piece,
            ROUND(SUM((? - d.usps_cost_per_piece) * d.num_pieces), 2) AS total_profit
        FROM ws3_mail_detail d
        JOIN ws3_mail_runs r ON r.run_id = d.run_id
        JOIN ws3_profiles p ON p.id = d.profile_id
        JOIN customers par ON par.customer_number = p.parent_customer_number
        WHERE {where_sql}
          AND d.usps_cost_per_piece IS NOT NULL
          AND d.num_pieces IS NOT NULL
          AND d.num_pieces > 0
        GROUP BY d.rate_type
        ORDER BY total_pieces DESC, d.rate_type
        """,
        [float(sell_to_rate), float(sell_to_rate), float(sell_to_rate), *params],
    )
    out: list[dict[str, Any]] = []
    for r in cur.fetchall():
        out.append(
            {
                "rate_type": str(r["rate_type"] or ""),
                "total_pieces": int(r["total_pieces"] or 0),
                "avg_usps_cost_per_piece": (
                    round(float(r["avg_usps_cost_per_piece"]), 4)
                    if r["avg_usps_cost_per_piece"] is not None
                    else None
                ),
                "sell_to_rate": round(float(r["sell_to_rate"] or 0.0), 4),
                "avg_profit_per_piece": (
                    round(float(r["avg_profit_per_piece"]), 4)
                    if r["avg_profit_per_piece"] is not None
                    else None
                ),
                "total_profit": round(float(r["total_profit"] or 0.0), 2),
            }
        )
    return out


def query_ws3_flats_profit_totals(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    *,
    parent_number: int | None,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
    sell_to_rate: float,
) -> dict[str, Any]:
    """Overall totals for WS3 flats profit over the range/scope."""
    where_sql, params = _ws3_scope_where_clause(
        conn,
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
        run_alias="r",
        profile_alias="p",
        parent_customer_alias="par",
    )
    row = conn.execute(
        f"""
        SELECT
            COUNT(DISTINCT p.parent_customer_number) AS parent_accounts,
            COUNT(DISTINCT r.mail_date) AS run_days,
            COALESCE(SUM(d.num_pieces), 0) AS total_pieces,
            ROUND(SUM((? - d.usps_cost_per_piece) * d.num_pieces), 2) AS total_profit
        FROM ws3_mail_detail d
        JOIN ws3_mail_runs r ON r.run_id = d.run_id
        JOIN ws3_profiles p ON p.id = d.profile_id
        JOIN customers par ON par.customer_number = p.parent_customer_number
        WHERE {where_sql}
          AND d.usps_cost_per_piece IS NOT NULL
          AND d.num_pieces IS NOT NULL
          AND d.num_pieces > 0
        """,
        [float(sell_to_rate), *params],
    ).fetchone()
    if not row:
        return {"parent_accounts": 0, "run_days": 0, "total_pieces": 0, "total_profit": 0.0}
    return {
        "parent_accounts": int(row["parent_accounts"] or 0),
        "run_days": int(row["run_days"] or 0),
        "total_pieces": int(row["total_pieces"] or 0),
        "total_profit": round(float(row["total_profit"] or 0.0), 2),
    }


def query_parcel_profit_totals(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    *,
    parent_number: int | None,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
) -> dict[str, Any]:
    """
    Totals for Parcel Profit block, based on billing_records within the selected date range + scope.

    Uses the same scope/date filters as parcel billing queries (_parcel_billing_filters).
    """
    where_sql, params = _parcel_billing_filters(
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
    )
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS parcel_count,
            COALESCE(SUM(br.final_postage), 0) AS total_final_postage,
            COALESCE(SUM(br.fully_paid_postage), 0) AS total_fully_paid_postage,
            COALESCE(SUM(br.billing_amount), 0) AS total_billing_amount
        FROM billing_records br
        LEFT JOIN customers c ON br.custom_account_code = c.customer_number
        WHERE {where_sql}
        """,
        params,
    ).fetchone()
    if not row:
        return {
            "parcel_count": 0,
            "total_final_postage": 0.0,
            "total_fully_paid_postage": 0.0,
            "total_billing_amount": 0.0,
        }
    return {
        "parcel_count": int(row["parcel_count"] or 0),
        "total_final_postage": round(float(row["total_final_postage"] or 0.0), 2),
        "total_fully_paid_postage": round(float(row["total_fully_paid_postage"] or 0.0), 2),
        "total_billing_amount": round(float(row["total_billing_amount"] or 0.0), 2),
    }


def recompute_ws3_parent_rejects_for_mail_dates(
    conn: sqlite3.Connection, mail_dates: list[str]
) -> None:
    """Rebuild ws3_parent_daily_rejects for each mail_date from detail + profile parent links."""
    for md in mail_dates:
        conn.execute("DELETE FROM ws3_parent_daily_rejects WHERE mail_date = ?", (md,))
        conn.execute(
            """
            INSERT INTO ws3_parent_daily_rejects (mail_date, parent_customer_number, reject_count)
            SELECT r.mail_date, pr.parent_customer_number, COALESCE(SUM(d.pcs_rejected), 0)
            FROM ws3_mail_detail d
            JOIN ws3_profiles pr ON d.profile_id = pr.id
            JOIN ws3_mail_runs r ON d.run_id = r.run_id
            WHERE r.mail_date = ? AND pr.parent_customer_number IS NOT NULL
            GROUP BY r.mail_date, pr.parent_customer_number
            """,
            (md,),
        )


def recompute_ws3_rejects_for_profile(conn: sqlite3.Connection, profile_id: int) -> None:
    cur = conn.execute(
        """
        SELECT DISTINCT r.mail_date
        FROM ws3_mail_detail d
        JOIN ws3_mail_runs r ON d.run_id = r.run_id
        WHERE d.profile_id = ?
        """,
        (int(profile_id),),
    )
    dates = [str(row[0]) for row in cur.fetchall() if row[0]]
    recompute_ws3_parent_rejects_for_mail_dates(conn, dates)


def list_ws3_profiles(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT p.id, p.profile_name, p.parent_customer_number, p.reject_fee,
               par.customer_name AS parent_customer_name
        FROM ws3_profiles p
        LEFT JOIN customers par ON par.customer_number = p.parent_customer_number
        ORDER BY p.profile_name COLLATE NOCASE
        """
    )
    out: list[dict[str, Any]] = []
    for r in cur.fetchall():
        rf = r["reject_fee"]
        out.append(
            {
                "id": int(r["id"]),
                "profile_name": r["profile_name"] or "",
                "parent_customer_number": (
                    int(r["parent_customer_number"])
                    if r["parent_customer_number"] is not None
                    else None
                ),
                "parent_customer_name": (r["parent_customer_name"] or "").strip()
                if r["parent_customer_name"]
                else None,
                "reject_fee": round(float(rf), 4) if rf is not None else None,
            }
        )
    return out


def list_ws3_assignment_accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """
    All customers, labeled for the UI:

    - **parent** — at least one other account lists this number as `parent_number`.
    - **main** — no child accounts (nothing uses this `customer_number` as `parent_number`).

    Every account is exactly one of these two.
    """
    cur = conn.execute(
        """
        SELECT customer_number, customer_name FROM customers ORDER BY customer_name COLLATE NOCASE
        """
    )
    rows = [dict(r) for r in cur.fetchall()]
    cur = conn.execute(
        """
        SELECT DISTINCT parent_number FROM customers WHERE parent_number IS NOT NULL
        """
    )
    parents_of_someone = {int(r[0]) for r in cur.fetchall() if r[0] is not None}

    out: list[dict[str, Any]] = []
    for r in rows:
        cn = int(r["customer_number"])
        nm = (r["customer_name"] or "").strip() or f"Account {cn}"
        kind = "parent" if cn in parents_of_someone else "main"
        out.append(
            {
                "customer_number": cn,
                "customer_name": nm,
                "kind": kind,
            }
        )
    out.sort(
        key=lambda x: (
            0 if x["kind"] == "parent" else 1,
            (x["customer_name"] or "").casefold(),
            x["customer_number"],
        )
    )
    return out


def customer_allowed_for_ws3_assignment(conn: sqlite3.Connection, customer_number: int) -> bool:
    r = conn.execute(
        "SELECT 1 FROM customers WHERE customer_number = ? LIMIT 1",
        (int(customer_number),),
    ).fetchone()
    return r is not None


def update_ws3_profile(
    conn: sqlite3.Connection,
    profile_id: int,
    parent_customer_number: int | None,
    reject_fee: float | None,
) -> dict[str, Any]:
    if parent_customer_number is not None:
        if not customer_allowed_for_ws3_assignment(conn, parent_customer_number):
            raise ValueError(
                "Parent account must be a parent company or a standalone main account (not a child)"
            )
        ex = conn.execute(
            "SELECT 1 FROM customers WHERE customer_number = ?",
            (int(parent_customer_number),),
        ).fetchone()
        if not ex:
            raise ValueError(f"Customer {parent_customer_number} does not exist")
    if reject_fee is not None and reject_fee < 0:
        raise ValueError("reject_fee must be non-negative")
    conn.execute(
        """
        UPDATE ws3_profiles
        SET parent_customer_number = ?, reject_fee = ?
        WHERE id = ?
        """,
        (parent_customer_number, reject_fee, int(profile_id)),
    )
    recompute_ws3_rejects_for_profile(conn, profile_id)
    return {"ok": True, "profile_id": int(profile_id)}


def update_ws3_profile_parent(
    conn: sqlite3.Connection, profile_id: int, parent_customer_number: int | None
) -> dict[str, Any]:
    """Backward-compatible: preserves existing reject_fee."""
    row = conn.execute(
        "SELECT reject_fee FROM ws3_profiles WHERE id = ?", (int(profile_id),)
    ).fetchone()
    if not row:
        raise ValueError(f"Profile {profile_id} not found")
    fee = float(row["reject_fee"]) if row["reject_fee"] is not None else None
    return update_ws3_profile(conn, profile_id, parent_customer_number, fee)


def _ws3_reject_rows_for_postage(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    parent_number: int | None,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
    consolidate: bool,
    remove_zeros: bool,
    hide_costs: bool,
) -> list[dict[str, Any]]:
    """Synthetic postage-shaped rows from ws3_parent_daily_rejects."""
    par = "par"
    conditions_ws3: list[str] = [f"r.mail_date BETWEEN ? AND ?"]
    conditions_post: list[str] = [f"r.file_date BETWEEN ? AND ?"]
    params: list[Any] = [start_date, end_date]

    if parent_number is not None:
        conditions_ws3.append(f"({par}.parent_number = ? OR {par}.customer_number = ?)")
        conditions_post.append(f"({par}.parent_number = ? OR {par}.customer_number = ?)")
        params.extend([parent_number, parent_number])
    if customer_number is not None:
        ep = effective_parent_account_for_ws3(conn, customer_number)
        conditions_ws3.append("r.parent_customer_number = ?")
        conditions_post.append("ep.customer_number = ?")
        params.append(ep)

    if not show_parents:
        conditions_ws3.append(
            f"""{par}.customer_number NOT IN (
            SELECT DISTINCT parent_number FROM customers WHERE parent_number IS NOT NULL
        )"""
        )
        conditions_post.append(
            f"""{par}.customer_number NOT IN (
            SELECT DISTINCT parent_number FROM customers WHERE parent_number IS NOT NULL
        )"""
        )
    if not show_main:
        conditions_ws3.append(
            f"NOT ({par}.parent_number IS NULL AND {par}.parent_name IS NULL)"
        )
        conditions_post.append(
            f"NOT ({par}.parent_number IS NULL AND {par}.parent_name IS NULL)"
        )

    where_sql = " AND ".join(conditions_ws3)
    where_post_sql = " AND ".join(conditions_post)

    if consolidate:
        sql = f"""
        SELECT r.parent_customer_number,
               SUM(r.reject_count) AS reject_count,
               {par}.customer_name AS par_name,
               {par}.parent_number AS par_parent_number,
               {par}.parent_name AS par_parent_name
        FROM ws3_parent_daily_rejects r
        JOIN customers {par} ON {par}.customer_number = r.parent_customer_number
        WHERE {where_sql}
        GROUP BY r.parent_customer_number, {par}.customer_name, {par}.parent_number, {par}.parent_name
        """
    else:
        sql = f"""
        SELECT r.mail_date, r.parent_customer_number, r.reject_count,
               {par}.customer_name AS par_name,
               {par}.parent_number AS par_parent_number,
               {par}.parent_name AS par_parent_name
        FROM ws3_parent_daily_rejects r
        JOIN customers {par} ON {par}.customer_number = r.parent_customer_number
        WHERE {where_sql}
        """

    cur = conn.execute(sql, params)
    ws3_rows = cur.fetchall()

    # Also pull postage-side presort rejects and roll them up to effective parent account so we can
    # display a single synthetic "Presort rejects" row consistent with WS3.
    # Note: these are informational only and not included in postage totals.
    if consolidate:
        post_sql = f"""
        SELECT ep.customer_number AS parent_customer_number,
               SUM(r.reject_count) AS reject_count,
               ep.customer_name AS par_name,
               ep.parent_number AS par_parent_number,
               ep.parent_name AS par_parent_name
        FROM postage_presort_rejects r
        JOIN customers c ON c.customer_number = r.account_code
        JOIN customers ep ON ep.customer_number = COALESCE(c.parent_number, c.customer_number)
        JOIN customers {par} ON {par}.customer_number = ep.customer_number
        WHERE {where_post_sql}
        GROUP BY ep.customer_number, ep.customer_name, ep.parent_number, ep.parent_name
        """
    else:
        post_sql = f"""
        SELECT r.file_date AS mail_date,
               ep.customer_number AS parent_customer_number,
               SUM(r.reject_count) AS reject_count,
               ep.customer_name AS par_name,
               ep.parent_number AS par_parent_number,
               ep.parent_name AS par_parent_name
        FROM postage_presort_rejects r
        JOIN customers c ON c.customer_number = r.account_code
        JOIN customers ep ON ep.customer_number = COALESCE(c.parent_number, c.customer_number)
        JOIN customers {par} ON {par}.customer_number = ep.customer_number
        WHERE {where_post_sql}
        GROUP BY r.file_date, ep.customer_number, ep.customer_name, ep.parent_number, ep.parent_name
        """
    post_rows = conn.execute(post_sql, params).fetchall()

    # Merge WS3 + postage-derived rows by key.
    merged: dict[tuple[str, int], dict[str, Any]] = {}
    def _key(r: sqlite3.Row) -> tuple[str, int]:
        if consolidate:
            return ("Combined", int(r["parent_customer_number"]))
        return (str(r["mail_date"]), int(r["parent_customer_number"]))

    for r in ws3_rows:
        merged[_key(r)] = dict(r)
    for r in post_rows:
        k = _key(r)
        if k in merged:
            merged[k]["reject_count"] = int(merged[k].get("reject_count") or 0) + int(r["reject_count"] or 0)
        else:
            merged[k] = dict(r)

    raw_rows = list(merged.values())

    oz_keys = [f"oz_{i}" for i in range(14)] + ["oz_13plus"]
    out: list[dict[str, Any]] = []
    for r in raw_rows:
        if consolidate:
            rc = int(r["reject_count"] or 0)
            if remove_zeros and rc == 0:
                continue
            pn = int(r["parent_customer_number"])
            par_name = r["par_name"] or ""
            parent_disp = (r["par_parent_name"] or "").strip() or par_name
            parent_num = (
                int(r["par_parent_number"])
                if r["par_parent_number"] is not None
                else pn
            )
            item: dict[str, Any] = {
                "date": "Combined",
                "parent_name": parent_disp,
                "parent_number": parent_num,
                "child_name": par_name,
                "child_number": pn,
                "mail_class": WS3_REJECT_MAIL_CLASS,
                **{k: 0 for k in oz_keys},
                "total_qty": rc,
            }
        else:
            rc = int(r["reject_count"] or 0)
            if remove_zeros and rc == 0:
                continue
            pn = int(r["parent_customer_number"])
            par_name = r["par_name"] or ""
            parent_disp = (r["par_parent_name"] or "").strip() or par_name
            parent_num = (
                int(r["par_parent_number"])
                if r["par_parent_number"] is not None
                else pn
            )
            fd = r["mail_date"]
            date_str = str(fd) if fd is not None else ""
            item = {
                "date": date_str,
                "parent_name": parent_disp,
                "parent_number": parent_num,
                "child_name": par_name,
                "child_number": pn,
                "mail_class": WS3_REJECT_MAIL_CLASS,
                **{k: 0 for k in oz_keys},
                "total_qty": rc,
            }
        if not hide_costs:
            item["total_cost"] = 0.0
            item["metered_cost"] = 0.0
            item["retail_cost"] = 0.0
        out.append(item)

    out.sort(
        key=lambda x: (
            x["date"] == "Combined",
            str(x["date"]),
            (x["parent_name"] or "").casefold(),
            (x["child_name"] or "").casefold(),
            (x["mail_class"] or "").casefold(),
        )
    )
    return out


def backfill_postage_uplift_othercls_1120_as_presort_rejects(
    conn: sqlite3.Connection,
    *,
    start_date: str = "2026-04-20",
    end_date: str = "2026-04-25",
    source: str = "bm_uplift_1120_othercls_backfill",
) -> dict[str, int]:
    """
    Data repair:
    Move bogus BM/DM uplift rows (OtherCls @ 1120 oz) out of `postage_data` and into
    `postage_presort_rejects` for the specified date range.
    """
    cur = conn.cursor()
    hits = cur.execute(
        """
        SELECT file_date, account_code, SUM(pieces) AS pcs, COUNT(*) AS row_count
        FROM postage_data
        WHERE file_date BETWEEN ? AND ?
          AND UPPER(mail_class) = 'OTHERCLS'
          AND weight_oz = 1120.0
        GROUP BY file_date, account_code
        """,
        (start_date, end_date),
    ).fetchall()

    moved_rejects = 0
    deleted_rows = 0
    for r in hits:
        fd = str(r["file_date"])
        acct = int(r["account_code"])
        pcs = int(r["pcs"] or 0)
        if pcs > 0:
            record_postage_presort_rejects(
                conn,
                file_date=fd,
                account_code=acct,
                reject_count=pcs,
                source=source,
                import_id=None,
            )
            moved_rejects += pcs
        cur.execute(
            """
            DELETE FROM postage_data
            WHERE file_date = ?
              AND account_code = ?
              AND UPPER(mail_class) = 'OTHERCLS'
              AND weight_oz = 1120.0
            """,
            (fd, acct),
        )
        deleted_rows += int(cur.rowcount or 0)

    return {
        "groups_touched": int(len(hits)),
        "postage_rows_deleted": int(deleted_rows),
        "reject_pieces_moved": int(moved_rejects),
    }


def query_postage(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    parent_number: int | None,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
    consolidate: bool,
    remove_zeros: bool,
    hide_costs: bool,
    kc_presort: bool = False,
    efd: bool = False,
) -> dict[str, Any]:
    _ = efd  # reserved for future reporting checks
    date_sel = "p.file_date" if not consolidate else "NULL"
    date_group = "p.file_date, " if not consolidate else ""
    order_date = "p.file_date, " if not consolidate else ""

    where_sql, params = postage_scope_where_clause(
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
    )

    kc_in = "(" + ",".join(f"'{c}'" for c in KC_PRESORT_METERED_MAIL_CLASSES_UPPER) + ")"
    eligible_qty_cond = "(p.weight_oz IS NULL OR p.weight_oz <= 13)"
    if kc_presort:
        eligible_qty_cond = (
            f"({eligible_qty_cond} OR (p.weight_oz > 13 AND UPPER(p.mail_class) IN {kc_in}))"
        )
        oz_13plus_sel = f"SUM(CASE WHEN (p.weight_oz > 13 AND UPPER(p.mail_class) IN {kc_in}) THEN p.pieces ELSE 0 END) AS oz_13plus"
    else:
        oz_13plus_sel = "0 AS oz_13plus"

    sql = f"""
    SELECT
        {date_sel} AS file_date,
        COALESCE(c.parent_name, c.customer_name) AS parent_name,
        COALESCE(c.parent_number, c.customer_number) AS parent_number,
        c.customer_name AS child_name,
        c.customer_number AS child_number,
        p.mail_class,
        SUM(CASE WHEN p.weight_oz = 0  THEN p.pieces ELSE 0 END) AS oz_0,
        SUM(CASE WHEN p.weight_oz = 1  THEN p.pieces ELSE 0 END) AS oz_1,
        SUM(CASE WHEN p.weight_oz = 2  THEN p.pieces ELSE 0 END) AS oz_2,
        SUM(CASE WHEN p.weight_oz = 3  THEN p.pieces ELSE 0 END) AS oz_3,
        SUM(CASE WHEN p.weight_oz = 4  THEN p.pieces ELSE 0 END) AS oz_4,
        SUM(CASE WHEN p.weight_oz = 5  THEN p.pieces ELSE 0 END) AS oz_5,
        SUM(CASE WHEN p.weight_oz = 6  THEN p.pieces ELSE 0 END) AS oz_6,
        SUM(CASE WHEN p.weight_oz = 7  THEN p.pieces ELSE 0 END) AS oz_7,
        SUM(CASE WHEN p.weight_oz = 8  THEN p.pieces ELSE 0 END) AS oz_8,
        SUM(CASE WHEN p.weight_oz = 9  THEN p.pieces ELSE 0 END) AS oz_9,
        SUM(CASE WHEN p.weight_oz = 10 THEN p.pieces ELSE 0 END) AS oz_10,
        SUM(CASE WHEN p.weight_oz = 11 THEN p.pieces ELSE 0 END) AS oz_11,
        SUM(CASE WHEN p.weight_oz = 12 THEN p.pieces ELSE 0 END) AS oz_12,
        SUM(CASE WHEN p.weight_oz = 13 THEN p.pieces ELSE 0 END) AS oz_13,
        {oz_13plus_sel},
        SUM(CASE WHEN {eligible_qty_cond} THEN p.pieces ELSE 0 END) AS total_qty,
        SUM(CASE WHEN {eligible_qty_cond} THEN
                 CASE
                   WHEN p.mail_class IN {POSTAGE_INVOICE_FLAT_MAIL_SQL_IN}
                        AND p.mail_class <> '{INVOICE_EFD_FLAT_MAIL_CLASS}'
                        AND CAST(ROUND(p.weight_oz) AS INTEGER) BETWEEN 1 AND 13
                        AND COALESCE(frc.rate_retail, 0) > 0
                   THEN p.pieces * frc.rate_retail
                   ELSE p.total_cost
                 END
            ELSE 0 END) AS total_cost,
        SUM(CASE WHEN {eligible_qty_cond}
                 AND p.mail_class IN {POSTAGE_INVOICE_FLAT_MAIL_SQL_IN}
                 AND CAST(ROUND(p.weight_oz) AS INTEGER) BETWEEN 1 AND 13
                 AND COALESCE(fret.rate_retail, 0) > 0
            THEN p.pieces * fret.rate_retail
            ELSE 0 END) AS retail_cost
    FROM postage_data p
    JOIN customers c ON p.account_code = c.customer_number
    LEFT JOIN flat_rate_costs frc
      ON p.mail_class IN {POSTAGE_INVOICE_FLAT_MAIL_SQL_IN}
     AND p.mail_class <> '{INVOICE_EFD_FLAT_MAIL_CLASS}'
     AND CAST(ROUND(p.weight_oz) AS INTEGER) BETWEEN 1 AND 13
     AND CAST(ROUND(p.weight_oz) AS INTEGER) = CAST(frc.weight_not_over_oz AS INTEGER)
    LEFT JOIN flat_rate_costs fret
      ON p.mail_class IN {POSTAGE_INVOICE_FLAT_MAIL_SQL_IN}
     AND CAST(ROUND(p.weight_oz) AS INTEGER) BETWEEN 1 AND 13
     AND CAST(ROUND(p.weight_oz) AS INTEGER) = CAST(fret.weight_not_over_oz AS INTEGER)
    WHERE {where_sql}
    GROUP BY {date_group}c.customer_number, p.mail_class
    ORDER BY {order_date}parent_name, c.customer_name, p.mail_class
    """

    cur = conn.execute(sql, params)
    rows_raw = [dict(r) for r in cur.fetchall()]

    rows: list[dict[str, Any]] = []
    total_pieces = 0
    total_cost_sum = 0.0
    total_retail_sum = 0.0

    for r in rows_raw:
        oz_keys = [f"oz_{i}" for i in range(14)] + ["oz_13plus"]
        oz_vals = {k: int(r[k] or 0) for k in oz_keys}
        tq = int(r["total_qty"] or 0)
        tc = float(r["total_cost"] or 0.0)
        trc = float(r.get("retail_cost") or 0.0)

        if remove_zeros and all(v == 0 for v in oz_vals.values()):
            continue

        fd = r["file_date"]
        if consolidate or fd is None:
            date_str = "Combined"
        else:
            date_str = str(fd)

        item: dict[str, Any] = {
            "date": date_str,
            "parent_name": r["parent_name"],
            "parent_number": r["parent_number"],
            "child_name": r["child_name"],
            "child_number": r["child_number"],
            "mail_class": r["mail_class"],
            **oz_vals,
            "total_qty": tq,
        }
        if not hide_costs:
            tc_r = round(tc, 2)
            tr_r = round(trc, 2)
            item["total_cost"] = tc_r
            item["metered_cost"] = tc_r
            item["retail_cost"] = tr_r
        rows.append(item)
        total_pieces += tq
        total_cost_sum += tc
        total_retail_sum += trc

    rej_rows = _ws3_reject_rows_for_postage(
        conn,
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
        consolidate,
        remove_zeros,
        hide_costs,
    )
    for rj in rej_rows:
        rows.append(rj)
        # Presort rejects are informational only; do not add to total_pieces (already metered).

    rows.sort(
        key=lambda x: (
            x["date"] == "Combined",
            str(x["date"]),
            (x["parent_name"] or "").casefold(),
            (x["child_name"] or "").casefold(),
            (x["mail_class"] or "").casefold(),
        )
    )

    out: dict[str, Any] = {
        "total_records": len(rows),
        "total_pieces": total_pieces,
        "total_cost": round(total_cost_sum, 2),
        "total_metered_cost": round(total_cost_sum, 2),
        "total_retail_cost": round(total_retail_sum, 2),
        "rows": rows,
    }
    if hide_costs:
        out.pop("total_cost", None)
        out.pop("total_metered_cost", None)
        out.pop("total_retail_cost", None)
    return out


def get_postage_row_details(
    conn: sqlite3.Connection,
    file_date: str,
    account_code: int,
    mail_class: str,
) -> list[dict[str, Any]]:
    """
    Underlying `postage_data` records for one dashboard row: date × account × mail_class.
    Returned rows include ids for audit/merge operations.
    """
    cur = conn.execute(
        """
        SELECT id, import_id, file_date, account_code, mail_class, weight_oz, pieces, total_cost, unmatched_account
        FROM postage_data
        WHERE file_date = ?
          AND account_code = ?
          AND mail_class = ?
        ORDER BY weight_oz ASC
        """,
        (file_date, int(account_code), str(mail_class)),
    )
    out: list[dict[str, Any]] = []
    for r in cur.fetchall():
        out.append(
            {
                "id": int(r["id"]),
                "import_id": int(r["import_id"]),
                "file_date": str(r["file_date"]),
                "account_code": int(r["account_code"]),
                "mail_class": str(r["mail_class"]),
                "weight_oz": float(r["weight_oz"]),
                "pieces": int(r["pieces"] or 0),
                "total_cost": round(float(r["total_cost"] or 0.0), 2),
                "unmatched_account": int(r["unmatched_account"] or 0),
            }
        )
    return out


def _scale_total_cost(old_cost: float, old_pieces: int, new_pieces: int) -> float:
    if old_pieces > 0:
        return float(old_cost) * (float(new_pieces) / float(old_pieces))
    if new_pieces == 0:
        return 0.0
    return float(old_cost)


def _customer_exists(conn: sqlite3.Connection, customer_number: int) -> bool:
    r = conn.execute(
        "SELECT 1 FROM customers WHERE customer_number = ? LIMIT 1", (int(customer_number),)
    ).fetchone()
    return r is not None


def preview_postage_row_update(
    conn: sqlite3.Connection,
    file_date: str,
    from_account_code: int,
    mail_class: str,
    to_account_code: int,
    pieces_by_id: dict[str, Any] | None,
) -> dict[str, Any]:
    rows = get_postage_row_details(conn, file_date, from_account_code, mail_class)
    if not rows:
        return {"ok": True, "rows": [], "summary": {"source_rows": 0, "updated": 0, "merged": 0}}

    if pieces_by_id is None:
        pieces_by_id = {}

    # Validate pieces_by_id early
    normalized_pieces: dict[int, int] = {}
    for k, v in pieces_by_id.items():
        try:
            pid = int(k)
            pv = int(v)
        except (TypeError, ValueError):
            continue
        if pv < 0:
            raise ValueError("pieces cannot be negative")
        normalized_pieces[pid] = pv

    to_unmatched = 0 if _customer_exists(conn, to_account_code) else 1

    lines: list[dict[str, Any]] = []
    merged = 0
    updated = 0

    for r in rows:
        src_id = int(r["id"])
        old_p = int(r["pieces"])
        old_c = float(r["total_cost"] or 0.0)
        new_p = int(normalized_pieces.get(src_id, old_p))
        new_c = _scale_total_cost(old_c, old_p, new_p)

        dest = conn.execute(
            """
            SELECT id, pieces, total_cost
            FROM postage_data
            WHERE import_id = ?
              AND file_date = ?
              AND account_code = ?
              AND mail_class = ?
              AND weight_oz = ?
            LIMIT 1
            """,
            (
                int(r["import_id"]),
                file_date,
                int(to_account_code),
                mail_class,
                float(r["weight_oz"]),
            ),
        ).fetchone()
        if dest and int(dest["id"]) != src_id:
            merged += 1
            action = "merged"
            dest_id = int(dest["id"])
            dest_old_p = int(dest["pieces"] or 0)
            dest_old_c = float(dest["total_cost"] or 0.0)
            dest_new_p = dest_old_p + new_p
            dest_new_c = dest_old_c + new_c
        else:
            updated += 1
            action = "updated"
            dest_id = None
            dest_old_p = None
            dest_old_c = None
            dest_new_p = None
            dest_new_c = None

        lines.append(
            {
                "source_postage_id": src_id,
                "import_id": int(r["import_id"]),
                "weight_oz": float(r["weight_oz"]),
                "old_account_code": int(from_account_code),
                "new_account_code": int(to_account_code),
                "old_pieces": old_p,
                "new_pieces": new_p,
                "old_total_cost": round(old_c, 2),
                "new_total_cost": round(new_c, 2),
                "action": action,
                "dest_postage_id": dest_id,
                "dest_before": (
                    None
                    if dest_id is None
                    else {
                        "old_pieces": dest_old_p,
                        "old_total_cost": round(dest_old_c, 2),
                        "new_pieces": dest_new_p,
                        "new_total_cost": round(dest_new_c, 2),
                    }
                ),
            }
        )

    return {
        "ok": True,
        "to_unmatched_account": to_unmatched,
        "rows": lines,
        "summary": {
            "source_rows": len(rows),
            "updated": updated,
            "merged": merged,
        },
    }


def apply_postage_row_update(
    conn: sqlite3.Connection,
    file_date: str,
    from_account_code: int,
    mail_class: str,
    to_account_code: int,
    pieces_by_id: dict[str, Any] | None,
    reason: str | None = None,
) -> dict[str, Any]:
    preview = preview_postage_row_update(
        conn,
        file_date=file_date,
        from_account_code=from_account_code,
        mail_class=mail_class,
        to_account_code=to_account_code,
        pieces_by_id=pieces_by_id,
    )
    lines = preview.get("rows") or []
    to_unmatched = int(preview.get("to_unmatched_account") or 0)

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO postage_edits (file_date, from_account_code, to_account_code, mail_class, reason, merged_rows, updated_rows)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_date,
            int(from_account_code),
            int(to_account_code),
            str(mail_class),
            (reason or "").strip() or None,
            int(preview["summary"]["merged"]),
            int(preview["summary"]["updated"]),
        ),
    )
    edit_id = int(cur.lastrowid)

    merged_count = 0
    updated_count = 0

    for ln in lines:
        src_id = int(ln["source_postage_id"])
        new_p = int(ln["new_pieces"])
        new_c = float(ln["new_total_cost"] or 0.0)
        dest_id = ln.get("dest_postage_id")

        # Grab current source snapshot for audit
        src = cur.execute(
            """
            SELECT account_code, weight_oz, pieces, total_cost, import_id
            FROM postage_data
            WHERE id = ?
            """,
            (src_id,),
        ).fetchone()
        if not src:
            continue

        old_acc = int(src["account_code"])
        woz = float(src["weight_oz"])
        old_p = int(src["pieces"] or 0)
        old_c = float(src["total_cost"] or 0.0)
        imp_id = int(src["import_id"])

        if dest_id is not None:
            dest_id_int = int(dest_id)
            dest = cur.execute(
                "SELECT pieces, total_cost FROM postage_data WHERE id = ?",
                (dest_id_int,),
            ).fetchone()
            if not dest:
                # Fallback: treat as non-merge
                cur.execute(
                    """
                    UPDATE postage_data
                    SET account_code = ?, pieces = ?, total_cost = ?, unmatched_account = ?
                    WHERE id = ?
                    """,
                    (int(to_account_code), new_p, float(new_c), to_unmatched, src_id),
                )
                updated_count += 1
                action = "updated"
                dest_id_int = None
                new_acc = int(to_account_code)
                final_p = new_p
                final_c = float(new_c)
            else:
                dest_old_p = int(dest["pieces"] or 0)
                dest_old_c = float(dest["total_cost"] or 0.0)
                dest_new_p = dest_old_p + new_p
                dest_new_c = dest_old_c + float(new_c)
                cur.execute(
                    """
                    UPDATE postage_data
                    SET pieces = ?, total_cost = ?, unmatched_account = ?
                    WHERE id = ?
                    """,
                    (dest_new_p, dest_new_c, to_unmatched, dest_id_int),
                )
                cur.execute("DELETE FROM postage_data WHERE id = ?", (src_id,))
                merged_count += 1
                action = "merged"
                new_acc = int(to_account_code)
                final_p = new_p
                final_c = float(new_c)
        else:
            # Guard uniqueness: if an identical dest row appeared between preview and apply, merge it now.
            dest = cur.execute(
                """
                SELECT id, pieces, total_cost
                FROM postage_data
                WHERE import_id = ?
                  AND file_date = ?
                  AND account_code = ?
                  AND mail_class = ?
                  AND weight_oz = ?
                LIMIT 1
                """,
                (imp_id, file_date, int(to_account_code), str(mail_class), woz),
            ).fetchone()
            if dest and int(dest["id"]) != src_id:
                dest_id_int = int(dest["id"])
                dest_old_p = int(dest["pieces"] or 0)
                dest_old_c = float(dest["total_cost"] or 0.0)
                dest_new_p = dest_old_p + new_p
                dest_new_c = dest_old_c + float(new_c)
                cur.execute(
                    """
                    UPDATE postage_data
                    SET pieces = ?, total_cost = ?, unmatched_account = ?
                    WHERE id = ?
                    """,
                    (dest_new_p, dest_new_c, to_unmatched, dest_id_int),
                )
                cur.execute("DELETE FROM postage_data WHERE id = ?", (src_id,))
                merged_count += 1
                action = "merged"
                dest_id_int = dest_id_int
            else:
                cur.execute(
                    """
                    UPDATE postage_data
                    SET account_code = ?, pieces = ?, total_cost = ?, unmatched_account = ?
                    WHERE id = ?
                    """,
                    (int(to_account_code), new_p, float(new_c), to_unmatched, src_id),
                )
                updated_count += 1
                action = "updated"
                dest_id_int = None

            new_acc = int(to_account_code)
            final_p = new_p
            final_c = float(new_c)

        cur.execute(
            """
            INSERT INTO postage_edit_lines (
                edit_id, source_postage_id, dest_postage_id, action, weight_oz,
                old_account_code, new_account_code, old_pieces, new_pieces, old_total_cost, new_total_cost
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                edit_id,
                src_id,
                int(dest_id_int) if dest_id_int is not None else None,
                action,
                woz,
                old_acc,
                new_acc,
                old_p,
                final_p,
                float(old_c),
                float(final_c),
            ),
        )

    return {
        "ok": True,
        "edit_id": edit_id,
        "summary": {"updated": updated_count, "merged": merged_count},
    }


def _parcel_lb_bucket(weight_oz: float | None) -> int | None:
    if weight_oz is None or weight_oz <= 0:
        return None
    b = math.ceil(weight_oz / 16.0)
    return min(int(b), 11)


def parcel_weight_lb_int(weight_oz: float | None) -> int | None:
    """Integer lb bucket (ceil oz/16), capped at 100, for heavy-parcel lookup."""
    if weight_oz is None or weight_oz <= 0:
        return None
    return min(int(math.ceil(weight_oz / 16.0)), 100)


def _parcel_billing_filters(
    start_date: str,
    end_date: str,
    parent_number: int | None,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
) -> tuple[str, list[Any]]:
    """Shared WHERE clause (no leading WHERE) for parcel billing: date range, weight, account scope."""
    ts_date = _billing_ts_date_sql("br.time_stamp")
    conditions = [
        f"{ts_date} BETWEEN ? AND ?",
        "(br.weight_oz IS NOT NULL AND br.weight_oz > 0)",
    ]
    params: list[Any] = [start_date, end_date]

    if parent_number is not None:
        conditions.append(
            "(c.customer_number IS NOT NULL AND (c.parent_number = ? OR c.customer_number = ?))"
        )
        params.extend([parent_number, parent_number])
    if customer_number is not None:
        conditions.append("br.custom_account_code = ?")
        params.append(customer_number)

    matched_filters: list[str] = []
    if not show_parents:
        matched_filters.append(
            """c.customer_number NOT IN (
            SELECT DISTINCT parent_number FROM customers WHERE parent_number IS NOT NULL
        )"""
        )
    if not show_main:
        matched_filters.append("NOT (c.parent_number IS NULL AND c.parent_name IS NULL)")
    if matched_filters:
        inner = " AND ".join(matched_filters)
        conditions.append(f"(c.customer_number IS NULL OR ({inner}))")

    return " AND ".join(conditions), params


def _postage_over_13_per_piece_retail(
    conn: sqlite3.Connection,
    *,
    weight_oz: float,
    pieces: int,
    total_cost: float,
    pm_rates: tuple[dict[tuple[int, int], float], dict[int, list[int]]],
) -> float:
    """Matrix retail at default zone for postage BM row, with total_cost/pieces fallback."""
    fb = (float(total_cost) / pieces) if pieces else 0.0
    priced = compute_retail_cost_for_piece(
        conn,
        weight_oz=weight_oz,
        zone_raw=DEFAULT_PARCEL_ZONE_FOR_POSTAGE_OVER_13_OZ,
        fallback_retail=fb,
        pm_rates=pm_rates,
    )
    return float(priced["retail"] or 0.0)


def query_parcel_report_rows(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    parent_number: int | None,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
) -> list[dict[str, Any]]:
    """One row per billing piece for BC Priority parcel export, plus one row per postage piece over 13 oz."""
    where_sql, params = _parcel_billing_filters(
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
    )
    cur = conn.execute(
        f"""
        SELECT br.custom_account_code,
               COALESCE(NULLIF(TRIM(c.customer_name), ''), '(no name)') AS account_name,
               CASE WHEN c.customer_number IS NULL THEN NULL
                    ELSE COALESCE(c.parent_name, c.customer_name) END AS parent_name,
               br.piece_id, br.time_stamp, br.usps_mail_class, br.zone,
               br.weight_oz, br.department_name, br.handling_type, br.impb
        FROM billing_records br
        LEFT JOIN customers c ON br.custom_account_code = c.customer_number
        WHERE {where_sql}
        ORDER BY br.time_stamp, br.custom_account_code, br.usps_mail_class, br.piece_id
        """,
        params,
    )
    out: list[dict[str, Any]] = [dict(r) for r in cur.fetchall()]

    for pr in _postage_over_13_parcel_scope_rows(
        conn,
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
        consolidate=False,
    ):
        pieces = int(pr["pieces"] or 0)
        if pieces <= 0:
            continue
        woz = float(pr["weight_oz"] or 0.0)
        if _parcel_lb_bucket(woz) is None:
            continue
        acc = pr["child_number"]
        fd = pr["bill_date"]
        mail = pr["mail_class"]
        ts = f"{fd} 12:00:00" if fd is not None else ""
        zstr = str(DEFAULT_PARCEL_ZONE_FOR_POSTAGE_OVER_13_OZ)
        for i in range(pieces):
            out.append(
                {
                    "custom_account_code": acc,
                    "account_name": (pr["child_name"] or "").strip() or "(no name)",
                    "parent_name": pr["parent_name"],
                    "piece_id": f"POSTAGE:{fd}:{acc}:{mail}:{woz}:{i + 1}",
                    "time_stamp": ts,
                    "usps_mail_class": mail,
                    "zone": zstr,
                    "weight_oz": woz,
                    "department_name": "",
                    "handling_type": "POSTAGE",
                    "impb": None,
                }
            )

    return out


def query_parcel_billing_rows_full(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    parent_number: int | None,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
) -> list[sqlite3.Row]:
    """
    Full raw billing rows in scope, including all billing_records columns.

    This is intended for the consolidated parcel CSV export (raw payload).
    """
    where_sql, params = _parcel_billing_filters(
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
    )
    cur = conn.execute(
        f"""
        SELECT
            CASE WHEN c.customer_number IS NULL THEN NULL
                 ELSE COALESCE(c.parent_name, c.customer_name) END AS parent_name,
            CASE WHEN c.customer_number IS NULL THEN NULL
                 ELSE COALESCE(c.parent_number, c.customer_number) END AS parent_number,
            br.*
        FROM billing_records br
        LEFT JOIN customers c ON br.custom_account_code = c.customer_number
        WHERE {where_sql}
        ORDER BY br.time_stamp, br.custom_account_code, br.usps_mail_class, br.piece_id
        """,
        params,
    )
    return cur.fetchall()


def _parse_money_csv(cell: Any) -> float | None:
    if cell is None:
        return None
    s = str(cell).strip().replace("$", "").replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_lb_label_csv(cell: Any) -> int | None:
    m = re.match(r"^(\d+)\s*lb", str(cell).strip(), re.I)
    return int(m.group(1)) if m else None


def _parse_parcel_summary_csv(path: Path) -> dict[tuple[int, int], tuple[float, float]]:
    """Read (zone, lb 1–10) -> (retail / Priority, EFD / discounted) from parcel summary.csv."""
    out: dict[tuple[int, int], tuple[float, float]] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    i = 0
    while i < len(rows):
        row = rows[i]
        seen: list[int] = []
        for cell in row:
            m = re.search(r"Zone\s*(\d+)", str(cell), re.I)
            if m:
                z = int(m.group(1))
                if not seen or seen[-1] != z:
                    seen.append(z)
        if len(seen) >= 2:
            za, zb = seen[0], seen[1]
            i += 1
            for _ in range(10):
                if i >= len(rows):
                    break
                r = rows[i]
                lb = _parse_lb_label_csv(r[0] if r else "")
                if lb is not None and 1 <= lb <= 10 and len(r) > 5:
                    ra, ea = _parse_money_csv(r[1]), _parse_money_csv(r[2])
                    rb, eb = _parse_money_csv(r[4]), _parse_money_csv(r[5])
                    if ra is not None and ea is not None:
                        out[(za, lb)] = (ra, ea)
                    if rb is not None and eb is not None:
                        out[(zb, lb)] = (rb, eb)
                i += 1
            continue
        i += 1
    return out


def get_parcel_summary_rates() -> dict[tuple[int, int], tuple[float, float]]:
    """Retail (Priority) and EFD prices per USPS zone (1–8) and weight row (1–10 lb), from parcel summary.csv."""
    global _parcel_summary_rates_cache
    path = PARCEL_SUMMARY_RATES_CSV
    if not path.is_file():
        return {}
    mtime = path.stat().st_mtime
    if _parcel_summary_rates_cache is not None and _parcel_summary_rates_cache[0] == mtime:
        return _parcel_summary_rates_cache[1]
    parsed = _parse_parcel_summary_csv(path)
    _parcel_summary_rates_cache = (mtime, parsed)
    return parsed


def clear_parcel_summary_rates_cache() -> None:
    global _parcel_summary_rates_cache
    _parcel_summary_rates_cache = None


def _parse_heavy_parcel_rates_csv(path: Path) -> dict[tuple[int, int], tuple[float, float]]:
    """(zone, weight_lb) -> (base retail per unit, efd per unit) from heavy_parcel_rates.csv."""
    out: dict[tuple[int, int], tuple[float, float]] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                z = int(str(row.get("zone", "")).strip())
                lb = int(str(row.get("weight_lb", "")).strip())
                base = float(str(row.get("base", "")).strip())
                efd = float(str(row.get("efd_per_unit", "")).strip())
            except (TypeError, ValueError):
                continue
            if 1 <= z <= 8 and 1 <= lb <= 100:
                out[(z, lb)] = (base, efd)
    return out


def get_heavy_parcel_rates() -> dict[tuple[int, int], tuple[float, float]]:
    """Retail (Base) and EFD per unit for USPS zones 1–8 and weight 1–100 lb."""
    global _heavy_parcel_rates_cache
    path = HEAVY_PARCEL_RATES_CSV
    if not path.is_file():
        return {}
    mtime = path.stat().st_mtime
    if _heavy_parcel_rates_cache is not None and _heavy_parcel_rates_cache[0] == mtime:
        return _heavy_parcel_rates_cache[1]
    parsed = _parse_heavy_parcel_rates_csv(path)
    _heavy_parcel_rates_cache = (mtime, parsed)
    return parsed


def clear_heavy_parcel_rates_cache() -> None:
    global _heavy_parcel_rates_cache
    _heavy_parcel_rates_cache = None


def _normalize_parcel_zone(zone: Any) -> int | None:
    """USPS zones 1–9 from billing zone text (e.g. '5', 'Zone 3')."""
    if zone is None:
        return None
    s = str(zone).strip()
    if not s:
        return None
    m = re.search(r"(\d+)", s)
    if not m:
        return None
    z = int(m.group(1))
    if 1 <= z <= 9:
        return z
    return None


def _load_retail_matrix_rates(
    conn: sqlite3.Connection,
    *,
    table: str,
    weight_unit: str,
) -> tuple[dict[tuple[int, int], float], dict[int, list[int]]]:
    """
    Load retail matrix rates from `priority_mail_retail` or `ground_advantage_retail`.

    Returns:
    - rate_by_zone_lb: (zone, lb) -> price
    - weights_by_zone: zone -> sorted list of available lb values
    """
    cur = conn.execute(
        f"""
        SELECT zone, weight_max, price
        FROM {table}
        WHERE row_type = 'matrix'
          AND weight_unit = ?
          AND zone IS NOT NULL
          AND weight_max IS NOT NULL
          AND price IS NOT NULL
        """,
        (str(weight_unit),),
    )
    rate_by_zone_lb: dict[tuple[int, int], float] = {}
    weights_by_zone: dict[int, set[int]] = {}
    for r in cur.fetchall():
        try:
            z = int(r["zone"])
            lb = int(round(float(r["weight_max"])))
            price = float(r["price"])
        except (TypeError, ValueError):
            continue
        if z < 1 or z > 9 or lb <= 0:
            continue
        rate_by_zone_lb[(z, lb)] = price
        weights_by_zone.setdefault(z, set()).add(lb)

    weights_sorted: dict[int, list[int]] = {
        z: sorted(list(wset)) for z, wset in weights_by_zone.items()
    }
    return rate_by_zone_lb, weights_sorted


def get_ground_advantage_retail_rates(
    conn: sqlite3.Connection,
) -> tuple[dict[tuple[int, int], float], dict[int, list[int]]]:
    """(zone, lb) -> GA retail price, and per-zone available weights."""
    return _load_retail_matrix_rates(conn, table="ground_advantage_retail", weight_unit="lb")


def get_priority_mail_retail_rates(
    conn: sqlite3.Connection,
) -> tuple[dict[tuple[int, int], float], dict[int, list[int]]]:
    """(zone, lb) -> Priority Mail retail price, and per-zone available weights."""
    return _load_retail_matrix_rates(conn, table="priority_mail_retail", weight_unit="lb")


def _retail_price_for_zone_lb(
    rate_by_zone_lb: dict[tuple[int, int], float],
    weights_by_zone: dict[int, list[int]],
    *,
    zone: int,
    lb: int,
) -> float | None:
    """Exact (zone,lb) if present; else nearest higher lb for that zone."""
    if zone < 1 or zone > 9 or lb <= 0:
        return None
    v = rate_by_zone_lb.get((zone, lb))
    if v is not None:
        return float(v)
    wlist = weights_by_zone.get(zone) or []
    for w in wlist:
        if w >= lb:
            vv = rate_by_zone_lb.get((zone, w))
            if vv is not None:
                return float(vv)
            break
    return None


def compute_retail_cost_for_piece(
    conn: sqlite3.Connection,
    *,
    weight_oz: float | None,
    zone_raw: Any,
    fallback_retail: float | None = None,
    ga_rates: tuple[dict[tuple[int, int], float], dict[int, list[int]]] | None = None,
    pm_rates: tuple[dict[tuple[int, int], float], dict[int, list[int]]] | None = None,
) -> dict[str, Any]:
    """
    Retail costing rule (parcels): Priority Mail retail matrix for all weights.
    Zone 1–9 required; otherwise uses fallback_retail.
    ga_rates is accepted for call-site compatibility but is not used.

    Returns dict with keys: service, zone, lb, retail.
    """
    z = _normalize_parcel_zone(zone_raw)
    lb = parcel_weight_lb_int(weight_oz)
    service: str | None = "PRIORITY_MAIL" if lb is not None else None

    retail: float | None = None
    if z is not None and lb is not None and service is not None:
        if pm_rates is None:
            pm_rates = get_priority_mail_retail_rates(conn)
        retail = _retail_price_for_zone_lb(
            pm_rates[0], pm_rates[1], zone=int(z), lb=int(lb)
        )
    if retail is None and fallback_retail is not None:
        try:
            retail = float(fallback_retail)
        except (TypeError, ValueError):
            retail = None

    return {"service": service, "zone": z, "lb": lb, "retail": retail}


def compute_parcel_report_af_hm_sections(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    parent_number: int | None,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
    *,
    parcel_discount: float = 0.25,
) -> dict[str, Any]:
    """
    Over-10lb block: aggregates for 11–100 lb by (customer, lb, zone) using retail from
    `priority_mail_retail` (fallback: billing `fully_paid_postage`).

    Per-customer **discount** totals are what the customer pays (retail − savings per piece, floored at 0);
    **savings** = ``parcel_discount`` × piece count. Heavy-parcel rows use the same savings rule.

    Customer **name** is ``customers.customer_name`` for ``custom_account_code``, not billing ``account_name``.
    """
    pm_rates = get_priority_mail_retail_rates(conn)
    where_sql, params = _parcel_billing_filters(
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
    )
    cur = conn.execute(
        f"""
        SELECT br.custom_account_code, c.customer_name AS db_customer_name,
               br.zone, br.weight_oz, br.fully_paid_postage, br.billing_amount
        FROM billing_records br
        LEFT JOIN customers c ON br.custom_account_code = c.customer_number
        WHERE {where_sql}
        """,
        params,
    )

    grand_total_qty = 0
    heavy_groups: dict[tuple[Any, int, int], dict[str, Any]] = {}
    customers: dict[Any, dict[str, Any]] = {}

    parcel_discount = max(0.0, float(parcel_discount or 0.0))

    for row in cur:
        grand_total_qty += 1
        fp = float(row["fully_paid_postage"] or 0.0)
        acc = row["custom_account_code"]
        raw_nm = row["db_customer_name"]
        name = (raw_nm or "").strip() if raw_nm is not None else ""
        if not name:
            name = f"Account {acc}" if acc is not None else "(no name)"

        priced = compute_retail_cost_for_piece(
            conn,
            weight_oz=row["weight_oz"],
            zone_raw=row["zone"],
            fallback_retail=fp,
            pm_rates=pm_rates,
        )
        z = priced["zone"]
        lb = priced["lb"]
        retail = float(priced["retail"] or 0.0)
        savings_piece = (
            parcel_discount if retail >= parcel_discount else (retail if retail > 0 else 0.0)
        )
        discounted_piece = max(0.0, retail - savings_piece)

        ck = acc
        if ck not in customers:
            customers[ck] = {
                "customer_number": acc,
                "name": name if name else "(no name)",
                "qty": 0,
                "cost": 0.0,
                "discount": 0.0,
                "savings": 0.0,
            }
        customers[ck]["qty"] += 1
        customers[ck]["cost"] += retail
        customers[ck]["discount"] += discounted_piece
        customers[ck]["savings"] += parcel_discount if retail > 0 else 0.0

        if z is not None and lb is not None and 11 <= lb <= 100:
            gk = (acc, lb, z)
            if gk not in heavy_groups:
                heavy_groups[gk] = {
                    "count": 0,
                    "sum_retail": 0.0,
                    "sum_discount": 0.0,
                    "sum_savings": 0.0,
                    "customer_name": name,
                }
            g = heavy_groups[gk]
            g["count"] += 1
            g["sum_retail"] += retail
            g["sum_discount"] += discounted_piece
            g["sum_savings"] += savings_piece

    z_post = DEFAULT_PARCEL_ZONE_FOR_POSTAGE_OVER_13_OZ
    for pr in _postage_over_13_parcel_scope_rows(
        conn,
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
        consolidate=False,
    ):
        pieces = int(pr["pieces"] or 0)
        if pieces <= 0:
            continue
        woz = float(pr["weight_oz"] or 0.0)
        acc = pr["child_number"]
        raw_nm = pr["child_name"]
        name = (raw_nm or "").strip() if raw_nm is not None else ""
        if not name:
            name = f"Account {acc}" if acc is not None else "(no name)"
        fp = (float(pr["total_cost"] or 0.0) / pieces) if pieces else 0.0
        priced = compute_retail_cost_for_piece(
            conn,
            weight_oz=woz,
            zone_raw=z_post,
            fallback_retail=fp,
            pm_rates=pm_rates,
        )
        z = priced["zone"]
        lb = priced["lb"]
        retail = float(priced["retail"] or 0.0)
        savings_piece = (
            parcel_discount if retail >= parcel_discount else (retail if retail > 0 else 0.0)
        )
        discounted_piece = max(0.0, retail - savings_piece)

        ck = acc
        if ck not in customers:
            customers[ck] = {
                "customer_number": acc,
                "name": name if name else "(no name)",
                "qty": 0,
                "cost": 0.0,
                "discount": 0.0,
                "savings": 0.0,
            }
        customers[ck]["qty"] += pieces
        customers[ck]["cost"] += retail * pieces
        customers[ck]["discount"] += discounted_piece * pieces
        customers[ck]["savings"] += (parcel_discount if retail > 0 else 0.0) * pieces
        grand_total_qty += pieces

        if z is not None and lb is not None and 11 <= lb <= 100:
            gk = (acc, lb, z)
            if gk not in heavy_groups:
                heavy_groups[gk] = {
                    "count": 0,
                    "sum_retail": 0.0,
                    "sum_discount": 0.0,
                    "sum_savings": 0.0,
                    "customer_name": name,
                }
            g = heavy_groups[gk]
            g["count"] += pieces
            g["sum_retail"] += retail * pieces
            g["sum_discount"] += discounted_piece * pieces
            g["sum_savings"] += (parcel_discount if retail > 0 else 0.0) * pieces

    def _heavy_row_sort_key(gk: tuple[Any, int, int]) -> tuple:
        acc, lb, z = gk
        g = heavy_groups[gk]
        nm = (g.get("customer_name") or "").lower()
        try:
            an = int(acc) if acc is not None else 0
        except (TypeError, ValueError):
            an = 0
        return (nm, an, lb, z)

    heavy_rows: list[dict[str, Any]] = []
    for gk in sorted(heavy_groups.keys(), key=_heavy_row_sort_key):
        acc, lb, z = gk
        g = heavy_groups[gk]
        cnt = g["count"]
        if cnt <= 0:
            continue
        base = float(g["sum_retail"] or 0.0) / float(cnt)
        efd = float(g["sum_discount"] or 0.0) / float(cnt)
        savings = float(parcel_discount) * float(cnt)
        heavy_rows.append(
            {
                "customer_name": g.get("customer_name") or "(no name)",
                "customer_number": acc,
                "count": cnt,
                "lbs": lb,
                "zone": z,
                "base": round(base, 2),
                "efd": round(efd, 2),
                "savings": round(savings, 2),
            }
        )

    def _cust_sort_key(k: Any) -> tuple:
        if k is None:
            return (1, 0)
        try:
            return (0, int(k))
        except (TypeError, ValueError):
            return (0, 0)

    cust_rows: list[dict[str, Any]] = []
    for ck in sorted(customers.keys(), key=_cust_sort_key):
        c = customers[ck]
        q = int(c["qty"] or 0)
        cust_rows.append(
            {
                "customer_number": c["customer_number"],
                "name": c["name"],
                "qty": q,
                "cost": round(c["cost"], 2),
                "discount": round(float(c.get("discount") or 0.0), 2),
                "savings": round(float(parcel_discount) * q, 2),
            }
        )

    return {
        "grand_total_qty": grand_total_qty,
        "heavy_rows": heavy_rows,
        "customers": cust_rows,
    }


def query_parcel_over_10lb_lines(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    parent_number: int | None,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
) -> list[dict[str, Any]]:
    """One row per billing piece with integer lb > 10 (11–100); base = Priority retail (fallback: fully_paid_postage)."""
    pm_rates = get_priority_mail_retail_rates(conn)
    where_sql, params = _parcel_billing_filters(
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
    )
    cur = conn.execute(
        f"""
        SELECT br.custom_account_code, br.zone, br.weight_oz, br.fully_paid_postage,
               br.time_stamp, br.piece_id,
               COALESCE(NULLIF(TRIM(c.customer_name), ''), NULLIF(TRIM(br.account_name), ''), '(no name)')
                 AS child_name
        FROM billing_records br
        LEFT JOIN customers c ON br.custom_account_code = c.customer_number
        WHERE {where_sql}
        ORDER BY br.zone,
                 LOWER(COALESCE(NULLIF(TRIM(c.customer_name), ''), NULLIF(TRIM(br.account_name), ''), '(no name)')),
                 br.weight_oz, br.time_stamp, br.custom_account_code, br.piece_id
        """,
        params,
    )
    out: list[dict[str, Any]] = []
    for row in cur:
        lbs = parcel_weight_lb_int(row["weight_oz"])
        if lbs is None or lbs <= 10:
            continue
        fp = float(row["fully_paid_postage"] or 0.0)
        priced = compute_retail_cost_for_piece(
            conn,
            weight_oz=row["weight_oz"],
            zone_raw=row["zone"],
            fallback_retail=fp,
            pm_rates=pm_rates,
        )
        z = priced["zone"]
        if z is None:
            continue
        base = round(float(priced["retail"] or fp or 0.0), 2)
        acc = row["custom_account_code"]
        nm = (row["child_name"] or "").strip() or "(no name)"
        out.append(
            {
                "customer_number": int(acc) if acc is not None else None,
                "child_name": nm,
                "lbs": lbs,
                "zone": z,
                "base": base,
            }
        )

    z_post = DEFAULT_PARCEL_ZONE_FOR_POSTAGE_OVER_13_OZ
    for pr in _postage_over_13_parcel_scope_rows(
        conn,
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
        consolidate=False,
    ):
        pieces = int(pr["pieces"] or 0)
        if pieces <= 0:
            continue
        woz = float(pr["weight_oz"] or 0.0)
        lbs = parcel_weight_lb_int(woz)
        if lbs is None or lbs <= 10:
            continue
        fp = (float(pr["total_cost"] or 0.0) / pieces) if pieces else 0.0
        priced = compute_retail_cost_for_piece(
            conn,
            weight_oz=woz,
            zone_raw=z_post,
            fallback_retail=fp,
            pm_rates=pm_rates,
        )
        z = priced["zone"]
        if z is None:
            continue
        base = round(float(priced["retail"] or fp or 0.0), 2)
        acc = pr["child_number"]
        nm = (pr["child_name"] or "").strip() or "(no name)"
        for _ in range(pieces):
            out.append(
                {
                    "customer_number": int(acc) if acc is not None else None,
                    "child_name": nm,
                    "lbs": lbs,
                    "zone": z,
                    "base": base,
                }
            )
    return out


def parcel_summary_title_name(
    conn: sqlite3.Connection,
    parent_number: int | None,
    customer_number: int | None,
) -> str:
    if parent_number is not None:
        r = conn.execute(
            "SELECT customer_name FROM customers WHERE customer_number = ?",
            (parent_number,),
        ).fetchone()
        return (r["customer_name"] or "").strip() or f"Account {parent_number}"
    if customer_number is not None:
        r = conn.execute(
            "SELECT customer_name FROM customers WHERE customer_number = ?",
            (customer_number,),
        ).fetchone()
        return (r["customer_name"] or "").strip() or f"Account {customer_number}"
    return "All Accounts"


def _postage_over_13_parcel_scope_rows(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    parent_number: int | None,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
    *,
    consolidate: bool,
    kc_presort: bool = False,
    efd: bool = False,
) -> list[sqlite3.Row]:
    """Postage rows (BM / any import): weight over 13 oz, merged into parcel reporting."""
    _ = efd  # reserved for future reporting checks
    date_sel = "p.file_date" if not consolidate else "NULL"
    where_sql, params = postage_scope_where_clause(
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
    )
    kc_in = "(" + ",".join(f"'{c}'" for c in KC_PRESORT_METERED_MAIL_CLASSES_UPPER) + ")"
    kc_exclude_sql = (
        f" AND UPPER(p.mail_class) NOT IN {kc_in} " if kc_presort else ""
    )
    sql = f"""
    SELECT
        {date_sel} AS bill_date,
        COALESCE(c.parent_name, c.customer_name) AS parent_name,
        COALESCE(c.parent_number, c.customer_number) AS parent_number,
        c.customer_name AS child_name,
        c.customer_number AS child_number,
        p.mail_class,
        p.weight_oz,
        p.pieces,
        p.total_cost
    FROM postage_data p
    JOIN customers c ON p.account_code = c.customer_number
    WHERE {where_sql}
      AND p.weight_oz > 13
      {kc_exclude_sql}
    """
    cur = conn.execute(sql, params)
    return cur.fetchall()


def query_parcel_zone_summary(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    parent_number: int | None,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
    hide_costs: bool,
    kc_presort: bool = False,
    efd: bool = False,
    *,
    parcel_discount: float = 0.25,
) -> dict[str, Any]:
    """
    Zone × weight (1–10 lb) matrix. **Counts** come from billing_records (same scope as
    query_parcels) plus all postage_data rows over 13 oz at default zone 2. Unit prices come from
    `priority_mail_retail` (retail) for all weights in the matrix.

    **Costs** (customer spend) = Σ ((retail − parcel_discount) × quantity), floored at 0 per piece.
    **Savings** = parcel_discount × piece count (same scope as costs).
    """
    pm_rates = get_priority_mail_retail_rates(conn)
    parcel_discount = max(0.0, float(parcel_discount or 0.0))
    where_sql, params = _parcel_billing_filters(
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
    )
    cur = conn.execute(
        f"""
        SELECT br.zone, br.weight_oz, br.fully_paid_postage
        FROM billing_records br
        LEFT JOIN customers c ON br.custom_account_code = c.customer_number
        WHERE {where_sql}
        """,
        params,
    )

    agg: dict[tuple[int, int], int] = {}
    total_pieces = 0
    total_cost = 0.0
    total_savings = 0.0

    for row in cur:
        z = _normalize_parcel_zone(row["zone"])
        b = _parcel_lb_bucket(row["weight_oz"])
        if z is None or b is None:
            continue
        lb_int = int(b)
        if lb_int > 10:
            # Heavy parcels are excluded from the 1–10 lb zone matrix (they appear in the heavy block),
            # but they still contribute to footer totals so totals match invoice roll-ups.
            total_pieces += 1
            rt = _retail_price_for_zone_lb(pm_rates[0], pm_rates[1], zone=int(z), lb=int(lb_int))
            if rt is None:
                rt = float(row["fully_paid_postage"] or 0.0)
            rtf = float(rt)
            total_cost += max(0.0, rtf - parcel_discount)
            total_savings += parcel_discount
            continue
        total_pieces += 1
        lb_row = lb_int
        agg[(z, lb_row)] = agg.get((z, lb_row), 0) + 1
        rt = _retail_price_for_zone_lb(pm_rates[0], pm_rates[1], zone=int(z), lb=int(lb_row))
        if rt is None:
            # Fallback so totals don't silently drop pieces with missing rate cells.
            rt = float(row["fully_paid_postage"] or 0.0)
        rtf = float(rt)
        total_cost += max(0.0, rtf - parcel_discount)
        total_savings += parcel_discount

    z_conv = DEFAULT_PARCEL_ZONE_FOR_POSTAGE_OVER_13_OZ
    for pr in _postage_over_13_parcel_scope_rows(
        conn,
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
        consolidate=False,
        kc_presort=kc_presort,
        efd=efd,
    ):
        pieces = int(pr["pieces"] or 0)
        if pieces <= 0:
            continue
        woz = float(pr["weight_oz"] or 0.0)
        b = _parcel_lb_bucket(woz)
        if b is None:
            continue
        lb_int = int(b)
        if lb_int > 10:
            # Heavy parcels are excluded from the 1–10 lb zone matrix (they appear in the heavy block),
            # but they still contribute to footer totals so totals match invoice roll-ups.
            total_pieces += pieces
            rt = _retail_price_for_zone_lb(pm_rates[0], pm_rates[1], zone=int(z_conv), lb=int(lb_int))
            if rt is None:
                rt = (float(pr["total_cost"] or 0.0) / pieces) if pieces else 0.0
            rtf = float(rt)
            total_cost += max(0.0, rtf - parcel_discount) * pieces
            total_savings += parcel_discount * pieces
            continue
        lb_row = lb_int
        total_pieces += pieces
        rt = _retail_price_for_zone_lb(
            pm_rates[0], pm_rates[1], zone=int(z_conv), lb=int(lb_row)
        )
        if rt is None:
            rt = (float(pr["total_cost"] or 0.0) / pieces) if pieces else 0.0
        rtf = float(rt)
        total_cost += max(0.0, rtf - parcel_discount) * pieces
        total_savings += parcel_discount * pieces
        agg[(z_conv, lb_row)] = agg.get((z_conv, lb_row), 0) + pieces

    def cell(zone: int, lb: int) -> dict[str, Any]:
        c = agg.get((zone, lb), 0)
        rt = _retail_price_for_zone_lb(pm_rates[0], pm_rates[1], zone=int(zone), lb=int(lb))
        pri = round(float(rt), 2) if rt is not None else None
        ef = None if pri is None else round(max(0.0, float(pri) - parcel_discount), 2)
        if hide_costs:
            return {"count": c, "priority": None, "efd": None}
        return {"count": c, "priority": pri, "efd": ef}

    blocks_spec = [
        {"zone_a": 1, "zone_b": 3},
        {"zone_a": 2, "zone_b": 4},
        {"zone_a": 5, "zone_b": 6},
        {"zone_a": 7, "zone_b": 8},
        {"zone_a": 9, "zone_b": 9},
    ]
    blocks: list[dict[str, Any]] = []
    for spec in blocks_spec:
        za, zb = spec["zone_a"], spec["zone_b"]
        brow: list[dict[str, Any]] = []
        for lb in range(1, 11):
            ca = cell(za, lb)
            cb = cell(zb, lb)
            if hide_costs:
                row_costs = None
                row_savings = None
            elif ca["count"] == 0 and cb["count"] == 0:
                row_costs = None
                row_savings = None
            else:
                rc = 0.0
                piece_cnt = int(ca["count"] or 0) + int(cb["count"] or 0)
                for cnt, zcell in ((ca["count"], ca), (cb["count"], cb)):
                    if not cnt:
                        continue
                    efd = zcell.get("efd")
                    if efd is not None:
                        rc += float(efd) * int(cnt)
                row_costs = round(rc, 2)
                row_savings = round(float(parcel_discount) * piece_cnt, 2)
            brow.append(
                {
                    "weight_label": f"{lb} lb",
                    "zone_a": ca,
                    "zone_b": cb,
                    "costs": row_costs,
                    "savings": row_savings,
                }
            )
        blocks.append({"zone_a": za, "zone_b": zb, "rows": brow})

    title = parcel_summary_title_name(conn, parent_number, customer_number)
    try:
        report_date = datetime.strptime(end_date, "%Y-%m-%d").strftime("%d-%b-%Y")
    except ValueError:
        report_date = end_date

    out: dict[str, Any] = {
        "report_date": report_date,
        "title_name": title,
        "blocks": blocks,
        "total_pieces": total_pieces,
        "total_cost": round(total_cost, 2),
        "total_savings": round(total_savings, 2),
        "hide_costs": hide_costs,
    }
    if hide_costs:
        out["total_cost"] = None
        out["total_savings"] = None
    return out


def query_parcels(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    parent_number: int | None,
    customer_number: int | None,
    show_parents: bool,
    show_main: bool,
    consolidate: bool,
    remove_zeros: bool,
    hide_costs: bool,
    kc_presort: bool = False,
    efd: bool = False,
) -> dict[str, Any]:
    ts_date = _billing_ts_date_sql("br.time_stamp")
    where_sql, params = _parcel_billing_filters(
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
    )

    date_sel = f"{ts_date}" if not consolidate else "NULL"

    sql = f"""
    SELECT
        {date_sel} AS bill_date,
        CASE WHEN c.customer_number IS NULL THEN 'Unmatched'
             ELSE COALESCE(c.parent_name, c.customer_name) END AS parent_name,
        CASE WHEN c.customer_number IS NULL THEN NULL
             ELSE COALESCE(c.parent_number, c.customer_number) END AS parent_number,
        CASE WHEN c.customer_number IS NULL THEN COALESCE(NULLIF(TRIM(br.account_name), ''), '(no name)')
             ELSE c.customer_name END AS child_name,
        CASE WHEN c.customer_number IS NULL THEN br.custom_account_code
             ELSE c.customer_number END AS child_number,
        br.usps_mail_class AS mail_class,
        br.zone,
        br.weight_oz,
        br.billing_amount,
        br.fully_paid_postage
    FROM billing_records br
    LEFT JOIN customers c ON br.custom_account_code = c.customer_number
    WHERE {where_sql}
    """

    cur = conn.execute(sql, params)
    raw = cur.fetchall()

    pm_rates = get_priority_mail_retail_rates(conn)

    agg: dict[tuple[Any, ...], dict[str, Any]] = {}

    for r in raw:
        bucket = _parcel_lb_bucket(r["weight_oz"])
        if bucket is None:
            continue
        bd = r["bill_date"]
        key = (
            bd if not consolidate else "COMBINED",
            r["parent_name"],
            r["parent_number"],
            r["child_name"],
            r["child_number"],
            r["mail_class"],
            r["zone"] or "",
        )
        if key not in agg:
            agg[key] = {
                "date_key": bd,
                "parent_name": r["parent_name"],
                "parent_number": r["parent_number"],
                "child_name": r["child_name"],
                "child_number": r["child_number"],
                "mail_class": r["mail_class"],
                "zone": r["zone"] or "",
                "lb": {i: 0 for i in range(1, 11)},
                "lb_10plus": 0,
                "total_qty": 0,
                "total_billed": 0.0,
                "total_retail": 0.0,
            }
        a = agg[key]
        priced = compute_retail_cost_for_piece(
            conn,
            weight_oz=r["weight_oz"],
            zone_raw=r["zone"],
            fallback_retail=float(r["fully_paid_postage"] or 0.0),
            pm_rates=pm_rates,
        )
        retail = float(priced["retail"] or 0.0)
        a["total_qty"] += 1
        # Retail-only: treat computed retail as both billed + retail summary values
        # (keeps existing response shape stable for UI/exports).
        a["total_billed"] += retail
        a["total_retail"] += retail
        if bucket <= 10:
            a["lb"][bucket] += 1
        else:
            a["lb_10plus"] += 1

    zone_for_conv = DEFAULT_PARCEL_ZONE_FOR_POSTAGE_OVER_13_OZ
    for pr in _postage_over_13_parcel_scope_rows(
        conn,
        start_date,
        end_date,
        parent_number,
        customer_number,
        show_parents,
        show_main,
        consolidate=consolidate,
        kc_presort=kc_presort,
        efd=efd,
    ):
        pieces = int(pr["pieces"] or 0)
        if pieces <= 0:
            continue
        woz = float(pr["weight_oz"] or 0.0)
        bucket = _parcel_lb_bucket(woz)
        if bucket is None:
            continue
        bd = pr["bill_date"]
        mc = pr["mail_class"]
        mail_key = (mc if mc is not None else "") or ""
        key = (
            bd if not consolidate else "COMBINED",
            pr["parent_name"],
            pr["parent_number"],
            pr["child_name"],
            pr["child_number"],
            mail_key,
            str(zone_for_conv),
        )
        if key not in agg:
            agg[key] = {
                "date_key": bd,
                "parent_name": pr["parent_name"],
                "parent_number": pr["parent_number"],
                "child_name": pr["child_name"],
                "child_number": pr["child_number"],
                "mail_class": mail_key,
                "zone": str(zone_for_conv),
                "lb": {i: 0 for i in range(1, 11)},
                "lb_10plus": 0,
                "total_qty": 0,
                "total_billed": 0.0,
                "total_retail": 0.0,
            }
        a = agg[key]
        retail_one = _postage_over_13_per_piece_retail(
            conn,
            weight_oz=woz,
            pieces=pieces,
            total_cost=float(pr["total_cost"] or 0.0),
            pm_rates=pm_rates,
        )
        a["total_qty"] += pieces
        a["total_billed"] += retail_one * pieces
        a["total_retail"] += retail_one * pieces
        if bucket <= 10:
            a["lb"][bucket] += pieces
        else:
            a["lb_10plus"] += pieces

    rows: list[dict[str, Any]] = []
    total_pieces = 0
    total_billed = 0.0
    total_retail = 0.0

    for a in agg.values():
        lb_vals = {f"lb_{i}": a["lb"][i] for i in range(1, 11)}
        lb_vals["lb_10plus"] = a["lb_10plus"]

        if remove_zeros and a["total_qty"] == 0:
            continue
        if remove_zeros and all(lb_vals[k] == 0 for k in lb_vals):
            continue

        dk = a["date_key"]
        if consolidate or dk is None or dk == "COMBINED":
            date_str = "Combined"
        else:
            date_str = str(dk)

        item: dict[str, Any] = {
            "date": date_str,
            "parent_name": a["parent_name"],
            "parent_number": a["parent_number"],
            "child_name": a["child_name"],
            "child_number": a["child_number"],
            "mail_class": a["mail_class"],
            "zone": a["zone"],
            **lb_vals,
            "total_qty": a["total_qty"],
        }
        if not hide_costs:
            item["total_billed"] = round(a["total_billed"], 2)
            item["total_retail"] = round(a["total_retail"], 2)
        rows.append(item)
        total_pieces += a["total_qty"]
        total_billed += a["total_billed"]
        total_retail += a["total_retail"]

    rows.sort(key=lambda x: (x["date"], x["parent_name"] or "", x["child_name"] or "", x["mail_class"] or ""))

    out: dict[str, Any] = {
        "total_records": len(rows),
        "total_pieces": total_pieces,
        "total_billed": round(total_billed, 2),
        "total_retail": round(total_retail, 2),
        "rows": rows,
    }
    if hide_costs:
        out.pop("total_billed", None)
        out.pop("total_retail", None)
    return out


def query_summary(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    postage_customers: list[dict[str, Any]] = []
    cur = conn.execute(
        """
        SELECT c.customer_number, c.customer_name,
               COALESCE(SUM(p.pieces), 0) AS pieces,
               COALESCE(SUM(p.total_cost), 0) AS cost
        FROM customers c
        LEFT JOIN postage_data p ON p.account_code = c.customer_number
            AND p.file_date BETWEEN ? AND ?
            AND (p.weight_oz IS NULL OR p.weight_oz <= 13)
        GROUP BY c.customer_number, c.customer_name
        ORDER BY c.customer_name COLLATE NOCASE
        """,
        (start_date, end_date),
    )
    for r in cur.fetchall():
        postage_customers.append(
            {
                "customer_number": r["customer_number"],
                "customer_name": r["customer_name"],
                "pieces": int(r["pieces"] or 0),
                "cost": round(float(r["cost"] or 0), 2),
                "unmatched": False,
            }
        )

    cur = conn.execute(
        """
        SELECT p.account_code AS account_code,
               COALESCE(SUM(p.pieces), 0) AS pieces,
               COALESCE(SUM(p.total_cost), 0) AS cost
        FROM postage_data p
        WHERE p.file_date BETWEEN ? AND ?
          AND (p.weight_oz IS NULL OR p.weight_oz <= 13)
          AND NOT EXISTS (
              SELECT 1 FROM customers c WHERE c.customer_number = p.account_code
          )
        GROUP BY p.account_code
        ORDER BY p.account_code
        """,
        (start_date, end_date),
    )
    for r in cur.fetchall():
        pieces = int(r["pieces"] or 0)
        cost = round(float(r["cost"] or 0), 2)
        if pieces == 0 and cost == 0.0:
            continue
        postage_customers.append(
            {
                "customer_number": int(r["account_code"]),
                "customer_name": "Unmatched",
                "pieces": pieces,
                "cost": cost,
                "unmatched": True,
            }
        )

    postage_classes: list[dict[str, Any]] = []
    cur = conn.execute(
        """
        SELECT mail_class,
               SUM(pieces) AS pieces,
               SUM(total_cost) AS cost
        FROM postage_data
        WHERE file_date BETWEEN ? AND ?
          AND (weight_oz IS NULL OR weight_oz <= 13)
        GROUP BY mail_class
        ORDER BY pieces DESC
        """,
        (start_date, end_date),
    )
    for r in cur.fetchall():
        postage_classes.append(
            {
                "mail_class": r["mail_class"],
                "pieces": int(r["pieces"] or 0),
                "cost": round(float(r["cost"] or 0), 2),
            }
        )

    ts_date = _billing_ts_date_sql("br.time_stamp")
    parcel_customers: list[dict[str, Any]] = []
    cur = conn.execute(
        f"""
        SELECT c.customer_number, c.customer_name,
               COUNT(br.id) AS pieces,
               COALESCE(SUM(br.billing_amount), 0) AS billed
        FROM customers c
        LEFT JOIN billing_records br ON br.custom_account_code = c.customer_number
            AND {ts_date} BETWEEN ? AND ?
            AND br.weight_oz IS NOT NULL AND br.weight_oz > 0
        GROUP BY c.customer_number, c.customer_name
        ORDER BY c.customer_name COLLATE NOCASE
        """,
        (start_date, end_date),
    )
    for r in cur.fetchall():
        parcel_customers.append(
            {
                "customer_number": r["customer_number"],
                "customer_name": r["customer_name"],
                "pieces": int(r["pieces"] or 0),
                "cost": round(float(r["billed"] or 0), 2),
                "unmatched": False,
            }
        )

    cur = conn.execute(
        f"""
        SELECT br.custom_account_code AS account_code,
               COUNT(br.id) AS pieces,
               COALESCE(SUM(br.billing_amount), 0) AS billed
        FROM billing_records br
        WHERE {ts_date} BETWEEN ? AND ?
          AND br.weight_oz IS NOT NULL AND br.weight_oz > 0
          AND br.custom_account_code IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM customers c WHERE c.customer_number = br.custom_account_code
          )
        GROUP BY br.custom_account_code
        ORDER BY br.custom_account_code
        """,
        (start_date, end_date),
    )
    for r in cur.fetchall():
        pieces = int(r["pieces"] or 0)
        cost = round(float(r["billed"] or 0), 2)
        if pieces == 0 and cost == 0.0:
            continue
        parcel_customers.append(
            {
                "customer_number": int(r["account_code"]),
                "customer_name": "Unmatched",
                "pieces": pieces,
                "cost": cost,
                "unmatched": True,
            }
        )

    cur = conn.execute(
        f"""
        SELECT COUNT(br.id) AS pieces,
               COALESCE(SUM(br.billing_amount), 0) AS billed
        FROM billing_records br
        WHERE {ts_date} BETWEEN ? AND ?
          AND br.weight_oz IS NOT NULL AND br.weight_oz > 0
          AND br.custom_account_code IS NULL
        """,
        (start_date, end_date),
    )
    r_null = cur.fetchone()
    if r_null and int(r_null["pieces"] or 0) > 0:
        parcel_customers.append(
            {
                "customer_number": None,
                "customer_name": "Unmatched",
                "pieces": int(r_null["pieces"] or 0),
                "cost": round(float(r_null["billed"] or 0), 2),
                "unmatched": True,
            }
        )

    parcel_classes: list[dict[str, Any]] = []
    cur = conn.execute(
        f"""
        SELECT br.usps_mail_class AS mail_class,
               COUNT(*) AS pieces,
               COALESCE(SUM(br.billing_amount), 0) AS cost
        FROM billing_records br
        WHERE {ts_date} BETWEEN ? AND ?
          AND br.weight_oz IS NOT NULL AND br.weight_oz > 0
        GROUP BY br.usps_mail_class
        ORDER BY pieces DESC
        """,
        (start_date, end_date),
    )
    for r in cur.fetchall():
        parcel_classes.append(
            {
                "mail_class": r["mail_class"] or "",
                "pieces": int(r["pieces"] or 0),
                "cost": round(float(r["cost"] or 0), 2),
            }
        )

    imports: list[dict[str, Any]] = []
    cur = conn.execute(
        """
        SELECT file_name, file_date AS d, row_count, imported_at, 'Postage' AS type
        FROM postage_imports
        WHERE file_date BETWEEN ? AND ?
        UNION ALL
        SELECT file_name, NULL AS d, row_count, imported_at, 'Billing' AS type
        FROM billing_imports
        WHERE date(imported_at) BETWEEN ? AND ?
        ORDER BY imported_at DESC
        """,
        (start_date, end_date, start_date, end_date),
    )
    for r in cur.fetchall():
        imports.append(
            {
                "file_name": r["file_name"],
                "file_date": r["d"],
                "row_count": r["row_count"],
                "imported_at": r["imported_at"],
                "type": r["type"],
            }
        )

    post_pieces = sum(c["pieces"] for c in postage_customers)
    post_cost = sum(c["cost"] for c in postage_customers)
    par_pieces = sum(c["pieces"] for c in parcel_customers)
    par_cost = sum(c["cost"] for c in parcel_customers)

    return {
        "date_range": {"start": start_date, "end": end_date},
        "postage": {
            "total_pieces": post_pieces,
            "total_cost": round(post_cost, 2),
            "by_customer": postage_customers,
            "by_class": postage_classes,
        },
        "parcels": {
            "total_pieces": par_pieces,
            "total_billed": round(par_cost, 2),
            "by_customer": parcel_customers,
            "by_class": parcel_classes,
        },
        "imports": imports,
    }
