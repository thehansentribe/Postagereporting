"""Background scheduler for scheduled report emails."""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import traceback
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import db
import email_service
import scheduler_db
from scheduler_tokens import (
    file_check,
    format_file_list,
    now_in_timezone,
    resolve_patterns,
    resolve_tokens,
    today_in_timezone,
)

_log = logging.getLogger(__name__)

_stop = False
_started = False
_start_lock = threading.Lock()
_cycle_lock = threading.Lock()
_holder_id = f"scheduler-{uuid.uuid4().hex[:8]}"
_last_cleanup_day: date | None = None

# In-memory retry state: job_id -> {attempts, next_retry_at}
_retry_state: dict[int, dict[str, Any]] = {}


def request_stop() -> None:
    global _stop
    _stop = True


def ensure_scheduler(interval_sec: int | None = None) -> None:
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
        t = threading.Thread(target=scheduler_loop, kwargs={"interval_sec": interval_sec}, daemon=True)
        t.start()


def _load_settings(conn) -> dict[str, Any]:
    return db.get_scheduler_settings(conn)


def _parse_scheduled_time(scheduled_time: str | None) -> tuple[int, int] | None:
    if not scheduled_time:
        return None
    parts = str(scheduled_time).strip().split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _in_effective_range(job: dict[str, Any], today: date) -> bool:
    iso = today.isoformat()
    start = job.get("effective_start_date")
    end = job.get("effective_end_date")
    if start and iso < str(start):
        return False
    if end and iso > str(end):
        return False
    return True


def _schedule_applies_today(job: dict[str, Any], today: date) -> bool:
    st = job.get("schedule_type")
    if st == "data-only":
        return True
    if st == "daily":
        return True
    if st == "weekly":
        dow = today.strftime("%a")[:3]
        days = [d.strip() for d in (job.get("days_of_week_csv") or "").split(",") if d.strip()]
        return dow in days or today.strftime("%a") in days
    if st == "monthly":
        dom = job.get("day_of_month")
        if dom is None:
            return False
        return today.day == int(dom)
    if st == "one-time":
        ot = job.get("one_time_at")
        if not ot:
            return False
        return str(ot)[:10] == today.isoformat()
    return False


def _time_gate_passed(job: dict[str, Any], now: datetime) -> bool:
    if job.get("schedule_type") == "data-only":
        return True
    hm = _parse_scheduled_time(job.get("scheduled_time"))
    if hm is None:
        return True
    h, m = hm
    return (now.hour, now.minute) >= (h, m)


def _expiration_deadline(job: dict[str, Any], today: date, settings: dict[str, Any]) -> datetime | None:
    if job.get("schedule_type") == "data-only":
        return None
    hm = _parse_scheduled_time(job.get("scheduled_time"))
    if hm is None:
        return None
    hours = job.get("expiration_hours")
    if hours is None:
        hours = settings.get(db.SETTING_DEFAULT_EXPIRATION_HOURS, db.DEFAULT_DEFAULT_EXPIRATION_HOURS)
    try:
        exp_h = float(hours)
    except (TypeError, ValueError):
        exp_h = float(db.DEFAULT_DEFAULT_EXPIRATION_HOURS)
    h, m = hm
    now_tz = now_in_timezone(str(settings.get(db.SETTING_TIMEZONE, db.DEFAULT_TIMEZONE)))
    start = datetime(today.year, today.month, today.day, h, m, tzinfo=now_tz.tzinfo)
    return start + timedelta(hours=exp_h)


def _folder_files(folder: str) -> list[str]:
    """Top-level, non-empty files in a watched report folder (subfolders skipped)."""
    p = Path(folder)
    if not p.is_dir():
        return []
    out: list[str] = []
    try:
        for entry in os.scandir(p):
            try:
                if entry.is_file() and entry.stat().st_size > 0:
                    out.append(entry.path)
            except OSError:
                continue
    except OSError:
        return []
    out.sort(key=lambda s: s.lower())
    return out


def _archive_sent_files(
    folder: str, files: list[str], archive_subdir: str, fire_date: str
) -> None:
    """Move already-sent files into <folder>/<archive_subdir>/<fire_date>/."""
    dest_dir = Path(folder) / (archive_subdir or "Sent") / fire_date
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _log.error("Could not create archive dir %s: %s", dest_dir, e)
        return
    for f in files:
        src = Path(f)
        if not src.exists():
            continue
        dest = dest_dir / src.name
        if dest.exists():
            dest = dest_dir / f"{dest.stem}_{int(time.time() * 1000)}{dest.suffix}"
        try:
            shutil.move(str(src), str(dest))
        except OSError as e:
            _log.error("Could not archive %s: %s", src, e)


