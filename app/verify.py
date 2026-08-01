"""The objective bar that turns Asta's loops from self-declared to resilient.

Every loop in Asta already stops on a signal the MODEL declares about itself:
`continue_working` says "not done", `ESCALATE:` says "too big", and a code leg
ends simply when the CLI stops typing. A model will emit "done" confidently while
the tests are red — so the mature learning stack (`learn.extract`, the teacher
half, credit/prune) can end up learning from a self-declared win. That is the one
hole this closes.

This module is the *oracle*: run the repo's own check (its tests / typecheck /
lint) as a subprocess and report pass or fail. Zero model tokens to check, and a
green suite cannot be talked out of — which is the whole point of "resilient".

Design rules that keep it safe and additive:

  - **No oracle → no change.** If no check can be resolved for a repo, the gate is
    a no-op and the task finishes exactly as it does today. Adding this can never
    make a task that used to complete stop completing.
  - **Off by default.** `ASTA_VERIFY=1` opts in, like every other cutover here.
  - **Explicit beats guessed.** An `.asta-verify` file (or `ASTA_VERIFY_CMD`) wins;
    auto-detection is a conservative convenience, never a heavy full-suite run.
  - **A broken oracle is skipped, not looped.** If the check command can't even be
    executed, we treat it as "no oracle" rather than failing the task forever.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

_FALSEY = ("0", "false", "no", "off", "")

#: How much of the check's output to keep — the tail carries the failure, and a
#: bounded tail is what gets fed back to the fixing leg (your own skill-evolution
#: `fat_outputs` rule: never let a full log into context).
_MAX_TAIL = 3000


def enabled() -> bool:
    """Off by default. The gate only engages when this is set AND an oracle exists."""
    return os.environ.get("ASTA_VERIFY", "0").strip().lower() not in _FALSEY


def max_rounds() -> int:
    """How many times a code task may auto-fix a failing check before it parks for
    Arun. The ceiling is what keeps "loop until green" from becoming "loop forever"."""
    try:
        return max(1, int(os.environ.get("ASTA_VERIFY_MAX_ROUNDS", "2")))
    except ValueError:
        return 2


def timeout_seconds() -> int:
    try:
        return max(30, int(os.environ.get("ASTA_VERIFY_TIMEOUT", "600")))
    except ValueError:
        return 600


def resolve_command(cwd: str, workspace: str | None = None) -> str | None:
    """The check to run for this repo, or None meaning 'no oracle — skip'.

    Priority, most explicit first:
      1. ASTA_VERIFY_CMD           — a global override (mainly for a pinned repo/CI).
      2. <cwd>/.asta-verify        — the repo's own scoped command, one per line.
      3. conservative auto-detect  — pytest for a python repo, the npm test script
                                     for a node one. Heavy suites (maven/gradle) are
                                     deliberately NOT auto-run; drop an .asta-verify.
    """
    override = os.environ.get("ASTA_VERIFY_CMD", "").strip()
    if override:
        return override

    root = Path(cwd)
    marker = root / ".asta-verify"
    try:
        if marker.is_file():
            for line in marker.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except OSError:
        pass

    return _autodetect(root)


def _autodetect(root: Path) -> str | None:
    """Cheap, safe defaults only. Anything that could run a multi-minute enterprise
    suite is left to an explicit `.asta-verify` on purpose."""
    try:
        names = {p.name for p in root.iterdir()}
    except OSError:
        return None

    py_markers = {"pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"}
    if names & py_markers or (root / "tests").is_dir():
        # Only claim pytest if the project actually looks pytest-shaped.
        if (root / "tests").is_dir() or "pytest.ini" in names or "tox.ini" in names \
                or _mentions(root / "pyproject.toml", "pytest") \
                or _mentions(root / "setup.cfg", "pytest"):
            return "python -m pytest -q"

    if "package.json" in names and _mentions(root / "package.json", '"test"'):
        return "npm test --silent"

    return None


def _mentions(path: Path, needle: str) -> bool:
    try:
        return needle in path.read_text()
    except OSError:
        return False


@dataclass
class VerifyResult:
    """Outcome of one check run.

    `ran` distinguishes "the oracle said no" (ran=True, ok=False) from "there was
    no usable oracle" (ran=False) — the gate must treat those completely
    differently: the first loops to fix, the second is a plain no-op.
    """
    ran: bool
    ok: bool
    command: str = ""
    code: int = 0
    tail: str = ""

    @property
    def summary(self) -> str:
        if not self.ran:
            return "no check to run"
        return "passed" if self.ok else f"failed (exit {self.code})"


async def run(cwd: str, command: str | None) -> VerifyResult:
    """Run the check as a subprocess. Never raises — a check that can't be executed
    is reported as ran=False (skip), never as a failure that would loop forever."""
    if not command:
        return VerifyResult(ran=False, ok=False)
    try:
        proc = await asyncio.create_subprocess_shell(
            command, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except (OSError, ValueError):
        return VerifyResult(ran=False, ok=False, command=command)
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds())
    except asyncio.TimeoutError:
        with _suppress():
            proc.kill()
        return VerifyResult(ran=True, ok=False, command=command, code=124,
                            tail=f"check timed out after {timeout_seconds()}s: {command}")
    code = proc.returncode or 0
    out = raw.decode(errors="replace")
    # exit 127 = command not found: the oracle is misconfigured, not the code.
    # Skip rather than loop forever on a broken check.
    if code == 127:
        return VerifyResult(ran=False, ok=False, command=command, code=code,
                            tail=out[-_MAX_TAIL:])
    return VerifyResult(ran=True, ok=(code == 0), command=command, code=code,
                        tail=out[-_MAX_TAIL:])


def failure_feedback(result: VerifyResult) -> str:
    """The bounded delta fed back to the fixing leg — the failure, not the history.

    This is the token lever: the leg resumes its cached session and gets only the
    failing output, not a fresh full context."""
    return (
        "\n\n[Asta verification gate — your own check FAILED]\n"
        f"Command: {result.command}\n"
        f"Exit: {result.code}\n"
        "Failing output (tail):\n"
        f"{result.tail}\n\n"
        "Fix the CAUSE of this failure and stop. Do not re-plan, do not re-run "
        "discovery — resume from where you are, make the check pass, then finish."
    )


_SIG_DIGITS = re.compile(r"\d+")
_SIG_WS = re.compile(r"\s+")


def signature(tail: str) -> str:
    """A stable fingerprint of a failure, so the gate can detect a PLATEAU — the
    fix leg reproducing the same failure instead of making progress.

    Digits (line numbers, counts, addresses, timings) and whitespace are stripped,
    so 'assert 1 == 2' at line 40 and 'assert 3 == 4' at line 51 fingerprint the
    same: the same KIND of failure. The tail carries the actual error, so we key on
    the end of the normalised text, not the whole log. Empty tail is its own stable
    signature (a check that fails silently still plateaus if it keeps doing so)."""
    norm = _SIG_WS.sub(" ", _SIG_DIGITS.sub("", (tail or "").lower())).strip()
    return hashlib.sha1(norm[-800:].encode()).hexdigest()[:16]


class _suppress:
    def __enter__(self): return self
    def __exit__(self, *a): return True
