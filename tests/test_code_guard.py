"""Round 5, PR G: the code-leg guard refuses what burned task 744's budget —
whole-file reads of big files and file edits through the shell — and lets the
ordinary work through."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from app import claude_cli
from app.hooks import code_guard as g

BLOCKED = [
    "python3 -c \"open('src/A.java','w').write('x')\"",
    "cd repo && python3 - <<'EOF'\nfrom pathlib import Path\nPath('src/A.java').write_text('x')\nEOF",
    "cat > src/A.java <<'EOF'\nclass A {}\nEOF",
    "sed -i '' 's/a/b/' src/A.java",
    "sed -i 's/a/b/' src/A.java",
    "echo hi > notes.txt",
    "echo hi >> src/A.java",
    "printf x | tee src/A.java",
    "perl -pi -e 's/a/b/' src/A.java",
]
ALLOWED = [
    "mvn -f service/pom.xml clean test -Dtest=FooTest > /tmp/build.log 2>&1; tail -5 /tmp/build.log",
    "grep -rn 'Foo' service/src | head -40",
    'git commit -m "Guard x > y before mapping"',
    "git diff --stat 2>/dev/null",
    "cd service && ls -la",
    "python3 - <<'EOF'\nprint(open('src/A.java').read()[:200])\nEOF",
    "echo done >&2",
    "node .asta-context/resolve-task.js . 'cams eta'",
    "echo '- lesson' >> .asta-context/lessons.md",
    "python3 -c \"open('/tmp/x.json','w').write('{}')\"",
]


@pytest.mark.parametrize("cmd", BLOCKED)
def test_shell_writes_into_the_repo_are_refused(cmd):
    assert g.shell_write(cmd), cmd
    assert "Edit tool" in g.decide({"tool_name": "Bash", "tool_input": {"command": cmd}}, "build")


@pytest.mark.parametrize("cmd", ALLOWED)
def test_ordinary_commands_run(cmd):
    assert g.shell_write(cmd) == "", cmd


def test_a_planning_leg_is_told_it_changes_nothing():
    why = g.decide({"tool_name": "Bash", "tool_input": {"command": "echo x > a.txt"}}, "plan")
    assert why.startswith("Planning leg")


def test_big_files_are_read_anchored(tmp_path):
    big = tmp_path / "Big.java"
    big.write_text("\n".join(f"line {i}" for i in range(g.READ_MAX_LINES + 50)))
    small = tmp_path / "Small.java"
    small.write_text("class Small {}\n")
    assert "Read it anchored" in g.decide({"tool_name": "Read", "tool_input": {"file_path": str(big)}}, "build")
    assert g.decide({"tool_name": "Read", "tool_input": {"file_path": str(big), "offset": 10,
                                                         "limit": 80}}, "build") == ""
    assert g.decide({"tool_name": "Read", "tool_input": {"file_path": str(small)}}, "build") == ""


def test_the_hook_speaks_claude_codes_protocol():
    event = {"tool_name": "Bash", "tool_input": {"command": "sed -i 's/a/b/' A.java"}}
    out = subprocess.run([sys.executable, str(claude_cli.CODE_GUARD), "build"],
                         input=json.dumps(event), capture_output=True, text=True, timeout=20)
    decision = json.loads(out.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    ok = subprocess.run([sys.executable, str(claude_cli.CODE_GUARD), "build"],
                        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}),
                        capture_output=True, text=True, timeout=20)
    assert ok.stdout.strip() == ""


def test_only_code_legs_get_the_guard(monkeypatch):
    monkeypatch.delenv("ASTA_CODE_GUARD", raising=False)
    assert claude_cli.leg_settings(False, False) == claude_cli.OUTPUT_SETTINGS
    s = json.loads(claude_cli.leg_settings(True, True))
    assert s["hooks"]["PreToolUse"][0]["hooks"][0]["command"].endswith("plan")
    assert s["bashOutputMaxChars"] == claude_cli.BASH_INLINE_CHARS
    monkeypatch.setenv("ASTA_CODE_GUARD", "0")
    monkeypatch.setenv("ASTA_STOP_TESTS", "0")
    assert claude_cli.leg_settings(True, False) == claude_cli.OUTPUT_SETTINGS
