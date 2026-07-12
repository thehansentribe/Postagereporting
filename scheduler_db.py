"""Database access for scheduled report jobs, groups, logs, and fire history."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

import db


def _row_to_job(row: sqlite3.Row, conn: sqlite3.Connection) -> dict[str, Any]:
    job_id = int(row["id"])
    return {
        "id": job_id,
        "name": row["name"],
        "description": row["description"],
        "enabled": bool(row["enabled"]),
        "schedule_type": row["schedule_type"],
        "scheduled_time": row["scheduled_time"],
        "days_of_week_csv": row["days_of_week_csv"],
        "day_of_month": row["day_of_month"],
        "one_time_at": row["one_time_at"],
        "effective_start_date": row["effective_start_date"],
        "effective_end_date": row["effective_end_date"],
        "subject_template": row["subject_template"],
        "body_template": row["body_template"],
        "data_readiness_mode": row["data_readiness_mode"],
        "stale_file_threshold_minutes": row["stale_file_threshold_minutes"],
        "expiration_hours": row["expiration_hours"],
        "send_failure_notification": bool(row["send_failure_notification"]),
        "retry_count": int(row["retry_count"] or 0),
        "retry_delay_seconds": int(row["retry_delay_seconds"] or 60),
        "attachment_folder": row["attachment_folder"],
        "post_send_action": row["post_send_action"] or "archive",
        "archive_subdir": row["archive_subdir"] or "Sent",
        "archived": bool(row["archived"]),
        "required_files": list_job_required_files(conn, job_id),
        "attachments": list_job_attachments(conn, job_id),
        "recipients": list_job_recipients(conn, job_id),
        "recipient_group_ids": list_job_recipient_group_ids(conn, job_id),
    }


def list_job_required_files(conn: sqlite3.Connection, job_id: int) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT id, file_path_pattern, sort_order
        FROM job_required_files
        WHERE job_id = ?
        ORDER BY sort_order, id
        """,
        (job_id,),
    )
    return [
        {
            "id": int(r["id"]),
            "file_path_pattern": r["file_path_pattern"],
            "sort_order": int(r["sort_order"]),
        }
        for r in cur.fetchall()
    ]


