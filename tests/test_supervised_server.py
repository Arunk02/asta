"""Restarting Asta must restart the Asta that is answering.

For two days there were two servers. An orphan held port 8321; the LaunchAgent's
process tried to bind, failed with `[Errno 48] address already in use`, and was
restarted by launchd forever. Everything looked fine — the UI answered, Teams was
read — and `launchctl kickstart com.asta.server` restarted a process that had
never served a request, so a restart changed nothing at all.

Nothing could see it. The log said only "address already in use", in a file
nobody opens unless something is already known to be wrong; and it is exactly
the condition under which stale in-memory state (a leaked bench double, say)
survives every attempt to clear it.

launchd knows which PID it supervises. Asta knows its own. When those differ,
say so — with the commands that fix it.
"""

from __future__ import annotations

import os

from app import health


def test_a_server_that_launchd_is_not_supervising_says_so(monkeypatch):
    monkeypatch.setattr(health, "_SERVING", True)
    monkeypatch.setattr(health, "supervising_pid", lambda: os.getpid() + 1)
    problem = health.unsupervised()
    assert problem, "the mismatch that made 'restart Asta' a no-op must be reported"
    assert str(os.getpid()) in problem, "say which process is answering"
    assert "kickstart" in problem, "a health problem nobody can act on is noise"


def test_the_supervised_server_is_not_a_problem(monkeypatch):
    monkeypatch.setattr(health, "_SERVING", True)
    monkeypatch.setattr(health, "supervising_pid", lambda: os.getpid())
    assert health.unsupervised() == ""


def test_launchd_not_managing_asta_at_all_is_not_a_problem(monkeypatch):
    """Run by hand on purpose is a choice, not a fault."""
    monkeypatch.setattr(health, "_SERVING", True)
    monkeypatch.setattr(health, "supervising_pid", lambda: 0)
    assert health.unsupervised() == ""


def test_a_process_that_is_not_serving_never_reports_this(monkeypatch):
    """The test suite, a CLI, a one-off script: every one of them has a PID that
    differs from the server's, and none of them is a second Asta."""
    monkeypatch.setattr(health, "_SERVING", False)
    monkeypatch.setattr(health, "supervising_pid", lambda: os.getpid() + 1)
    assert health.unsupervised() == ""


def test_the_pid_is_read_from_what_launchctl_actually_prints(monkeypatch):
    printed = '{\n\t"PID" = 69502;\n\t"LastExitStatus" = 0;\n}\n'

    class _Out:
        returncode, stdout = 0, printed

    monkeypatch.setattr(health.subprocess, "run", lambda *a, **k: _Out())
    monkeypatch.setattr(health.sys, "platform", "darwin")
    assert health.supervising_pid() == 69502
