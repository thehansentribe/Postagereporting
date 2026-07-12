"""Poll input/ and watch/incoming/ for importable files."""

from __future__ import annotations

import re
import shutil
import time
import traceback
from datetime import date as date_cls
from pathlib import Path
from typing import Any

import db
import importer
import ws3_mail_detail

ROOT = Path(__file__).resolve().parent
INPUT_DIR = ROOT / "input"
WATCH_DIR = ROOT / "watch"
INCOMING = WATCH_DIR / "incoming"
PROCESSED = WATCH_DIR / "processed"
FAILED = WATCH_DIR / "failed"
# Copy of each file as picked up from input/incoming, before import (audit trail).
PROCESSED_INPUT_ARCHIVE = ROOT / "processed"
# Normalized BM *_report.csv files written here so input/ is not rescanned as raw BM.
REPORTS_DIR = ROOT / "reports"
LOG_FILE = WATCH_DIR / "watch.log"

_stop = False
_last_scan_ts: str | None = None


def ensure_dirs() -> None:
    for d in (INPUT_DIR, INCOMING, PROCESSED, FAILED, REPORTS_DIR, PROCESSED_INPUT_ARCHIVE):
        d.mkdir(parents=True, exist_ok=True)


def _log_line(msg: str) -> None:
    ensure_dirs()
    with open(LOG_FILE, "a", encoding="utf-8") as log:
        log.write(msg + "\n")


def _is_parent_customer_csv(name: str) -> bool:
    # e.g. Parent_Customer_.csv, Parent Customer .csv
    return bool(re.match(r"Parent[_ ]Customer.*\.csv$", name, re.IGNORECASE))


def _is_flat_rate_csv(name: str) -> bool:
    return bool(re.match(r"Flatscostdata.*\.csv$", name, re.IGNORECASE))


def _is_billing_csv(name: str) -> bool:
    low = name.lower()
    return "export_billing" in low or "billing" in low


def _is_pm_retail_csv(name: str) -> bool:
    low = name.lower()
    if not low.endswith(".csv"):
        return False
    # Prefer explicit names, but keep flexible for renamed files.
    return ("pm" in low and "retail" in low) or ("priority" in low and "retail" in low)


def _is_pm_retail_xlsx(name: str) -> bool:
    """Compact Priority Mail zone matrix (.xlsx), e.g. Priority mail zones.xlsx."""
    low = name.lower()
    if not low.endswith(".xlsx"):
        return False
    if "priority" not in low:
        return False
    return ("retail" in low) or ("zone" in low) or ("zones" in low)


def _is_ground_advantage_retail_csv(name: str) -> bool:
    low = name.lower()
    if not low.endswith(".csv"):
        return False
    return "ground" in low and "advantage" in low and "retail" in low


def _is_bm_report_csv(name: str) -> bool:
    """Normalized flat report: ends with _report.csv and BM prefix (underscore or space)."""
    low = name.lower()
    if not low.endswith("_report.csv"):
        return False
    return bool(re.search(r"BM[_\s]", name, re.IGNORECASE))


def _is_bm_raw_export(name: str) -> bool:
    """BM postage file not already normalized to *_report.csv (xls, xlsx, or raw csv)."""
    low = name.lower()
    is_bm_prefix = bool(re.search(r"BM[_\s]", name, re.IGNORECASE))
    is_dm_weight_break = low.startswith("dm weight break by account-carrier-class")
    if not (is_bm_prefix or is_dm_weight_break):
        return False
    if _is_bm_report_csv(name):
        return False
    suf = Path(name).suffix.lower()
    return suf in (".csv", ".xlsx", ".xls")


def _is_ws3_customer_mail_detail(name: str) -> bool:
    """NetSort WS3 FCFL Customer Mail Detail (xls/xlsx)."""
    low = name.lower().replace(" ", "")
    suf = Path(name).suffix.lower()
    if suf not in (".xls", ".xlsx"):
        return False
    # Anything after the prefix is ignored (e.g. WS3_FCFL_CustomerMailDetail(3).xls).
    return low.startswith("ws3_fcfl_customermaildetail")


def process_one_file(path: Path) -> dict:
    """Import a single file; raises on failure. Does not move file."""
    name = path.name
    db_path = db.DB_PATH

    if _is_parent_customer_csv(name):
        return importer.import_customers_csv(str(path), db_path)
    if _is_flat_rate_csv(name):
        return importer.import_flat_rate_costs(str(path), db_path)
    if _is_pm_retail_csv(name) or _is_pm_retail_xlsx(name):
        return importer.import_priority_mail_retail(str(path), db_path)
    if _is_ground_advantage_retail_csv(name):
        return importer.import_ground_advantage_retail(str(path), db_path)
    if _is_billing_csv(name) and name.lower().endswith(".csv"):
        return importer.import_billing_csv(str(path), db_path)
    if _is_bm_report_csv(name):
        return importer.import_bm_csv(str(path), db_path)
    if _is_bm_raw_export(name):
        ensure_dirs()
        return importer.process_bm_file(str(path), db_path, csv_out_dir=str(REPORTS_DIR))
    if _is_ws3_customer_mail_detail(name):
        ensure_dirs()
        return ws3_mail_detail.process_ws3_mail_detail_file(
            str(path), db_path, csv_out_path=str(REPORTS_DIR / "mail_detail_export.csv")
        )

    raise ValueError(f"Unrecognized file type: {name}")


