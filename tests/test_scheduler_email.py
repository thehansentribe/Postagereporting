"""Tests for scheduled report email tokens and file-drop sender."""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

import pytest

import db as dbmod
import email_service
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