def list_job_attachments(conn: sqlite3.Connection, job_id: int) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT id, file_path_pattern, sort_order
        FROM job_attachments
        WHERE job_id = ?
        ORDER BY sort_order, id
        """,
        (job_id,),
    )
    return [
        {
            "id": int(r["id"]),
            "file_path_pattern": r["file_path_pattern"],
            "sort_order": int(r["sort_order"]),
        }
        for r in cur.fetchall()
    ]


def list_job_recipients(conn: sqlite3.Connection, job_id: int) -> list[str]:
    cur = conn.execute(
        "SELECT email_address FROM job_recipients WHERE job_id = ? ORDER BY id",
        (job_id,),
    )
    return [str(r["email_address"]) for r in cur.fetchall()]


def list_job_recipient_group_ids(conn: sqlite3.Connection, job_id: int) -> list[int]:
    cur = conn.execute(
        "SELECT group_id FROM job_recipient_groups WHERE job_id = ? ORDER BY group_id",
        (job_id,),
    )
    return [int(r["group_id"]) for r in cur.fetchall()]


def list_jobs(
    conn: sqlite3.Connection,
    *,
    enabled_only: bool = False,
    include_archived: bool = False,
    archived_only: bool = False,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM scheduled_jobs"
    clauses = []
    if enabled_only:
        clauses.append("enabled = 1")
    if archived_only:
        clauses.append("archived = 1")
    elif not include_archived:
        clauses.append("archived = 0")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY name COLLATE NOCASE"
    rows = conn.execute(sql).fetchall()
    return [_row_to_job(r, conn) for r in rows]


def set_job_archived(conn: sqlite3.Connection, job_id: int, archived: bool) -> dict[str, Any]:
    cur = conn.execute(
        "UPDATE scheduled_jobs SET archived = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (1 if archived else 0, job_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"Job {job_id} not found")
    out = get_job(conn, job_id)
    assert out is not None
    return out


def get_job(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM scheduled_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_job(row, conn)


def _replace_job_children(conn: sqlite3.Connection, job_id: int, payload: dict[str, Any]) -> None:
    conn.execute("DELETE FROM job_required_files WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM job_attachments WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM job_recipients WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM job_recipient_groups WHERE job_id = ?", (job_id,))

    for i, pat in enumerate(payload.get("required_files") or []):
        if isinstance(pat, dict):
            pattern = pat.get("file_path_pattern") or pat.get("pattern") or ""
            order = int(pat.get("sort_order", i))
        else:
            pattern = str(pat)
            order = i
        if not str(pattern).strip():
            continue
        conn.execute(
            """
            INSERT INTO job_required_files (job_id, file_path_pattern, sort_order)
            VALUES (?, ?, ?)
            """,
            (job_id, str(pattern).strip(), order),
        )

    for i, pat in enumerate(payload.get("attachments") or []):
        if isinstance(pat, dict):
            pattern = pat.get("file_path_pattern") or pat.get("pattern") or ""
            order = int(pat.get("sort_order", i))
        else:
            pattern = str(pat)
            order = i
        if not str(pattern).strip():
            continue
        conn.execute(
            """
            INSERT INTO job_attachments (job_id, file_path_pattern, sort_order)
            VALUES (?, ?, ?)
            """,
            (job_id, str(pattern).strip(), order),
        )

    for email in payload.get("recipients") or []:
        em = str(email).strip()
        if em:
            conn.execute(
                "INSERT INTO job_recipients (job_id, email_address) VALUES (?, ?)",
                (job_id, em),
            )

    for gid in payload.get("recipient_group_ids") or []:
        try:
            g = int(gid)
        except (TypeError, ValueError):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO job_recipient_groups (job_id, group_id) VALUES (?, ?)",
            (job_id, g),
        )


def create_job(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    cur = conn.execute(
        """
        INSERT INTO scheduled_jobs (
            name, description, enabled, schedule_type, scheduled_time,
            days_of_week_csv, day_of_month, one_time_at,
            effective_start_date, effective_end_date,
            subject_template, body_template, data_readiness_mode,
            stale_file_threshold_minutes, expiration_hours,
            send_failure_notification, retry_count, retry_delay_seconds,
            attachment_folder, post_send_action, archive_subdir, archived
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(payload["name"]).strip(),
            payload.get("description"),
            1 if payload.get("enabled", True) else 0,
            payload["schedule_type"],
            payload.get("scheduled_time"),
            payload.get("days_of_week_csv"),
            payload.get("day_of_month"),
            payload.get("one_time_at"),
            payload.get("effective_start_date"),
            payload.get("effective_end_date"),
            payload["subject_template"],
            payload["body_template"],
            payload.get("data_readiness_mode") or "all_required",
            payload.get("stale_file_threshold_minutes"),
            payload.get("expiration_hours"),
            1 if payload.get("send_failure_notification") else 0,
            int(payload.get("retry_count") or 0),
            int(payload.get("retry_delay_seconds") or 60),
            (str(payload.get("attachment_folder")).strip() or None)
            if payload.get("attachment_folder")
            else None,
            payload.get("post_send_action") or "archive",
            payload.get("archive_subdir") or "Sent",
            1 if payload.get("archived") else 0,
        ),
    )
    job_id = int(cur.lastrowid)
    _replace_job_children(conn, job_id, payload)
    out = get_job(conn, job_id)
    assert out is not None
    return out


