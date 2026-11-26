"""Stop hook for Asta's build legs: a leg that changed code may not finish until a test run after
its last edit passed.

Wired by app/claude_cli.py (`--settings` on a build leg, next to code_guard). Reads Claude Code's
Stop-hook JSON on stdin and the session transcript it names. Exit 2 with a reason on stderr sends
the leg back to work; exit 0 lets it stop. After MAX_TRIES blocks in one session it lets the leg stop
and says why, so a leg that truly can't run tests (no Docker, a broken base) is not held forever.

Why: "done" was the leg's own opinion. A PR whose tests were never run reached Arun as finished,
and the verifier caught it only afterwards. A rule in a prompt is a wish, and an exit code is a gate
(the code-guard lesson, task 744). Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

MAX_TRIES = int(os.environ.get("ASTA_STOP_TESTS_TRIES", "3"))
SOURCE = re.compile(r"\.(java|kt|kts|scala|groovy|avsc|py|ts|tsx|js|jsx|go|rs)$")
TEST_CMD = re.compile(r"\b(mvn|mvnw|\./mvnw)\b[^\n|;&]*\b(test|verify|install|package)\b"
                      r"|\b(gradle|gradlew)\b[^\n|;&]*\b(test|check|build)\b"
                      r"|\bpytest\b|\bpython3? -m pytest\b|\b(npm|pnpm|yarn)\s+(run\s+)?test\b|\bgo test\b|\bcargo test\b")
FAILED = re.compile(r"BUILD FAILURE|Tests run: 0,|\bFAILED\b|\bfailed\b.*\btests?\b|There are test failures"
                    r"|COMPILATION ERROR|\[ERROR\]")
SKIP_TESTS = re.compile(r"-DskipTests|-Dmaven\.test\.skip=true")


def _events(transcript: Path):
    """(kind, payload) in order: ("edit", path) and ("test", passed:bool) from a JSONL transcript."""
    uses: dict[str, str] = {}
    out = []
    try:
        lines = transcript.read_text(errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        msg = rec.get("message") if isinstance(rec, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use":
                name, inp = part.get("name", ""), part.get("input") or {}
                if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                    p = str(inp.get("file_path") or inp.get("notebook_path") or "")
                    if SOURCE.search(p):
                        out.append(("edit", p))
                elif name == "Bash":
                    cmd = str(inp.get("command") or "")
                    if TEST_CMD.search(cmd) and not SKIP_TESTS.search(cmd):
                        uses[part.get("id", "")] = cmd
            elif part.get("type") == "tool_result" and part.get("tool_use_id") in uses:
                body = part.get("content")
                if isinstance(body, list):
                    body = " ".join(str(b.get("text", "")) for b in body if isinstance(b, dict))
                passed = not part.get("is_error") and not FAILED.search(str(body or ""))
                out.append(("test", passed))
    return out


def verdict(events) -> str:
    """'' when the leg may stop, else what it still owes."""
    last_edit = max((i for i, (k, _) in enumerate(events) if k == "edit"), default=-1)
    if last_edit < 0:
        return ""
    after = [ok for k, ok in events[last_edit + 1:] if k == "test"]
    edited = sorted({Path(p).name for k, p in events if k == "edit"})[:6]
    if after and after[-1]:
        return ""
    if after:
        return (f"The last test run after your edits failed. Fix it and run the scoped tests again "
                f"(changed: {', '.join(edited)}).")
    return (f"You changed {', '.join(edited)} but ran no tests after the last edit. Run the scoped "
            f"tests for those classes with the command in the service's CLAUDE.md (output to a file, "
            f"tail it), fix what fails, then finish. 'Tests run: 0' does not count.")


def _state(session: str) -> Path:
    safe = re.sub(r"[^\w-]", "", session)[:80] or "unknown"
    return Path(tempfile.gettempdir()) / f"asta-stop-tests-{safe}.json"


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    transcript = Path(str(data.get("transcript_path") or ""))
    if not transcript.is_file():
        return 0
    owed = verdict(_events(transcript))
    state = _state(str(data.get("session_id") or ""))
    if not owed:
        state.unlink(missing_ok=True)
        return 0
    tries = 0
    try:
        tries = int(json.loads(state.read_text()).get("tries", 0))
    except (OSError, ValueError):
        pass
    if tries >= MAX_TRIES:
        print(f"[tests-on-stop] gave up after {tries} tries; the leg stops without a passing test run.",
              file=sys.stderr)
        return 0
    state.write_text(json.dumps({"tries": tries + 1}))
    print(owed, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