def evaluate_data_readiness(
    job: dict[str, Any], today: date, settings: dict[str, Any]
) -> dict[str, Any]:
    folder = str(job.get("attachment_folder") or "").strip()
    if folder:
        files = _folder_files(folder)
        stale = job.get("stale_file_threshold_minutes")
        checks = [file_check(f, stale) for f in files]
        ready = len(files) > 0
        return {
            "ready": ready,
            "resolved": files,
            "checks": checks,
            "missing": [] if ready else [folder],
            "present": files,
            "folder_mode": True,
        }
    patterns = [f["file_path_pattern"] for f in job.get("required_files") or []]
    if not patterns:
        return {"ready": True, "resolved": [], "missing": [], "present": []}
    resolved = resolve_patterns(patterns, today, job_name=job.get("name") or "")
    stale = job.get("stale_file_threshold_minutes")
    checks = [file_check(p, stale) for p in resolved]
    present = [c for c in checks if c["present"]]
    missing = [c for c in checks if not c["present"]]
    mode = job.get("data_readiness_mode") or "all_required"
    if mode == "any_required":
        ready = len(present) > 0
    else:
        ready = len(missing) == 0
    return {
        "ready": ready,
        "resolved": resolved,
        "checks": checks,
        "missing": [c["path"] for c in missing],
        "present": [c["path"] for c in present],
    }


def execute_job_send(
    conn,
    job: dict[str, Any],
    settings: dict[str, Any],
    *,
    fire_date: str | None = None,
    manual: bool = False,
) -> dict[str, Any]:
    tz_name = str(settings.get(db.SETTING_TIMEZONE, db.DEFAULT_TIMEZONE))
    today = today_in_timezone(tz_name)
    fdate = fire_date or today.isoformat()
    data = evaluate_data_readiness(job, today, settings)
    resolved_files = data.get("resolved") or []
    file_list = format_file_list(resolved_files)
    subject = resolve_tokens(
        job["subject_template"], today, job_name=job["name"], file_list=file_list
    )
    body = resolve_tokens(
        job["body_template"], today, job_name=job["name"], file_list=file_list
    )
    folder = str(job.get("attachment_folder") or "").strip()
    if folder:
        attachments = data.get("resolved") or []
    else:
        attach_patterns = [a["file_path_pattern"] for a in job.get("attachments") or []]
        attachments = resolve_patterns(attach_patterns, today, job_name=job["name"])
    recipients = scheduler_db.expand_job_recipients(conn, job)
    if not recipients:
        return {"success": False, "error": "No recipients configured"}

    if not manual and (job.get("required_files") or folder) and not data["ready"]:
        return {"success": False, "error": "Required files not ready", "waiting": True}
    if folder and not attachments:
        return {"success": False, "error": "No files in folder", "waiting": True}

    for ap in attachments:
        chk = file_check(ap, job.get("stale_file_threshold_minutes"))
        if not chk["present"]:
            return {
                "success": False,
                "error": f"Attachment missing or stale: {ap}",
            }

    email_root = str(settings.get(db.SETTING_EMAIL_ROOT_PATH, db.DEFAULT_EMAIL_ROOT_PATH))
    result = email_service.send_dict(
        {
            "subject": subject,
            "body": body,
            "recipients": recipients,
            "attachments": attachments,
            "emailRootPath": email_root,
            "timezone": tz_name,
        }
    )
    if result.get("success"):
        if folder and (job.get("post_send_action") or "archive") == "archive":
            _archive_sent_files(
                folder, attachments, job.get("archive_subdir") or "Sent", fdate
            )
        scheduler_db.mark_job_fired(
            conn, int(job["id"]), fdate, result.get("baseName"), "success"
        )
        scheduler_db.insert_execution_log(
            conn,
            job_id=int(job["id"]),
            job_name=job["name"],
            status="success",
            base_name=result.get("baseName"),
            recipient_count=len(recipients),
            resolved_recipients=recipients,
            resolved_files=resolved_files + attachments,
            details={"subject": subject, "body": body, "manual": manual},
        )
    else:
        scheduler_db.insert_execution_log(
            conn,
            job_id=int(job["id"]),
            job_name=job["name"],
            status="failed",
            base_name=result.get("baseName"),
            recipient_count=len(recipients),
            resolved_recipients=recipients,
            resolved_files=resolved_files,
            error_message=result.get("error"),
            details={"manual": manual},
        )
    return result


