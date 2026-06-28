"""Backup and restore of the database and data directories.

Backup builds a ``.zip`` containing a consistent SQLite snapshot (via the online
backup API, so WAL changes are checkpointed) plus the report/data directories.

Restore validates an uploaded ``.zip``, extracts it into a staging folder, and
marks it pending. The pending restore is applied on the next startup by
``apply_pending_restore()`` — *before* any DB connection is opened. Staging the
restore this way is safe on Windows, which cannot replace ``postage.db`` while a
process holds it open.

Paths are read from :mod:`db` and :mod:`watcher` at call time (never redefined),
so the same constants the app uses are the ones backed up and restored.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import watcher

APP_MARKER = "postage-reporting"
MANIFEST_NAME = "backup_manifest.json"
BACKUP_FORMAT_VERSION = 1

# Top-level entries restore is allowed to write back under db.ROOT. Anything else
# in an archive is ignored, so a backup can never scatter files outside these.
_RESTORE_WHITELIST = {
    "postage.db",
    "parcel summary.csv",
    "heavy_parcel_rates.csv",
    "reports",
    "PostageReports",
    "watch",
    "processed",
    "input",
}

# Transient files that must never be captured or left behind for postage.db.
_DB_SIDECARS = ("postage.db-wal", "postage.db-shm")


def _staging_dir() -> Path:
    return db.ROOT / ".restore_staging"


def _pending_marker() -> Path:
    return db.ROOT / ".restore_pending"


def _always_files() -> list[tuple[str, Path]]:
    """Single files always included (postage.db is handled separately)."""
    return [
        ("parcel summary.csv", db.PARCEL_SUMMARY_RATES_CSV),
        ("heavy_parcel_rates.csv", db.HEAVY_PARCEL_RATES_CSV),
    ]


def _always_dirs() -> list[tuple[str, Path]]:
    """Generated-output directories always included (modest size)."""
    return [
        ("reports", watcher.REPORTS_DIR),
        ("PostageReports", db.ROOT / "PostageReports"),
    ]


def _archive_dirs() -> list[tuple[str, Path]]:
    """Large audit archives, included only when ``include_archives`` is set."""
    return [
        ("watch", watcher.WATCH_DIR),
        ("processed", watcher.PROCESSED_INPUT_ARCHIVE),
        ("input", watcher.INPUT_DIR),
    ]


# ----------------------------------------------------------------------------
# Backup
# ----------------------------------------------------------------------------


def _snapshot_db(dest: Path) -> None:
    """Write a consistent snapshot of postage.db to ``dest`` (WAL-safe)."""
    src = sqlite3.connect(str(db.DB_PATH))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _add_dir_to_zip(zf: zipfile.ZipFile, arc_root: str, src_dir: Path) -> int:
    if not src_dir.is_dir():
        return 0
    count = 0
    for p in sorted(src_dir.rglob("*")):
        if not p.is_file():
            continue
        if p.name in _DB_SIDECARS or p.name == ".DS_Store":
            continue
        rel = p.relative_to(src_dir).as_posix()
        zf.write(p, f"{arc_root}/{rel}")
        count += 1
    return count


def create_backup(out_path: str | Path, *, include_archives: bool = False) -> dict[str, Any]:
    """Build a backup zip at ``out_path``. Returns a summary dict."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    included: list[str] = []
    file_count = 0

    # SQLite online backup needs a real file path; use a sibling of the zip.
    snap = out_path.parent / (out_path.name + ".dbsnap.tmp")
    try:
        _snapshot_db(snap)

        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(snap, "postage.db")
            included.append("postage.db")
            file_count += 1

            for arc, path in _always_files():
                if Path(path).is_file():
                    zf.write(path, arc)
                    included.append(arc)
                    file_count += 1

            dirs = _always_dirs()
            if include_archives:
                dirs = dirs + _archive_dirs()
            for arc, path in dirs:
                n = _add_dir_to_zip(zf, arc, Path(path))
                if n:
                    included.append(f"{arc}/")
                    file_count += n

            manifest = {
                "app": APP_MARKER,
                "format_version": BACKUP_FORMAT_VERSION,
                "created": datetime.now().isoformat(timespec="seconds"),
                "include_archives": include_archives,
                "contains": included,
            }
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
    finally:
        snap.unlink(missing_ok=True)

    return {
        "path": str(out_path),
        "files": file_count,
        "bytes": out_path.stat().st_size,
        "include_archives": include_archives,
        "contains": included,
    }


