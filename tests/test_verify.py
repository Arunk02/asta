"""The objective bar. These prove the oracle is honest and, above all, SAFE:
no oracle or a broken oracle must skip (ran=False), never fail a task forever.
"""

from __future__ import annotations

import asyncio

import pytest

from app import verify


# --- gating -------------------------------------------------------------------

def test_off_by_default(monkeypatch):
    monkeypatch.delenv("ASTA_VERIFY", raising=False)
    assert verify.enabled() is False
    monkeypatch.setenv("ASTA_VERIFY", "1")
    assert verify.enabled() is True


def test_rounds_and_timeout_have_safe_floors(monkeypatch):
    monkeypatch.setenv("ASTA_VERIFY_MAX_ROUNDS", "0")   # can't disable the fix loop to 0
    assert verify.max_rounds() == 1
    monkeypatch.setenv("ASTA_VERIFY_MAX_ROUNDS", "junk")
    assert verify.max_rounds() == 2
    monkeypatch.setenv("ASTA_VERIFY_TIMEOUT", "5")       # floor keeps a check runnable
    assert verify.timeout_seconds() == 30


# --- command resolution -------------------------------------------------------

def test_no_oracle_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTA_VERIFY_CMD", raising=False)
    assert verify.resolve_command(str(tmp_path)) is None


def test_env_override_wins(tmp_path, monkeypatch):
    (tmp_path / ".asta-verify").write_text("from-file")
    monkeypatch.setenv("ASTA_VERIFY_CMD", "from-env")
    assert verify.resolve_command(str(tmp_path)) == "from-env"


def test_dotfile_beats_autodetect(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTA_VERIFY_CMD", raising=False)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")     # would auto-detect pytest
    (tmp_path / ".asta-verify").write_text("# a comment\nmvn -q -Dtest=Foo test\n")
    assert verify.resolve_command(str(tmp_path)) == "mvn -q -Dtest=Foo test"


def test_autodetect_pytest(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTA_VERIFY_CMD", raising=False)
    (tmp_path / "tests").mkdir()
    assert verify.resolve_command(str(tmp_path)) == "python -m pytest -q"


def test_autodetect_node(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTA_VERIFY_CMD", raising=False)
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
    assert verify.resolve_command(str(tmp_path)) == "npm test --silent"


def test_autodetect_stays_silent_on_heavy_repos(tmp_path, monkeypatch):
    """A bare maven/gradle repo must NOT auto-run its full suite — that needs an
    explicit .asta-verify. Guessing here would dump minutes of logs per loop."""
    monkeypatch.delenv("ASTA_VERIFY_CMD", raising=False)
    (tmp_path / "pom.xml").write_text("<project/>")
    assert verify.resolve_command(str(tmp_path)) is None


# --- running the check --------------------------------------------------------

def test_run_none_is_a_skip():
    r = asyncio.run(verify.run("/tmp", None))
    assert r.ran is False and r.ok is False


def test_green_command_passes(tmp_path):
    r = asyncio.run(verify.run(str(tmp_path), "true"))
    assert r.ran is True and r.ok is True and r.code == 0


def test_red_command_fails_and_keeps_the_tail(tmp_path):
    r = asyncio.run(verify.run(str(tmp_path), "echo BOOM_FAILURE; exit 1"))
    assert r.ran is True and r.ok is False and r.code == 1
    assert "BOOM_FAILURE" in r.tail


def test_missing_command_is_skipped_not_looped(tmp_path):
    """exit 127 = command not found -> a misconfigured oracle must SKIP (ran=False),
    or a typo'd .asta-verify would loop a task forever."""
    r = asyncio.run(verify.run(str(tmp_path), "this_binary_does_not_exist_xyz"))
    assert r.ran is False


def test_tail_is_bounded(tmp_path):
    r = asyncio.run(verify.run(str(tmp_path), "for i in $(seq 1 5000); do echo line$i; done; exit 1"))
    assert r.ran is True and r.ok is False
    assert len(r.tail) <= verify._MAX_TAIL


def test_signature_ignores_digits_so_same_failure_matches():
    """A plateau is 'the same KIND of failure again' — line numbers and values
    change, the failure doesn't."""
    a = verify.signature("tests/foo.py:40: assert 1 == 2")
    b = verify.signature("tests/foo.py:51: assert 3 == 4")
    assert a == b


def test_signature_separates_genuinely_different_failures():
    a = verify.signature("ImportError: no module named foo")
    b = verify.signature("assert response.status == ok")
    assert a != b


def test_empty_tail_has_a_stable_signature():
    assert verify.signature("") == verify.signature("")


def test_failure_feedback_carries_the_delta_not_history():
    r = verify.VerifyResult(ran=True, ok=False, command="pytest -q", code=1,
                            tail="E   assert 1 == 2")
    fb = verify.failure_feedback(r)
    assert "pytest -q" in fb and "assert 1 == 2" in fb
    assert "Fix the CAUSE" in fb and "Do not re-plan" in fb