def row_count_from_result(result: dict) -> int:
    return int(
        result.get("rows_imported")
        or result.get("row_count")
        or 0
    )


def scan_once() -> dict[str, Any]:
    """
    Process pending files in ``input/`` and ``watch/incoming/``.

    Returns ``{"processed": [{"file", "rows"}, ...], "failed": [{"file", "error"}, ...]}``.
    """
    global _last_scan_ts
    ensure_dirs()
    _last_scan_ts = time.strftime("%Y-%m-%d %H:%M:%S")

    processed: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []

    candidates: list[Path] = []
    seen: set[str] = set()
    for folder in (INPUT_DIR, INCOMING):
        if not folder.is_dir():
            continue
        for p in sorted(folder.iterdir()):
            if not p.is_file():
                continue
            key = str(p.resolve())
            if key in seen:
                continue
            seen.add(key)
            if p.name.startswith("."):
                continue
            candidates.append(p)

    if not candidates:
        return {"processed": processed, "failed": failed}

    ts = _last_scan_ts
    dest_day = PROCESSED / date_cls.today().isoformat()
    dest_day.mkdir(parents=True, exist_ok=True)

    for fpath in candidates:
        fts = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            archive_day = PROCESSED_INPUT_ARCHIVE / date_cls.today().isoformat()
            archive_day.mkdir(parents=True, exist_ok=True)
            arch_target = archive_day / fpath.name
            if arch_target.exists():
                stem, suf = arch_target.stem, arch_target.suffix
                arch_target = archive_day / f"{stem}_{int(time.time())}{suf}"
            shutil.copy2(str(fpath), str(arch_target))

            result = process_one_file(fpath)
            n = row_count_from_result(result)
            target = dest_day / fpath.name
            if target.exists():
                stem, suf = target.stem, target.suffix
                target = dest_day / f"{stem}_{int(time.time())}{suf}"
            shutil.move(str(fpath), str(target))
            _log_line(f"{fts} | OK | {fpath.name} | {n} rows")
            processed.append({"file": fpath.name, "rows": n})
        except Exception as e:
            try:
                shutil.move(str(fpath), str(FAILED / fpath.name))
            except OSError:
                pass
            err_log = FAILED / (fpath.name + ".log")
            with open(err_log, "w", encoding="utf-8") as el:
                el.write(f"Failed at {fts}\n{traceback.format_exc()}")
            _log_line(f"{fts} | FAIL | {fpath.name} | {e}")
            failed.append({"file": fpath.name, "error": str(e)})

    if processed:
        try:
            generate_ready_daily_reports()
        except Exception:
            fts = time.strftime("%Y-%m-%d %H:%M:%S")
            _log_line(f"{fts} | FAIL | daily_reports | {traceback.format_exc()}")

    return {"processed": processed, "failed": failed}


def generate_ready_daily_reports(window_days: int = 14) -> list[dict[str, Any]]:
    """Generate daily report sets for ready, incomplete business days in a trailing window.

    Bounded to the last ``window_days`` days and idempotent (skips complete folders).
    """
    import exports

    from datetime import timedelta

    today = date_cls.today()
    start = (today - timedelta(days=window_days)).isoformat()
    end = today.isoformat()

    conn = db.get_connection()
    try:
        readiness = db.query_report_readiness(conn, start, end)
    finally:
        conn.close()

    generated: list[dict[str, Any]] = []
    for item in readiness.get("daily_reports", []):
        if not item.get("date"):
            continue
        d = item["date"]
        if item.get("complete"):
            continue
        # Only ready days appear as candidates; skip days still missing sources.
        conn = db.get_connection()
        try:
            day_ready = db.query_report_readiness(conn, d, d).get("ready", False)
        finally:
            conn.close()
        if not day_ready:
            continue
        result = exports.save_daily_report_set(d)
        generated.append(result)
        _log_line(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} | DAILY | {d} | "
            f"saved {len(result['saved'])}, failed {len(result['failed'])}"
        )
    return generated


def watch_loop(interval_sec: int = 60) -> None:
    ensure_dirs()
    while not _stop:
        try:
            scan_once()
        except Exception:
            fts = time.strftime("%Y-%m-%d %H:%M:%S")
            _log_line(f"{fts} | FAIL | watch_loop | {traceback.format_exc()}")
        for _ in range(interval_sec):
            if _stop:
                break
            time.sleep(1)


def request_stop() -> None:
    global _stop
    _stop = True