# ----------------------------------------------------------------------------
# Restore (staged; applied on next startup)
# ----------------------------------------------------------------------------


def _read_manifest(zf: zipfile.ZipFile) -> dict[str, Any] | None:
    try:
        with zf.open(MANIFEST_NAME) as f:
            return json.loads(f.read().decode("utf-8"))
    except KeyError:
        return None
    except (ValueError, OSError):
        return None


def validate_backup_zip(zip_path: str | Path) -> dict[str, Any]:
    """Check the zip is one of our backups. Modifies nothing.

    Returns ``{"ok": True, "manifest": {...}}`` or ``{"ok": False, "error": ...}``.
    """
    zip_path = Path(zip_path)
    if not zipfile.is_zipfile(zip_path):
        return {"ok": False, "error": "Uploaded file is not a valid .zip archive."}
    with zipfile.ZipFile(zip_path) as zf:
        manifest = _read_manifest(zf)
        if not manifest or manifest.get("app") != APP_MARKER:
            return {
                "ok": False,
                "error": "Not a valid Postage Reporting backup (manifest missing).",
            }
        if "postage.db" not in set(zf.namelist()):
            return {"ok": False, "error": "Backup does not contain postage.db."}
    return {"ok": True, "manifest": manifest}


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract whitelisted members under ``dest``, guarding against path traversal."""
    dest = dest.resolve()
    for member in zf.infolist():
        name = member.filename
        if not name or name.endswith("/"):
            continue
        parts = Path(name).parts
        if not parts:
            continue
        top = parts[0]
        if top != MANIFEST_NAME and top not in _RESTORE_WHITELIST:
            continue
        target = (dest / name).resolve()
        if target != dest and not str(target).startswith(str(dest) + os.sep):
            raise ValueError(f"Unsafe path in archive: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def stage_restore(zip_path: str | Path) -> dict[str, Any]:
    """Validate and stage a restore. Live data is untouched until the next start.

    Returns ``{"ok": True, "restart_required": True, ...}`` or ``{"ok": False, "error": ...}``.
    """
    v = validate_backup_zip(zip_path)
    if not v.get("ok"):
        return v

    staging = _staging_dir()
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        _safe_extract(zf, staging)

    manifest = v.get("manifest") or {}
    _pending_marker().write_text(
        datetime.now().isoformat(timespec="seconds"), encoding="utf-8"
    )
    return {
        "ok": True,
        "restart_required": True,
        "created": manifest.get("created"),
        "include_archives": bool(manifest.get("include_archives")),
        "staged": manifest.get("contains", []),
    }


def apply_pending_restore() -> dict[str, Any] | None:
    """Apply a staged restore, if any. Call once at startup before opening the DB.

    Snapshots the current database first (reversible), then replaces each staged
    entry under ``db.ROOT``. Returns a summary, or ``None`` if nothing was pending.
    """
    pending = _pending_marker()
    staging = _staging_dir()
    if not pending.exists():
        return None
    if not staging.is_dir():
        pending.unlink(missing_ok=True)
        return None

    # 1) Reversible snapshot of the current database.
    if db.DB_PATH.is_file():
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(db.DB_PATH, db.ROOT / f"postage.db.bak-restore-{ts}")
        except OSError:
            pass

    applied: list[str] = []
    for entry in sorted(staging.iterdir()):
        name = entry.name
        if name == MANIFEST_NAME or name not in _RESTORE_WHITELIST:
            continue
        target = db.ROOT / name
        if entry.is_dir():
            old = db.ROOT / f"{name}.pre-restore"
            if old.exists():
                shutil.rmtree(old, ignore_errors=True)
            if target.exists():
                target.rename(old)
            shutil.move(str(entry), str(target))
            if old.exists():
                shutil.rmtree(old, ignore_errors=True)
        else:
            if name == "postage.db":
                # Drop stale WAL/SHM so the restored DB is read cleanly.
                for sidecar in _DB_SIDECARS:
                    (db.ROOT / sidecar).unlink(missing_ok=True)
            os.replace(str(entry), str(target))
        applied.append(name)

    shutil.rmtree(staging, ignore_errors=True)
    pending.unlink(missing_ok=True)
    return {"applied": applied}
