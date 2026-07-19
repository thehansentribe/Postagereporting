"""VB6 file-drop email sender — the only module that writes to the email root."""

from __future__ import annotations

import errno
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scheduler_tokens import now_in_timezone

_LINE_ENDING = "\r\n"
_ENCODING = "latin-1"

_basename_lock = threading.Lock()
_last_second: str = ""
_last_counter: int = 0


@dataclass
class EmailSendRequest:
    subject: str
    body: str
    recipients: list[str]
    email_root_path: str
    attachments: list[str] = field(default_factory=list)
    timezone: str = "America/Chicago"


@dataclass
class EmailSendResult:
    success: bool
    base_name: str = ""
    error: str | None = None


def generate_base_name(timezone: str = "America/Chicago") -> str:
    """YYMMDDHHMMSS in configured TZ; append digit if same second."""
    global _last_second, _last_counter
    now = now_in_timezone(timezone)
    sec = now.strftime("%y%m%d%H%M%S")
    with _basename_lock:
        if sec == _last_second:
            _last_counter += 1
            return f"{sec}{_last_counter}"
        _last_second = sec
        _last_counter = 0
        return sec


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding=_ENCODING, newline="") as f:
        normalized = content.replace("\r\n", "\n").replace("\n", _LINE_ENDING)
        if normalized and not normalized.endswith(_LINE_ENDING):
            normalized += _LINE_ENDING
        f.write(normalized)
    os.replace(tmp, path)


def _friendly_os_error(e: OSError, root: Path) -> str:
    """Turn low-level OS errors into an actionable message for the email folder."""
    if getattr(e, "errno", None) in (errno.EPERM, errno.EACCES):
        return (
            f"Cannot write to the email folder '{root}': permission denied "
            f"({e}). Make sure the app has write access to this folder. On macOS, "
            f"grant the launcher Full Disk Access (System Settings > Privacy & "
            f"Security) or use an email folder outside ~/Documents and ~/Desktop; "
            f"on Windows, confirm write permission to the network share."
        )
    return str(e)


def _validate_file(path: Path) -> str | None:
    if not path.is_file():
        return f"Missing file: {path}"
    if path.stat().st_size <= 0:
        return f"Empty file: {path}"
    return None


def _format_attach_list_path(email_root: str, base_name: str, filename: str) -> str:
    """Windows-style path for VB6 attach.txt from the configured email root."""
    root = email_root.strip().replace("/", "\\").rstrip("\\")
    return f"{root}\\{base_name}\\{filename}"


def send(request: EmailSendRequest) -> EmailSendResult:
    """
    Write email files per VB6 watcher protocol. Never writes .dat until prior files validate.
    """
    root = Path(request.email_root_path.strip())
    if not request.recipients:
        return EmailSendResult(success=False, error="No recipients")
    if not str(request.subject or "").strip():
        return EmailSendResult(success=False, error="Subject is empty")
    if not str(request.body or "").strip():
        return EmailSendResult(success=False, error="Body is empty")

    base_name = generate_base_name(request.timezone)
    attach_dir: Path | None = None
    copied_attach_paths: list[Path] = []

    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return EmailSendResult(
            success=False, base_name=base_name, error=_friendly_os_error(e, root)
        )

    subject_path = root / f"{base_name}subject.txt"
    body_path = root / f"{base_name}body.txt"
    attach_list_path = root / f"{base_name}attach.txt"
    dat_path = root / f"{base_name}.dat"

    try:
        if request.attachments:
            attach_dir = root / base_name
            attach_dir.mkdir(parents=True, exist_ok=True)
            for src in request.attachments:
                src_p = Path(src)
                if not src_p.is_file():
                    raise OSError(f"Attachment not found: {src}")
                dest = attach_dir / src_p.name
                if dest.exists():
                    stem, suf = dest.stem, dest.suffix
                    dest = attach_dir / f"{stem}_{id(dest)}{suf}"
                shutil.copy2(str(src_p), str(dest))
                copied_attach_paths.append(dest)

            attach_lines = [
                _format_attach_list_path(
                    request.email_root_path, base_name, p.name
                )
                for p in copied_attach_paths
            ]
            _atomic_write_text(attach_list_path, "\n".join(attach_lines) + _LINE_ENDING)

        _atomic_write_text(body_path, request.body)
        _atomic_write_text(subject_path, request.subject.strip())

        for p in (subject_path, body_path):
            err = _validate_file(p)
            if err:
                raise OSError(err)
        if request.attachments:
            err = _validate_file(attach_list_path)
            if err:
                raise OSError(err)
            for p in copied_attach_paths:
                err = _validate_file(p)
                if err:
                    raise OSError(err)

        dat_content = _LINE_ENDING.join(r.strip() for r in request.recipients if r.strip())
        if dat_content and not dat_content.endswith(_LINE_ENDING):
            dat_content += _LINE_ENDING
        _atomic_write_text(dat_path, dat_content)

    except OSError as e:
        _cleanup_partial(root, base_name, attach_dir)
        return EmailSendResult(
            success=False, base_name=base_name, error=_friendly_os_error(e, root)
        )

    return EmailSendResult(success=True, base_name=base_name)


def send_dict(payload: dict[str, Any]) -> dict[str, Any]:
    """Dict API matching plan: EmailService.send({...})."""
    req = EmailSendRequest(
        subject=str(payload.get("subject") or ""),
        body=str(payload.get("body") or ""),
        recipients=list(payload.get("recipients") or []),
        email_root_path=str(payload.get("emailRootPath") or payload.get("email_root_path") or ""),
        attachments=list(payload.get("attachments") or []),
        timezone=str(payload.get("timezone") or "America/Chicago"),
    )
    res = send(req)
    out: dict[str, Any] = {"success": res.success, "baseName": res.base_name}
    if res.error:
        out["error"] = res.error
    return out


def _cleanup_partial(root: Path, base_name: str, attach_dir: Path | None) -> None:
    for name in (
        f"{base_name}subject.txt",
        f"{base_name}body.txt",
        f"{base_name}attach.txt",
        f"{base_name}.dat",
    ):
        p = root / name
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    if attach_dir and attach_dir.is_dir():
        try:
            shutil.rmtree(attach_dir)
        except OSError:
            pass


def test_email_root_write(email_root_path: str) -> dict[str, Any]:
    """Create, write, and delete a temp file under email root."""
    root = Path(email_root_path.strip())
    try:
        root.mkdir(parents=True, exist_ok=True)
        test_file = root / ".scheduler_write_test.tmp"
        _atomic_write_text(test_file, "ok")
        if not test_file.is_file() or test_file.stat().st_size == 0:
            return {"ok": False, "error": "Test file missing or empty after write"}
        test_file.unlink()
        return {"ok": True}
    except OSError as e:
        return {"ok": False, "error": str(e)}
