"""Test for POST /api/system/restart (in-place re-exec)."""

from __future__ import annotations

import importlib
import tempfile
from pathlib import Path

import pytest

import db as dbmod
import watcher as watchermod

pytest.importorskip("flask")


def test_system_restart_returns_ok_and_reexecs(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "restart.db"
        monkeypatch.setattr(dbmod, "DB_PATH", p)
        dbmod.init_db()
        monkeypatch.setattr(watchermod, "ensure_dirs", lambda: None)

        import app as appmod

        appmod = importlib.reload(appmod)
        monkeypatch.setattr(appmod, "_ensure_watcher", lambda: None)
        appmod.app.config.update(TESTING=True)

        calls: dict = {}

        def fake_execv(exe, argv):
            calls["exe"] = exe
            calls["argv"] = list(argv)

        monkeypatch.setattr(appmod.os, "execv", fake_execv)
        monkeypatch.setattr(appmod.time, "sleep", lambda *a, **k: None)

        # Run the re-exec worker synchronously so the assertion is deterministic
        # (and so the test process is never actually replaced).
        class SyncThread:
            def __init__(self, target=None, daemon=None, **kwargs):
                self._target = target

            def start(self):
                if self._target:
                    self._target()

        monkeypatch.setattr(appmod.threading, "Thread", SyncThread)

        client = appmod.app.test_client()
        r = client.post("/api/system/restart")

        assert r.status_code == 200
        j = r.get_json()
        assert j["ok"] is True
        assert j["restarting"] is True
        assert calls["exe"] == appmod.sys.executable
        assert calls["argv"][0] == appmod.sys.executable
