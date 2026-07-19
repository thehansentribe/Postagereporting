"""Tests for scheduled report email tokens and file-drop sender."""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

import pytest

import db as dbmod
import email_service
import scheduler
import scheduler_db
import scheduler_tokens
from scheduler import evaluate_data_readiness


def test_resolve_tokens():
    d = date(2026, 5, 22)
    out = scheduler_tokens.resolve_tokens(
        "Report {YYYY}-{MM}-{DD} ({DOW}) {JOB_NAME}",
        d,
        job_name="Daily",
    )
    assert "2026" in out
    assert "05" in out
    assert "22" in out
    assert "Daily" in out


def test_email_service_write_order_and_no_dat_on_failure():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        missing_attach = str(root / "nope.csv")
        result = email_service.send(
            email_service.EmailSendRequest(
                subject="Test subject",
                body="Test body",
                recipients=["a@example.com"],
                email_root_path=str(root),
                attachments=[missing_attach],
            )
        )
        assert not result.success
        dat_files = list(root.glob("*.dat"))
        assert len(dat_files) == 0


def test_email_service_success_crlf_and_dat_last():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        result = email_service.send(
            email_service.EmailSendRequest(
                subject="Subj",
                body="Body line",
                recipients=["one@test.com", "two@test.com"],
                email_root_path=str(root),
                timezone="America/Chicago",
            )
        )
        assert result.success
        base = result.base_name
        body_path = root / f"{base}body.txt"
        subj_path = root / f"{base}subject.txt"
        dat_path = root / f"{base}.dat"
        assert body_path.is_file() and subj_path.is_file() and dat_path.is_file()
        raw = body_path.read_bytes()
        assert b"\r\n" in raw or raw.endswith(b"\n")
        dat_text = dat_path.read_text(encoding="latin-1")
        assert "one@test.com" in dat_text
        assert "two@test.com" in dat_text


def test_format_attach_list_path_windows_style():
    """Working-sample shape: c:\\email\\<base>\\<file> with backslashes only."""
    name = "BCBS (3901) 7-10-2026.xlsx"
    assert (
        email_service._format_attach_list_path(r"c:\email", "260712104351", name)
        == r"c:\email\260712104351\BCBS (3901) 7-10-2026.xlsx"
    )
    assert (
        email_service._format_attach_list_path("c:/email/", "260712104351", name)
        == r"c:\email\260712104351\BCBS (3901) 7-10-2026.xlsx"
    )
    assert (
        email_service._format_attach_list_path(r"c:\email\\", "260712104351", name)
        == r"c:\email\260712104351\BCBS (3901) 7-10-2026.xlsx"
    )


def test_attach_txt_uses_configured_root_with_backslashes(tmp_path):
    """attach.txt lists configured root\\base\\file, not Path.resolve() host paths."""
    write_root = tmp_path / "email_drop"
    write_root.mkdir()
    src = tmp_path / "BCBS (3901) 7-10-2026.xlsx"
    src.write_bytes(b"xlsx-bytes")

    result = email_service.send(
        email_service.EmailSendRequest(
            subject="Parcel reports",
            body="Attached.",
            recipients=["dest@example.com"],
            email_root_path=str(write_root),
            attachments=[str(src)],
            timezone="America/Chicago",
        )
    )
    assert result.success
    base = result.base_name
    attach_list = write_root / f"{base}attach.txt"
    assert attach_list.is_file()
    lines = [
        ln for ln in attach_list.read_text(encoding="latin-1").splitlines() if ln.strip()
    ]
    assert len(lines) == 1
    expected = email_service._format_attach_list_path(str(write_root), base, src.name)
    assert lines[0] == expected
    assert "\\" in lines[0]
    assert "/" not in lines[0]
    assert not lines[0].startswith("/")
    assert lines[0].endswith(f"\\{base}\\{src.name}")
    assert lines[0] != str((write_root / base / src.name).resolve())