def _send_admin_alert(conn, settings: dict[str, Any], subject: str, body: str) -> None:
    admin = str(settings.get(db.SETTING_ADMIN_NOTIFICATION_EMAIL, "")).strip()
    if not admin:
        return
    email_service.send_dict(
        {
            "subject": subject,
            "body": body,
            "recipients": [admin],
            "emailRootPath": str(
                settings.get(db.SETTING_EMAIL_ROOT_PATH, db.DEFAULT_EMAIL_ROOT_PATH)
            ),
            "timezone": str(settings.get(db.SETTING_TIMEZONE, db.DEFAULT_TIMEZONE)),
        }
    )


def run_cycle() -> dict[str, Any]:
    """Single scheduler evaluation pass."""
    summary: dict[str, Any] = {"evaluated": 0, "sent": 0, "skipped": 0, "errors": 0}
    if not _cycle_lock.acquire(blocking=False):
        summary["skipped_lock"] = True
        return summary

    conn = db.get_connection()
    try:
        if not scheduler_db.try_acquire_scheduler_lock(conn, _holder_id):
            summary["skipped_lock"] = True
            conn.close()
            return summary
        conn.commit()

        settings = _load_settings(conn)
        tz_name = str(settings.get(db.SETTING_TIMEZONE, db.DEFAULT_TIMEZONE))
        now = now_in_timezone(tz_name)
        today = now.date()
        today_iso = today.isoformat()

        global _last_cleanup_day
        retention = int(settings.get(db.SETTING_LOG_RETENTION_DAYS, db.DEFAULT_LOG_RETENTION_DAYS))
        if _last_cleanup_day != today:
            scheduler_db.purge_old_scheduler_logs(conn, retention)
            conn.commit()
            _last_cleanup_day = today

        jobs = scheduler_db.list_jobs(conn, enabled_only=True)
        for job in jobs:
            summary["evaluated"] += 1
            jid = int(job["id"])
            if not _in_effective_range(job, today):
                summary["skipped"] += 1
                continue
            if not _schedule_applies_today(job, today):
                summary["skipped"] += 1
                continue

            if scheduler_db.job_fired_on_date(conn, jid, today_iso):
                summary["skipped"] += 1
                continue

            if not _time_gate_passed(job, now):
                summary["skipped"] += 1
                continue

            deadline = _expiration_deadline(job, today, settings)
            if deadline and now > deadline:
                if not scheduler_db.job_expired_on_date(conn, jid, today_iso):
                    scheduler_db.mark_job_fired(conn, jid, today_iso, None, "expired")
                    scheduler_db.insert_execution_log(
                        conn,
                        job_id=jid,
                        job_name=job["name"],
                        status="expired",
                        error_message="Expiration window passed without send",
                    )
                    if job.get("send_failure_notification"):
                        _send_admin_alert(
                            conn,
                            settings,
                            f"Job expired: {job['name']}",
                            f"Scheduled job '{job['name']}' expired on {today_iso} without sending.",
                        )
                summary["skipped"] += 1
                continue

            data = evaluate_data_readiness(job, today, settings)
            if not data["ready"]:
                scheduler_db.insert_execution_log(
                    conn,
                    job_id=jid,
                    job_name=job["name"],
                    status="waiting",
                    resolved_files=data.get("resolved"),
                    error_message="waiting: " + ", ".join(data.get("missing") or []),
                    details={"missing": data.get("missing"), "present": data.get("present")},
                )
                summary["skipped"] += 1
                continue

            retry = _retry_state.get(jid)
            if retry and retry.get("next_retry_at"):
                if datetime.now(timezone.utc) < retry["next_retry_at"]:
                    summary["skipped"] += 1
                    continue

            result = execute_job_send(conn, job, settings, fire_date=today_iso)
            if result.get("success"):
                summary["sent"] += 1
                _retry_state.pop(jid, None)
            else:
                summary["errors"] += 1
                attempts = (_retry_state.get(jid) or {}).get("attempts", 0) + 1
                max_retry = int(job.get("retry_count") or 0)
                delay = int(job.get("retry_delay_seconds") or 60)
                if attempts <= max_retry:
                    _retry_state[jid] = {
                        "attempts": attempts,
                        "next_retry_at": datetime.now(timezone.utc) + timedelta(seconds=delay),
                    }
                else:
                    _retry_state.pop(jid, None)
                    if job.get("send_failure_notification"):
                        _send_admin_alert(
                            conn,
                            settings,
                            f"Job failed: {job['name']}",
                            result.get("error") or "Unknown error",
                        )

        conn.commit()
    except Exception:
        _log.error("Scheduler cycle failed: %s", traceback.format_exc())
        summary["errors"] += 1
    finally:
        try:
            scheduler_db.release_scheduler_lock(conn, _holder_id)
            conn.commit()
        except Exception:
            pass
        conn.close()
        _cycle_lock.release()

    return summary


