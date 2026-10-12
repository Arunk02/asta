"""12 Oct: "when was far away and lap in sleep, if i send any message, im not
getting any respond".

The bridge took `messages.upsert` of type "notify" only. What he sent to the Asta
group while the Mac slept came back on reconnect as type "append" and was
dropped without a log line, so nothing reached Asta from 9 Oct 16:42 on. And the
bridge reconnected every 3 s on each dark wake: 10,705 times in three days.

The rules live in whatsapp/catchup.js and are run here with node.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "whatsapp" / "bridge.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")

NOW = 1_791_750_000
GROUP = "120363409483000000@g.us"


def _run(script: str):
    code = (f"import * as c from {json.dumps(str(ROOT / 'whatsapp' / 'catchup.js'))};\n"
            f"const out = (() => {{ {script} }})();\nconsole.log(JSON.stringify(out));")
    res = subprocess.run([NODE, "--input-type=module", "-e", code],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _msg(mid, ts, text="hi", jid=GROUP, image=False):
    m = {"key": {"id": mid, "remoteJid": jid, "fromMe": True}, "messageTimestamp": ts,
         "message": {"conversation": text} if text else {}}
    if image:
        m["message"]["imageMessage"] = {"caption": ""}
    return m


def _take(messages, last_ts=NOW - 7200, mine=()):
    return _run(f"""
      const r = c.takeCatchup({json.dumps(messages)}, {{
        lastTs: {last_ts}, now: {NOW}, maxAgeSec: 86400,
        allowed: (j) => j === {json.dumps(GROUP)},
        mine: (id) => {json.dumps(list(mine))}.includes(id), botMark: "🤖 " }});
      return {{take: r.take.map((m) => m.key.id), skipped: r.skipped}};""")


@needs_node
def test_what_he_sent_while_it_slept_is_answered_oldest_first():
    out = _take([_msg("b", NOW - 600, "and check task 276"),
                 _msg("a", NOW - 3600, "HYNBFDPWY7R why is AP still in progress")])
    assert out["take"] == ["a", "b"]


@needs_node
@pytest.mark.parametrize("msg, why", [
    (_msg("x", NOW - 60, jid="someone@s.whatsapp.net"), "other chat"),
    (_msg("x", NOW - 60, "🤖 Sent to Vinish"), "ours"),
    (_msg("x", NOW - 60, ""), "no text"),
    (_msg("x", NOW - 9000), "already handled"),
    (_msg("x", NOW - 90000), "too old"),
])
def test_what_is_never_answered_again(msg, why):
    out = _take([msg], last_ts=NOW - 7200 if why != "too old" else 0)
    assert out["take"] == [] and out["skipped"][0]["why"] == why


@needs_node
def test_a_message_this_bridge_sent_is_never_taken_as_his():
    out = _take([_msg("sent-by-bridge", NOW - 60, "ok")], mine=("sent-by-bridge",))
    assert out["take"] == [] and out["skipped"][0]["why"] == "ours"


@needs_node
def test_a_photo_without_a_caption_still_counts():
    assert _take([_msg("p", NOW - 60, "", image=True)])["take"] == ["p"]


@needs_node
def test_a_protobuf_long_timestamp_is_read():
    assert _run("return c.tsOf({messageTimestamp: {toNumber: () => 1791750000}});") == NOW


@needs_node
def test_reconnects_back_off_to_a_minute():
    assert _run("return [1,2,3,4,5,6,9].map((n) => c.backoffMs(n));") == \
        [3000, 6000, 12000, 24000, 48000, 60000, 60000]


def test_the_bridge_answers_a_caught_up_batch_not_only_live_messages():
    js = BRIDGE.read_text()
    assert 'if (type !== "notify") return;' not in js
    assert "takeCatchup(" in js and "Picked up" in js
    assert "saveLastTs(tsOf(msg))" in js, "a handled message moves the mark, so it is never re-answered"
    assert "backoffMs(closes)" in js and "closes = 0;" in js
    assert (ROOT / ".gitignore").read_text().count("whatsapp/state.json") == 1


@needs_node
def test_the_bridge_still_parses():
    assert subprocess.run([NODE, "--check", str(BRIDGE)], capture_output=True).returncode == 0
