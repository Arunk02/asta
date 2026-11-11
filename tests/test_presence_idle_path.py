"""Idle time is read by full path, so it works under Asta's launchd PATH (11 Nov).

The job's PATH has no /usr/sbin; a bare "ioreg" was never found, the idle time
was unknown, and unknown means "at the laptop" — away mode switched itself off
while he was out, and pushes held for "at the laptop" waited until stale.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

from app import presence


def test_ioreg_is_called_by_its_full_path(monkeypatch):
    called = {}

    class Proc:
        async def communicate(self):
            return b'  "HIDIdleTime" = 1200000000000\n', b""

    async def exec_(*args, **kw):
        called["args"] = args
        return Proc()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_)
    assert asyncio.run(presence.idle_seconds()) == 1200.0
    assert called["args"][0] == "/usr/sbin/ioreg"


@pytest.mark.skipif(sys.platform != "darwin" or not os.path.exists("/usr/sbin/ioreg"),
                    reason="needs macOS ioreg")
def test_idle_is_read_under_the_launchd_path(monkeypatch):
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    assert asyncio.run(presence.idle_seconds()) is not None