def update_job(conn: sqlite3.Connection, job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    existing = get_job(conn, job_id)
    if existing is None:
        raise ValueError(f"Job {job_id} not found")
    merged = {**existing, **payload}
    conn.execute(
        """
        UPDATE scheduled_jobs SET
            name = ?, description = ?, enabled = ?, schedule_type = ?,
            scheduled_time = ?, days_of_week_csv = ?, day_of_month = ?,
            one_time_at = ?, effective_start_date = ?, effective_end_date = ?,
            subject_template = ?, body_template = ?, data_readiness_mode = ?,
            stale_file_threshold_minutes = ?, expiration_hours = ?,
            send_failure_notification = ?, retry_count = ?, retry_delay_seconds = ?,
            attachment_folder = ?, post_send_action = ?, archive_subdir = ?,
            archived = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            str(merged["name"]).strip(),
            merged.get("description"),
            1 if merged.get("enabled", True) else 0,
            merged["schedule_type"],
            merged.get("scheduled_time"),
            merged.get("days_of_week_csv"),
            merged.get("day_of_month"),
            merged.get("one_time_at"),
            merged.get("effective_start_date"),
            merged.get("effective_end_date"),
            merged["subject_template"],
            merged["body_template"],
            merged.get("data_readiness_mode") or "all_required",
            merged.get("stale_file_threshold_minutes"),
            merged.get("expiration_hours"),
            1 if merged.get("send_failure_notification") else 0,
            int(merged.get("retry_count") or 0),
            int(merged.get("retry_delay_seconds") or 60),
            (str(merged.get("attachment_folder")).strip() or None)
            if merged.get("attachment_folder")
            else None,
            merged.get("post_send_action") or "archive",
            merged.get("archive_subdir") or "Sent",
            1 if merged.get("archived") else 0,
            job_id,
        ),
    )
    if any(
        k in payload
        for k in (
            "required_files",
            "attachments",
            "recipients",
            "recipient_group_ids",
        )
    ):
        _replace_job_children(conn, job_id, merged)
    out = get_job(conn, job_id)
    assert out is not None
    return out


def delete_job(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute("DELETE FROM scheduled_jobs WHERE id = ?", (job_id,))


def list_recipient_groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, group_name FROM recipient_groups ORDER BY group_name COLLATE NOCASE"
    ).fetchall()
    out = []
    for r in rows:
        gid = int(r["id"])
        members = conn.execute(
            "SELECT email_address FROM recipient_group_members WHERE group_id = ? ORDER BY id",
            (gid,),
        ).fetchall()
        out.append(
            {
                "id": gid,
                "group_name": r["group_name"],
                "members": [str(m["email_address"]) for m in members],
                "member_count": len(members),
            }
        )
    return out


def get_recipient_group(conn: sqlite3.Connection, group_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, group_name FROM recipient_groups WHERE id = ?", (group_id,)
    ).fetchone()
    if row is None:
        return None
    gid = int(row["id"])
    members = conn.execute(
        "SELECT email_address FROM recipient_group_members WHERE group_id = ? ORDER BY id",
        (gid,),
    ).fetchall()
    return {
        "id": gid,
        "group_name": row["group_name"],
        "members": [str(m["email_address"]) for m in members],
    }


def create_recipient_group(conn: sqlite3.Connection, name: str, members: list[str]) -> dict[str, Any]:
    cur = conn.execute(
        "INSERT INTO recipient_groups (group_name) VALUES (?)", (name.strip(),)
    )
    gid = int(cur.lastrowid)
    for em in members:
        em = str(em).strip()
        if em:
            conn.execute(
                "INSERT INTO recipient_group_members (group_id, email_address) VALUES (?, ?)",
                (gid, em),
            )
    out = get_recipient_group(conn, gid)
    assert out is not None
    return out


def update_recipient_group(
    conn: sqlite3.Connection, group_id: int, name: str, members: list[str]
) -> dict[str, Any]:
    conn.execute(
        "UPDATE recipient_groups SET group_name = ? WHERE id = ?",
        (name.strip(), group_id),
    )
    conn.execute("DELETE FROM recipient_group_members WHERE group_id = ?", (group_id,))
    for em in members:
        em = str(em).strip()
        if em:
            conn.execute(
                "INSERT INTO recipient_group_members (group_id, email_address) VALUES (?, ?)",
                (group_id, em),
            )
    out = get_recipient_group(conn, group_id)
    if out is None:
        raise ValueError(f"Group {group_id} not found")
    return out


def delete_recipient_group(conn: sqlite3.Connection, group_id: int) -> dict[str, Any]:
    refs = conn.execute(
        "SELECT COUNT(*) AS c FROM job_recipient_groups WHERE group_id = ?", (group_id,)
    ).fetchone()
    ref_count = int(refs["c"]) if refs else 0
    conn.execute("DELETE FROM recipient_groups WHERE id = ?", (group_id,))
    return {"deleted": True, "referenced_by_jobs": ref_count}


def expand_job_recipients(conn: sqlite3.Connection, job: dict[str, Any]) -> list[str]:
    emails: list[str] = []
    seen: set[str] = set()
    for em in job.get("recipients") or []:
        low = str(em).strip().lower()
        if low and low not in seen:
            seen.add(low)
            emails.append(str(em).strip())
    for gid in job.get("recipient_group_ids") or []:
        rows = conn.execute(
            "SELECT email_address FROM recipient_group_members WHERE group_id = ?",
            (int(gid),),
        ).fetchall()
        for r in rows:
            em = str(r["email_address"]).strip()
            low = em.lower()
            if low and low not in seen:
                seen.add(low)
                emails.append(em)
    return emails


def job_fired_on_date(conn: sqlite3.Connection, job_id: int, fire_date: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM job_fire_history
        WHERE job_id = ? AND fire_date = ? AND status = 'success'
        """,
        (job_id, fire_date),
    ).fetchone()
    return row is not None


def job_expired_on_date(conn: sqlite3.Connection, job_id: int, fire_date: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM job_fire_history
        WHERE job_id = ? AND fire_date = ? AND status = 'expired'
        """,
        (job_id, fire_date),
    ).fetchone()
    return row is not None


def mark_job_fired(
    conn: sqlite3.Connection,
    job_id: int,
    fire_date: str,
    base_name: str | None,
    status: str,
) -> None:
    conn.execute(
        """
        INSERT INTO job_fire_history (job_id, fire_date, base_name, status)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(job_id, fire_date) DO UPDATE SET
            base_name = excluded.base_name,
            status = excluded.status,
            created_at = CURRENT_TIMESTAMP
        """,
        (job_id, fire_date, base_name, status),
    )


def insert_execution_log(
    conn: sqlite3.Connection,
    *,
    job_id: int | None,
    job_name: str | None,
    status: str,
    base_name: str | None = None,
    recipient_count: int | None = None,
    resolved_recipients: list[str] | None = None,
    resolved_files: list[str] | None = None,
    error_message: str | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO execution_log (
            job_id, job_name, status, base_name, recipient_count,
            resolved_recipients, resolved_files, error_message, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            job_name,
            status,
            base_name,
            recipient_count,
            json.dumps(resolved_recipients) if resolved_recipients is not None else None,
            json.dumps(resolved_files) if resolved_files is not None else None,
            error_message,
            json.dumps(details) if details is not None else None,
        ),
    )
    return int(cur.lastrowid)


def list_execution_log(
    conn: sqlite3.Connection,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    status: str | None = None,
    job_id: int | None = None,
    job_name_search: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if start_date:
        conditions.append("date(logged_at) >= date(?)")
        params.append(start_date)
    if end_date:
        conditions.append("date(logged_at) <= date(?)")
        params.append(end_date)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if job_id is not None:
        conditions.append("job_id = ?")
        params.append(job_id)
    if job_name_search:
        conditions.append("job_name LIKE ?")
        params.append(f"%{job_name_search}%")
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT id, logged_at, job_id, job_name, status, base_name,
               recipient_count, resolved_recipients, resolved_files,
               error_message, details_json
        FROM execution_log
        {where}
        ORDER BY logged_at DESC
        LIMIT ?
    """
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_execution_log_row(r) for r in rows]


def get_execution_log_entry(conn: sqlite3.Connection, log_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM execution_log WHERE id = ?", (log_id,)
    ).fetchone()
    if row is None:
        return None
    return _execution_log_row(row, full=True)


def _execution_log_row(row: sqlite3.Row, *, full: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": int(row["id"]),
        "logged_at": row["logged_at"],
        "job_id": row["job_id"],
        "job_name": row["job_name"],
        "status": row["status"],
        "base_name": row["base_name"],
        "recipient_count": row["recipient_count"],
        "error_message": row["error_message"],
    }
    if full or row["resolved_recipients"]:
        try:
            out["resolved_recipients"] = json.loads(row["resolved_recipients"] or "[]")
        except json.JSONDecodeError:
            out["resolved_recipients"] = []
    if full or row["resolved_files"]:
        try:
            out["resolved_files"] = json.loads(row["resolved_files"] or "[]")
        except json.JSONDecodeError:
            out["resolved_files"] = []
    if full and row["details_json"]:
        try:
            out["details"] = json.loads(row["details_json"])
        except json.JSONDecodeError:
            out["details"] = {}
    return out


def purge_old_scheduler_logs(conn: sqlite3.Connection, retention_days: int) -> dict[str, int]:
    cur = conn.execute(
        """
        DELETE FROM execution_log
        WHERE logged_at < datetime('now', ?)
        """,
        (f"-{int(retention_days)} days",),
    )
    exec_deleted = int(cur.rowcount)
    cur = conn.execute(
        """
        DELETE FROM job_fire_history
        WHERE fire_date < date('now', ?)
        """,
        (f"-{int(retention_days)} days",),
    )
    hist_deleted = int(cur.rowcount)
    return {"execution_log": exec_deleted, "job_fire_history": hist_deleted}


def try_acquire_scheduler_lock(conn: sqlite3.Connection, holder: str, ttl_sec: int = 120) -> bool:
    """Acquire or refresh scheduler lock if stale or held by same holder."""
    row = conn.execute(
        "SELECT locked_at, locked_by FROM scheduler_lock WHERE lock_name = 'main'"
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO scheduler_lock (lock_name, locked_at, locked_by)
            VALUES ('main', CURRENT_TIMESTAMP, ?)
            """,
            (holder,),
        )
        return True
    locked_by = row["locked_by"]
    if locked_by == holder:
        conn.execute(
            """
            UPDATE scheduler_lock SET locked_at = CURRENT_TIMESTAMP, locked_by = ?
            WHERE lock_name = 'main'
            """,
            (holder,),
        )
        return True
    stale = conn.execute(
        """
        SELECT 1 FROM scheduler_lock
        WHERE lock_name = 'main'
          AND locked_at < datetime('now', ?)
        """,
        (f"-{ttl_sec} seconds",),
    ).fetchone()
    if stale:
        conn.execute(
            """
            UPDATE scheduler_lock SET locked_at = CURRENT_TIMESTAMP, locked_by = ?
            WHERE lock_name = 'main'
            """,
            (holder,),
        )
        return True
    return False


def release_scheduler_lock(conn: sqlite3.Connection, holder: str) -> None:
    conn.execute(
        """
        DELETE FROM scheduler_lock
        WHERE lock_name = 'main' AND locked_by = ?
        """,
        (holder,),
    )