def test_duplicate_fire_prevention(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "sched.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        conn = dbmod.get_connection()
        try:
            job = scheduler_db.create_job(
                conn,
                {
                    "name": "Dup test",
                    "schedule_type": "data-only",
                    "subject_template": "S",
                    "body_template": "B",
                    "recipients": ["x@y.com"],
                },
            )
            conn.commit()
            scheduler_db.mark_job_fired(conn, job["id"], "2026-05-22", "base1", "success")
            conn.commit()
            assert scheduler_db.job_fired_on_date(conn, job["id"], "2026-05-22")
        finally:
            conn.close()


def test_data_readiness_all_required(tmp_path):
    f = tmp_path / "file.csv"
    f.write_text("data\n")
    job = {
        "name": "J",
        "required_files": [{"file_path_pattern": str(f)}],
        "data_readiness_mode": "all_required",
        "stale_file_threshold_minutes": None,
    }
    data = evaluate_data_readiness(job, date.today(), {})
    assert data["ready"] is True


def test_folder_mode_empty_not_ready(tmp_path):
    folder = tmp_path / "reports"
    folder.mkdir()
    job = {"name": "Folder job", "attachment_folder": str(folder)}
    data = evaluate_data_readiness(job, date.today(), {})
    assert data["ready"] is False
    assert data["folder_mode"] is True
    assert data["resolved"] == []


def test_folder_mode_with_files_ready(tmp_path):
    folder = tmp_path / "reports"
    folder.mkdir()
    (folder / "a.csv").write_text("x\n")
    (folder / "b.csv").write_text("y\n")
    # A subfolder should be ignored by folder scanning.
    (folder / "Sent").mkdir()
    job = {"name": "Folder job", "attachment_folder": str(folder)}
    data = evaluate_data_readiness(job, date.today(), {})
    assert data["ready"] is True
    assert len(data["resolved"]) == 2
    assert all(p.endswith(".csv") for p in data["resolved"])


def test_folder_mode_send_and_archive(monkeypatch, tmp_path):
    db_path = tmp_path / "sched.db"
    monkeypatch.setattr(dbmod, "DB_PATH", db_path)
    dbmod.init_db()

    folder = tmp_path / "reports"
    folder.mkdir()
    (folder / "one.csv").write_text("111\n")
    (folder / "two.csv").write_text("222\n")
    email_root = tmp_path / "email"

    conn = dbmod.get_connection()
    try:
        job = scheduler_db.create_job(
            conn,
            {
                "name": "Parcel Summary",
                "schedule_type": "weekly",
                "scheduled_time": "08:00",
                "days_of_week_csv": "Mon,Tue,Wed,Thu,Fri",
                "attachment_folder": str(folder),
                "subject_template": "{JOB_NAME} {YYYY}-{MM}-{DD}",
                "body_template": "Files: {FILE_LIST}",
                "recipients": ["dest@example.com"],
            },
        )
        conn.commit()

        settings = dbmod.get_scheduler_settings(conn)
        settings[dbmod.SETTING_EMAIL_ROOT_PATH] = str(email_root)

        result = scheduler.execute_job_send(
            conn, job, settings, fire_date="2026-05-22"
        )
        conn.commit()
    finally:
        conn.close()

    assert result.get("success") is True
    # Originals moved out of the watch folder.
    assert not (folder / "one.csv").exists()
    assert not (folder / "two.csv").exists()
    # Archived into Sent/<fire_date>/.
    archive = folder / "Sent" / "2026-05-22"
    assert (archive / "one.csv").is_file()
    assert (archive / "two.csv").is_file()
    # Email drop written with a .dat trigger and attachment copies.
    assert list(email_root.glob("*.dat"))


def test_folder_mode_job_roundtrip(monkeypatch, tmp_path):
    db_path = tmp_path / "sched.db"
    monkeypatch.setattr(dbmod, "DB_PATH", db_path)
    dbmod.init_db()
    conn = dbmod.get_connection()
    try:
        created = scheduler_db.create_job(
            conn,
            {
                "name": "RT",
                "schedule_type": "weekly",
                "days_of_week_csv": "Mon",
                "scheduled_time": "09:00",
                "attachment_folder": "/tmp/watch",
                "post_send_action": "archive",
                "archive_subdir": "Sent",
                "subject_template": "S",
                "body_template": "B",
                "recipients": ["x@y.com"],
            },
        )
        conn.commit()
        fetched = scheduler_db.get_job(conn, created["id"])
        assert fetched["attachment_folder"] == "/tmp/watch"
        assert fetched["post_send_action"] == "archive"
        assert fetched["archive_subdir"] == "Sent"
    finally:
        conn.close()