def scheduler_loop(interval_sec: int | None = None) -> None:
    while not _stop:
        conn = db.get_connection()
        try:
            settings = _load_settings(conn)
            interval = interval_sec
            if interval is None:
                interval = int(
                    settings.get(
                        db.SETTING_POLLING_INTERVAL_SECONDS,
                        db.DEFAULT_POLLING_INTERVAL_SECONDS,
                    )
                )
        finally:
            conn.close()

        try:
            run_cycle()
        except Exception:
            _log.error("Scheduler loop error: %s", traceback.format_exc())

        for _ in range(max(1, int(interval))):
            if _stop:
                break
            time.sleep(1)


def preview_job(conn, job: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    tz_name = str(settings.get(db.SETTING_TIMEZONE, db.DEFAULT_TIMEZONE))
    today = today_in_timezone(tz_name)
    data = evaluate_data_readiness(job, today, settings)
    resolved_files = data.get("resolved") or []
    file_list = format_file_list(resolved_files)
    subject = resolve_tokens(
        job["subject_template"], today, job_name=job["name"], file_list=file_list
    )
    body = resolve_tokens(
        job["body_template"], today, job_name=job["name"], file_list=file_list
    )
    folder = str(job.get("attachment_folder") or "").strip()
    if folder:
        attachments = data.get("resolved") or []
    else:
        attach_patterns = [a["file_path_pattern"] for a in job.get("attachments") or []]
        attachments = resolve_patterns(attach_patterns, today, job_name=job["name"])
    attach_checks = [
        file_check(p, job.get("stale_file_threshold_minutes")) for p in attachments
    ]
    recipients = scheduler_db.expand_job_recipients(conn, job)
    return {
        "subject": subject,
        "body": body,
        "recipients": recipients,
        "recipient_count": len(recipients),
        "required_files": data.get("checks"),
        "attachments": attach_checks,
        "resolved_required_paths": resolved_files,
        "resolved_attachment_paths": attachments,
    }


def dashboard_summary(conn, settings: dict[str, Any]) -> dict[str, Any]:
    tz_name = str(settings.get(db.SETTING_TIMEZONE, db.DEFAULT_TIMEZONE))
    today_iso = today_in_timezone(tz_name).isoformat()
    jobs = scheduler_db.list_jobs(conn)
    active = sum(1 for j in jobs if j.get("enabled"))
    logs_today = scheduler_db.list_execution_log(
        conn, start_date=today_iso, end_date=today_iso, limit=500
    )
    sent_today = sum(1 for e in logs_today if e["status"] == "success")
    failed_waiting = sum(
        1 for e in logs_today if e["status"] in ("failed", "waiting", "expired")
    )
    timeline = _build_timeline(conn, jobs, today_iso, settings)
    pending = sum(1 for t in timeline if t.get("status") == "pending")
    return {
        "total_active_jobs": active,
        "pending_today": pending,
        "completed_today": sent_today,
        "failed_waiting_today": failed_waiting,
        "timeline": timeline,
    }


def _build_timeline(
    conn, jobs: list[dict[str, Any]], today_iso: str, settings: dict[str, Any]
) -> list[dict[str, Any]]:
    tz_name = str(settings.get(db.SETTING_TIMEZONE, db.DEFAULT_TIMEZONE))
    now = now_in_timezone(tz_name)
    today = date.fromisoformat(today_iso)
    out: list[dict[str, Any]] = []
    for job in jobs:
        if not job.get("enabled"):
            continue
        if not _in_effective_range(job, today):
            continue
        if not _schedule_applies_today(job, today):
            continue
        jid = int(job["id"])
        item: dict[str, Any] = {
            "job_id": jid,
            "job_name": job["name"],
            "schedule_type": job.get("schedule_type"),
            "scheduled_time": job.get("scheduled_time"),
        }
        if scheduler_db.job_fired_on_date(conn, jid, today_iso):
            row = conn.execute(
                """
                SELECT base_name, created_at FROM job_fire_history
                WHERE job_id = ? AND fire_date = ? AND status = 'success'
                """,
                (jid, today_iso),
            ).fetchone()
            item["status"] = "sent"
            item["base_name"] = row["base_name"] if row else None
            item["fired_at"] = row["created_at"] if row else None
        elif scheduler_db.job_expired_on_date(conn, jid, today_iso):
            item["status"] = "expired"
        elif not _time_gate_passed(job, now):
            item["status"] = "pending"
        else:
            data = evaluate_data_readiness(job, today, settings)
            if not data["ready"]:
                item["status"] = "waiting"
                item["missing_files"] = data.get("missing")
                item["present_files"] = data.get("present")
            else:
                item["status"] = "ready"
        out.append(item)
    out.sort(key=lambda x: (x.get("scheduled_time") or "", x.get("job_name") or ""))
    return out
