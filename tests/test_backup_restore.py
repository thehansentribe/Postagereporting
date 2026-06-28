"""Tests for backup_restore: create_backup, stage_restore, apply_pending_restore."""

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

import backup_restore
import db
import watcher


def _set_marker(path: Path, value: str) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS marker (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("INSERT OR REPLACE INTO marker (k, v) VALUES ('id', ?)", (value,))
    conn.commit()
    conn.close()


def _read_marker(path: Path) -> str:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute("SELECT v FROM marker WHERE k = 'id'").fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A throwaway app root with a DB, pricing CSVs, and data dirs, wired into
    backup_restore via monkeypatched db/watcher path constants."""
    root = tmp_path / "app"
    root.mkdir()

    db_path = root / "postage.db"
    _set_marker(db_path, "ORIGINAL")

    (root / "parcel summary.csv").write_text("zone,rate\n1,1.00\n", encoding="utf-8")
    (root / "heavy_parcel_rates.csv").write_text("zone,rate\n1,2.00\n", encoding="utf-8")

    reports = root / "reports"
    reports.mkdir()
    (reports / "mail_detail_export.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    postage_reports = root / "PostageReports"
    postage_reports.mkdir()
    (postage_reports / "invoice.xlsx").write_bytes(b"PK\x03\x04stub")

    watch = root / "watch"
    (watch / "processed").mkdir(parents=True)
    (watch / "processed" / "old.csv").write_text("x\n", encoding="utf-8")

    processed = root / "processed"
    processed.mkdir()
    (processed / "audit.csv").write_text("y\n", encoding="utf-8")

    inp = root / "input"
    inp.mkdir()
    (inp / ".gitkeep").write_text("", encoding="utf-8")

    monkeypatch.setattr(db, "ROOT", root)
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(db, "PARCEL_SUMMARY_RATES_CSV", root / "parcel summary.csv")
    monkeypatch.setattr(db, "HEAVY_PARCEL_RATES_CSV", root / "heavy_parcel_rates.csv")
    monkeypatch.setattr(watcher, "REPORTS_DIR", reports)
    monkeypatch.setattr(watcher, "WATCH_DIR", watch)
    monkeypatch.setattr(watcher, "PROCESSED_INPUT_ARCHIVE", processed)
    monkeypatch.setattr(watcher, "INPUT_DIR", inp)
    return root


# --- create_backup ---------------------------------------------------------


def test_create_backup_default_excludes_archives(env, tmp_path):
    out = tmp_path / "backup.zip"
    summary = backup_restore.create_backup(out, include_archives=False)

    assert out.is_file()
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read(backup_restore.MANIFEST_NAME))

    assert "postage.db" in names
    assert "parcel summary.csv" in names
    assert "heavy_parcel_rates.csv" in names
    assert "reports/mail_detail_export.csv" in names
    assert "PostageReports/invoice.xlsx" in names
    assert not any(n.startswith("watch/") for n in names)
    assert not any(n.startswith("processed/") for n in names)

    assert manifest["app"] == backup_restore.APP_MARKER
    assert manifest["include_archives"] is False
    assert summary["include_archives"] is False
    assert summary["bytes"] > 0


def test_create_backup_with_archives(env, tmp_path):
    out = tmp_path / "backup-full.zip"
    backup_restore.create_backup(out, include_archives=True)
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    assert "watch/processed/old.csv" in names
    assert "processed/audit.csv" in names
    assert "input/.gitkeep" in names


def test_backup_db_is_consistent_snapshot(env, tmp_path):
    out = tmp_path / "b.zip"
    backup_restore.create_backup(out, include_archives=False)
    extract = tmp_path / "x"
    extract.mkdir()
    with zipfile.ZipFile(out) as zf:
        zf.extract("postage.db", extract)
    assert _read_marker(extract / "postage.db") == "ORIGINAL"


# --- validation ------------------------------------------------------------


def test_validate_rejects_plain_zip(env, tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("hello.txt", "not a backup")
    res = backup_restore.validate_backup_zip(bad)
    assert res["ok"] is False
    assert "valid Postage Reporting backup" in res["error"]


def test_validate_rejects_non_zip(env, tmp_path):
    notzip = tmp_path / "x.zip"
    notzip.write_text("definitely not a zip", encoding="utf-8")
    res = backup_restore.validate_backup_zip(notzip)
    assert res["ok"] is False


# --- stage / apply ---------------------------------------------------------


def test_stage_restore_does_not_touch_live_data(env, tmp_path):
    out = tmp_path / "src.zip"
    backup_restore.create_backup(out, include_archives=True)  # snapshot of ORIGINAL
    _set_marker(env / "postage.db", "MUTATED")

    res = backup_restore.stage_restore(out)
    assert res["ok"] is True
    assert res["restart_required"] is True
    # Live DB unchanged until apply runs at next startup.
    assert _read_marker(env / "postage.db") == "MUTATED"
    assert (env / ".restore_pending").exists()
    assert (env / ".restore_staging").is_dir()


def test_stage_restore_rejects_invalid_zip(env, tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("hello.txt", "nope")
    res = backup_restore.stage_restore(bad)
    assert res["ok"] is False
    assert not (env / ".restore_pending").exists()


def test_apply_pending_restore_swaps_db_and_snapshots(env, tmp_path):
    out = tmp_path / "src.zip"
    backup_restore.create_backup(out, include_archives=True)  # snapshot of ORIGINAL
    _set_marker(env / "postage.db", "MUTATED")
    backup_restore.stage_restore(out)

    summary = backup_restore.apply_pending_restore()
    assert summary is not None
    assert "postage.db" in summary["applied"]

    # DB restored to ORIGINAL.
    assert _read_marker(env / "postage.db") == "ORIGINAL"
    # Pre-restore snapshot of the MUTATED DB kept for rollback.
    snaps = list(env.glob("postage.db.bak-restore-*"))
    assert snaps, "expected a pre-restore snapshot"
    assert _read_marker(snaps[0]) == "MUTATED"
    # Working files cleaned up.
    assert not (env / ".restore_staging").exists()
    assert not (env / ".restore_pending").exists()


def test_apply_restores_directories(env, tmp_path):
    out = tmp_path / "src.zip"
    backup_restore.create_backup(out, include_archives=True)
    (env / "reports" / "mail_detail_export.csv").unlink()  # wipe to prove restore
    backup_restore.stage_restore(out)
    backup_restore.apply_pending_restore()
    assert (env / "reports" / "mail_detail_export.csv").read_text() == "a,b\n1,2\n"
    assert (env / "processed" / "audit.csv").exists()


def test_apply_pending_restore_noop_when_nothing_pending(env):
    assert backup_restore.apply_pending_restore() is None
