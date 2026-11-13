"""Files can go with a Teams message — shown first, checked twice, never secrets."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app import agent, attachments, capabilities, loop, main, ops, store, tasks
from app import teams_bridge as tb

WHO = "Marlowe Reyes"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "work").mkdir()
    return tmp_path


def _file(home, rel, body=b"print('hi')\n"):
    p = home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return str(p)


# --- what may be attached ------------------------------------------------------

def test_a_work_file_passes(home):
    f = _file(home, "work/producer/Producer.java")
    assert attachments.check([f]) == [f]


def test_a_task_worktree_file_passes(home):
    f = _file(home, ".asta-worktrees/repo/task-9/Producer.java")
    assert attachments.check(f) == [f]


@pytest.mark.parametrize("rel", [
    "work/.env", "work/app/.env.local", "work/certs/client.pem", "work/kafka.jks",
    "work/jaas.conf", "work/db-password.txt", ".ssh/notes.txt", ".config/gh/hosts.yml",
    "work/vault-dump.json",
])
def test_secrets_and_hidden_config_are_refused(home, rel):
    f = _file(home, rel)
    with pytest.raises(attachments.Refused):
        attachments.check([f])


def test_outside_home_missing_too_many_and_too_big_are_refused(home, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "x.txt"
    outside.write_text("x")
    with pytest.raises(attachments.Refused):
        attachments.check([str(outside)])
    with pytest.raises(attachments.Refused):
        attachments.check([str(home / "work/nope.txt")])
    many = [_file(home, f"work/f{i}.txt") for i in range(attachments.MAX_FILES + 1)]
    with pytest.raises(attachments.Refused):
        attachments.check(many)
    big = home / "work/big.bin"
    big.write_bytes(b"0" * (attachments.MAX_BYTES + 1))
    with pytest.raises(attachments.Refused):
        attachments.check([str(big)])


# --- staged, shown, carried, sent -------------------------------------------------

@pytest.fixture
def conv(monkeypatch):
    c = store.create_conversation(model="claude_cli", workspace=None)
    monkeypatch.setattr(tasks, "current_conversation", lambda: c["id"])
    store.add_ui_message(c["id"], "user", f"attach the producer file and send it to {WHO}")
    return c["id"]


def test_a_message_with_a_file_is_staged_never_sent_on_its_own(home, conv, monkeypatch):
    f = _file(home, "work/producer/Producer.java")
    sent = []

    async def send_message(*a, **k):
        sent.append((a, k))
        return WHO

    monkeypatch.setattr(tb, "send_message", send_message)
    out = agent.prepare_to_send("here is the producer", to=WHO, channel="teams", files=[f])
    assert "Draft staged" in out and "Producer.java" in out
    staged = loop.take(conv)
    assert staged["files"] == [f]
    assert not sent
    assert main._mechanical_send(staged)["args"]["files"] == [f]


def test_a_secret_is_refused_at_staging(home, conv):
    f = _file(home, "work/.env")
    out = agent.prepare_to_send("config", to=WHO, channel="teams", files=[f])
    assert out.startswith("Not staged")
    assert not loop.take(conv)


def test_files_only_on_teams(home, conv):
    f = _file(home, "work/a.txt")
    assert agent.prepare_to_send("x", to="someone@x.com", channel="email",
                                 files=[f]).startswith("Not staged")


def test_a_revision_keeps_the_files(home, conv):
    f = _file(home, "work/producer/Producer.java")
    agent.prepare_to_send("here is the producer", to=WHO, channel="teams", files=[f])
    staged = loop.take(conv)
    loop.stage(conv, staged)
    agent.prepare_to_send("producer attached, run it with the dev profile", to=WHO,
                          channel="teams")
    assert loop.take(conv)["files"] == [f]


def test_the_op_rechecks_and_passes_files_to_the_bridge(home, monkeypatch):
    f = _file(home, "work/producer/Producer.java")
    got = {}

    async def send_message(chat, text, allow_group=False, files=None):
        got.update(chat=chat, text=text, files=files)
        return chat

    monkeypatch.setattr(tb, "send_message", send_message)
    line = asyncio.run(ops.REGISTRY["teams_send"]["run"](to=WHO, text="producer", files=[f]))
    assert got["files"] == [f] and "Producer.java" in line


def test_the_op_refuses_a_file_that_became_a_secret(home, monkeypatch):
    f = _file(home, "work/token.txt")

    async def send_message(*a, **k):
        raise AssertionError("must not send")

    monkeypatch.setattr(tb, "send_message", send_message)
    with pytest.raises(attachments.Refused):
        asyncio.run(ops.REGISTRY["teams_send"]["run"](to=WHO, text="x", files=[f]))
