"""Date and job token resolution for scheduled report emails."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

_TOKEN_RE = re.compile(r"\{(\w+)\}")


def resolve_tokens(text: str, ref_date: date, *, job_name: str = "", file_list: str = "") -> str:
    """Replace {YYYY}, {YY}, {MM}, {DD}, {DOW}, {JOB_NAME}, {FILE_LIST} in text."""
    dow = ref_date.strftime("%a")
    mapping = {
        "YYYY": ref_date.strftime("%Y"),
        "YY": ref_date.strftime("%y"),
        "MM": ref_date.strftime("%m"),
        "DD": ref_date.strftime("%d"),
        "DOW": dow,
        "JOB_NAME": job_name,
        "FILE_LIST": file_list,
    }

    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        return mapping.get(key, m.group(0))

    return _TOKEN_RE.sub(repl, text or "")


def format_file_list(paths: list[str]) -> str:
    if not paths:
        return ""
    return "\n".join(paths)


def now_in_timezone(tz_name: str) -> datetime:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Chicago")
    return datetime.now(tz)


def today_in_timezone(tz_name: str) -> date:
    return now_in_timezone(tz_name).date()


def file_check(path: str, stale_minutes: int | None = None) -> dict[str, Any]:
    """Return exists, size, mtime_iso; apply stale threshold if set."""
    from pathlib import Path

    p = Path(path)
    out: dict[str, Any] = {
        "path": path,
        "exists": False,
        "size": 0,
        "mtime": None,
        "present": False,
    }
    if not p.is_file():
        return out
    st = p.stat()
    out["exists"] = True
    out["size"] = int(st.st_size)
    out["mtime"] = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
    if out["size"] <= 0:
        return out
    if stale_minutes is not None and stale_minutes > 0:
        age_sec = datetime.now().timestamp() - st.st_mtime
        if age_sec > stale_minutes * 60:
            return out
    out["present"] = True
    return out


def resolve_patterns(
    patterns: list[str], ref_date: date, *, job_name: str = ""
) -> list[str]:
    return [resolve_tokens(p, ref_date, job_name=job_name) for p in patterns]
