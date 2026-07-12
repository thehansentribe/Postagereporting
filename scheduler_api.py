"""Flask routes for scheduled report email system."""

from __future__ import annotations

import csv
import io
import os
import re
import string
from typing import Any

from flask import Blueprint, Response, jsonify, render_template, request

import db
import email_service
import scheduler
import scheduler_db

bp = Blueprint("scheduler", __name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _conn():
    return db.get_connection()


def _validate_settings(payload: dict[str, Any]) -> str | None:
    poll = payload.get(db.SETTING_POLLING_INTERVAL_SECONDS)
    if poll is not None:
        try:
            if int(poll) < 5:
                return "pollingIntervalSeconds must be at least 5"
        except (TypeError, ValueError):
            return "pollingIntervalSeconds must be an integer"
    ret = payload.get(db.SETTING_LOG_RETENTION_DAYS)
    if ret is not None:
        try:
            if int(ret) < 1:
                return "logRetentionDays must be at least 1"
        except (TypeError, ValueError):
            return "logRetentionDays must be an integer"
    exp = payload.get(db.SETTING_DEFAULT_EXPIRATION_HOURS)
    if exp is not None:
        try:
            if float(exp) < 0:
                return "defaultExpirationHours must be non-negative"
        except (TypeError, ValueError):
            return "defaultExpirationHours must be a number"
    admin = str(payload.get(db.SETTING_ADMIN_NOTIFICATION_EMAIL, "")).strip()
    if admin and not _EMAIL_RE.match(admin):
        return "adminNotificationEmail is invalid"
    return None


@bp.route("/report-settings")
def report_settings_page():
    return render_template("report_settings.html")


@bp.route("/scheduler")
def scheduler_dashboard_page():
    return render_template("scheduler_dashboard.html")


@bp.route("/scheduler/jobs")
def scheduler_jobs_page():
    return render_template("scheduler_jobs.html")


@bp.route("/scheduler/jobs/new")
def scheduler_job_new_page():
    return render_template("scheduler_job_editor.html", job_id=None)


@bp.route("/scheduler/reports/new")
def report_simple_new_page():
    return render_template("report_simple_new.html")


@bp.route("/scheduler/jobs/<int:job_id>")
def scheduler_job_edit_page(job_id: int):
    return render_template("scheduler_job_editor.html", job_id=job_id)


@bp.route("/scheduler/groups")
def scheduler_groups_page():
    return render_template("scheduler_groups.html")


@bp.route("/scheduler/log")
def scheduler_log_page():
    return render_template("scheduler_log.html")


@bp.get("/api/scheduler/settings")
def api_get_settings():
    try:
        conn = _conn()
        data = db.get_scheduler_settings(conn)
        conn.close()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.put("/api/scheduler/settings")
def api_put_settings():
    payload = request.get_json(silent=True) or {}
    err = _validate_settings(payload)
    if err:
        return jsonify({"error": err}), 400
    try:
        conn = _conn()
        with conn:
            db.set_scheduler_settings(conn, payload)
        data = db.get_scheduler_settings(conn)
        conn.close()
        return jsonify({"ok": True, **data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.post("/api/scheduler/settings/test-email-root")
def api_test_email_root():
    payload = request.get_json(silent=True) or {}
    path = payload.get("emailRootPath") or payload.get("email_root_path")
    if not path:
        conn = _conn()
        settings = db.get_scheduler_settings(conn)
        conn.close()
        path = settings.get(db.SETTING_EMAIL_ROOT_PATH)
    result = email_service.test_email_root_write(str(path))
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


def _windows_drive_roots() -> list[dict[str, str]]:
    roots: list[dict[str, str]] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if os.path.exists(drive):
            roots.append({"name": drive, "path": drive})
    return roots


def _browse_root_listing() -> dict[str, Any]:
    if os.name == "nt":
        return {
            "current": "",
            "parent": None,
            "folders": _windows_drive_roots(),
            "separator": "\\",
        }
    entries = _list_subdirectories("/")
    return {"current": "/", "parent": None, "folders": entries, "separator": "/"}


def _list_subdirectories(path: str) -> list[dict[str, str]]:
    folders: list[dict[str, str]] = []
    with os.scandir(path) as it:
        for entry in it:
            try:
                if entry.is_dir():
                    folders.append({"name": entry.name, "path": entry.path})
            except OSError:
                continue
    folders.sort(key=lambda f: f["name"].lower())
    return folders


@bp.get("/api/scheduler/browse-folders")
def api_browse_folders():
    raw = (request.args.get("path") or "").strip()
    sep = "\\" if os.name == "nt" else "/"
    if not raw:
        return jsonify(_browse_root_listing())
    try:
        if not os.path.isdir(raw):
            return jsonify({"error": f"Not a folder: {raw}"}), 400
        current = os.path.abspath(raw) if not raw.startswith("\\\\") else raw
        parent = os.path.dirname(current.rstrip("\\/")) or None
        if parent == current:
            parent = None
        # A drive root (e.g. "C:\\") or UNC share root should map back to the drive list / null.
        if os.name == "nt" and parent and len(parent) <= 2 and parent.endswith(":"):
            parent = parent + "\\"
        folders = _list_subdirectories(current)
        return jsonify(
            {
                "current": current,
                "parent": parent,
                "folders": folders,
                "separator": sep,
            }
        )
    except PermissionError:
        return jsonify({"error": f"Permission denied: {raw}"}), 400
    except FileNotFoundError:
        return jsonify({"error": f"Folder not found: {raw}"}), 400
    except OSError as e:
        return jsonify({"error": str(e)}), 400


@bp.get("/api/scheduler/folder-files")
def api_folder_files():
    raw = (request.args.get("path") or "").strip()
    if not raw:
        return jsonify({"error": "path required"}), 400
    try:
        files = scheduler._folder_files(raw)
        return jsonify(
            {
                "folder": raw,
                "files": [{"name": os.path.basename(f), "path": f} for f in files],
                "count": len(files),
            }
        )
    except OSError as e:
        return jsonify({"error": str(e)}), 400


@bp.get("/api/scheduler/dashboard")
def api_dashboard():
    try:
        conn = _conn()
        settings = db.get_scheduler_settings(conn)
        data = scheduler.dashboard_summary(conn, settings)
        conn.close()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.get("/api/scheduler/jobs")
def api_list_jobs():
    archived_only = request.args.get("archived") in ("1", "true", "yes")
    try:
        conn = _conn()
        jobs = scheduler_db.list_jobs(conn, archived_only=archived_only)
        conn.close()
        return jsonify({"jobs": jobs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.post("/api/scheduler/jobs/<int:job_id>/archive")
def api_archive_job(job_id: int):
    payload = request.get_json(silent=True) or {}
    archived = payload.get("archived", True)
    try:
        conn = _conn()
        with conn:
            job = scheduler_db.set_job_archived(conn, job_id, bool(archived))
        conn.close()
        return jsonify(job)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.get("/api/scheduler/jobs/<int:job_id>")
def api_get_job(job_id: int):
    try:
        conn = _conn()
        job = scheduler_db.get_job(conn, job_id)
        conn.close()
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(job)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.post("/api/scheduler/jobs")
def api_create_job():
    payload = request.get_json(silent=True) or {}
    if not str(payload.get("name", "")).strip():
        return jsonify({"error": "name required"}), 400
    try:
        conn = _conn()
        with conn:
            job = scheduler_db.create_job(conn, payload)
        conn.close()
        return jsonify(job), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.put("/api/scheduler/jobs/<int:job_id>")
def api_update_job(job_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        conn = _conn()
        with conn:
            job = scheduler_db.update_job(conn, job_id, payload)
        conn.close()
        return jsonify(job)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.delete("/api/scheduler/jobs/<int:job_id>")
def api_delete_job(job_id: int):
    try:
        conn = _conn()
        with conn:
            scheduler_db.delete_job(conn, job_id)
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.post("/api/scheduler/jobs/<int:job_id>/preview")
def api_preview_job(job_id: int):
    try:
        conn = _conn()
        job = scheduler_db.get_job(conn, job_id)
        if job is None:
            conn.close()
            return jsonify({"error": "Job not found"}), 404
        settings = db.get_scheduler_settings(conn)
        data = scheduler.preview_job(conn, job, settings)
        conn.close()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.post("/api/scheduler/jobs/preview-draft")
def api_preview_draft():
    """Preview unsaved job definition from editor payload."""
    payload = request.get_json(silent=True) or {}
    try:
        conn = _conn()
        settings = db.get_scheduler_settings(conn)
        job = {
            "name": payload.get("name") or "Preview",
            "subject_template": payload.get("subject_template") or "",
            "body_template": payload.get("body_template") or "",
            "required_files": payload.get("required_files") or [],
            "attachments": payload.get("attachments") or [],
            "recipients": payload.get("recipients") or [],
            "recipient_group_ids": payload.get("recipient_group_ids") or [],
            "data_readiness_mode": payload.get("data_readiness_mode"),
            "stale_file_threshold_minutes": payload.get("stale_file_threshold_minutes"),
        }
        data = scheduler.preview_job(conn, job, settings)
        conn.close()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.post("/api/scheduler/jobs/<int:job_id>/run-now")
def api_run_now(job_id: int):
    try:
        conn = _conn()
        job = scheduler_db.get_job(conn, job_id)
        if job is None:
            conn.close()
            return jsonify({"error": "Job not found"}), 404
        settings = db.get_scheduler_settings(conn)
        with conn:
            result = scheduler.execute_job_send(conn, job, settings, manual=True)
        conn.close()
        if not result.get("success"):
            return jsonify(result), 400
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.get("/api/scheduler/recipient-groups")
def api_list_groups():
    try:
        conn = _conn()
        groups = scheduler_db.list_recipient_groups(conn)
        conn.close()
        return jsonify({"groups": groups})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.post("/api/scheduler/recipient-groups")
def api_create_group():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("group_name") or "").strip()
    if not name:
        return jsonify({"error": "group_name required"}), 400
    members = payload.get("members") or []
    try:
        conn = _conn()
        with conn:
            g = scheduler_db.create_recipient_group(conn, name, members)
        conn.close()
        return jsonify(g), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.put("/api/scheduler/recipient-groups/<int:group_id>")
def api_update_group(group_id: int):
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("group_name") or "").strip()
    if not name:
        return jsonify({"error": "group_name required"}), 400
    try:
        conn = _conn()
        with conn:
            g = scheduler_db.update_recipient_group(
                conn, group_id, name, payload.get("members") or []
            )
        conn.close()
        return jsonify(g)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.delete("/api/scheduler/recipient-groups/<int:group_id>")
def api_delete_group(group_id: int):
    try:
        conn = _conn()
        with conn:
            out = scheduler_db.delete_recipient_group(conn, group_id)
        conn.close()
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.get("/api/scheduler/execution-log")
def api_execution_log():
    try:
        conn = _conn()
        rows = scheduler_db.list_execution_log(
            conn,
            start_date=request.args.get("start_date"),
            end_date=request.args.get("end_date"),
            status=request.args.get("status"),
            job_id=request.args.get("job_id", type=int),
            job_name_search=request.args.get("q"),
            limit=request.args.get("limit", type=int) or 500,
        )
        conn.close()
        return jsonify({"rows": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.get("/api/scheduler/execution-log/<int:log_id>")
def api_execution_log_detail(log_id: int):
    try:
        conn = _conn()
        row = scheduler_db.get_execution_log_entry(conn, log_id)
        conn.close()
        if row is None:
            return jsonify({"error": "Not found"}), 404
        return jsonify(row)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.get("/api/scheduler/execution-log.csv")
def api_execution_log_csv():
    try:
        conn = _conn()
        rows = scheduler_db.list_execution_log(
            conn,
            start_date=request.args.get("start_date"),
            end_date=request.args.get("end_date"),
            status=request.args.get("status"),
            job_id=request.args.get("job_id", type=int),
            job_name_search=request.args.get("q"),
            limit=5000,
        )
        conn.close()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(
            [
                "logged_at",
                "job_name",
                "status",
                "base_name",
                "recipient_count",
                "error_message",
            ]
        )
        for r in rows:
            w.writerow(
                [
                    r.get("logged_at"),
                    r.get("job_name"),
                    r.get("status"),
                    r.get("base_name"),
                    r.get("recipient_count"),
                    r.get("error_message"),
                ]
            )
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=execution_log.csv"},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def register_scheduler(app) -> None:
    app.register_blueprint(bp)
