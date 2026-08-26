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

    # The workspace's OWN pinned command, which is the authority. Asta was asking
    # Arun to configure something his context engine already recorded: every repo
    # carries `_pins.yml` with `unit_test` — and the booking one even documents
    # WHY `clean` is mandatory (MapStruct refuses to regenerate over stale
    # sources). Reading it beats asking him, and it stays correct when he changes
    # the build without telling Asta.
    pinned = _pinned_command(root)
    if pinned:
        return pinned

    # Asta's own map, for a repo whose workspace pins nothing.
    mapped = _configured_command(root)
    if mapped:
        return mapped

    return _autodetect(root)


#: Where a contmark workspace keeps its per-repo facts.
PINS = "_pins.yml"


def _pinned_command(root: Path) -> str | None:
    """The unit-test command this repo's workspace already pins, if any.

    Prefers `unit_test` over `build`: a gate that decides whether a change is
    done wants the tests run, and `package` can pass with a suite that never ran.
    """
    for pins in _pins_files(root):
        try:
            import yaml
            data = yaml.safe_load(pins.read_text()) or {}
        except Exception:                              # noqa: BLE001
            continue
        commands = data.get("commands") if isinstance(data.get("commands"), dict) else data
        for key in ("unit_test", "test", "build"):
            value = (commands or {}).get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _pins_files(root: Path) -> list[Path]:
    """`_pins.yml` for this repo, wherever its workspace keeps context."""
    out = []
    for ctx in (".contmark", ".context", ".asta-context"):
        # The repo may BE the workspace, or sit inside one.
        for base in (root, root.parent):
            candidate = base / ctx / "repos" / root.name / PINS
            if candidate.is_file():
                out.append(candidate)
    return out


#: repo directory name -> check command. Lives with Asta's data, not in the repo.
COMMANDS_FILE = Path(__file__).resolve().parent.parent / "data" / "verify-commands.json"


def _configured_command(root: Path) -> str | None:
    """The check configured for this repo in Asta's own map, if any."""
    try:
        import json
        data = json.loads(COMMANDS_FILE.read_text())
    except (OSError, ValueError):
        return None
    cmd = data.get(root.name)
    return cmd.strip() if isinstance(cmd, str) and cmd.strip() else None


#: Build systems whose suites are too heavy to run on a guess, but whose presence
#: proves the repo HAS a check — so "no oracle" would be the wrong conclusion.
_HEAVY_MARKERS = ("pom.xml", "build.gradle", "build.gradle.kts")


def unconfigured(cwd: str) -> str | None:
    """A repo that plainly has a test suite but no check Asta may run.

    The verifier is a no-op where no command resolves, which is correct — and
    silent, which is not. Every repo in the booking workspace is Maven, so the
    gate was switched on and verified nothing at all, indistinguishable from a
    gate that was working. This is what makes that state visible.
    """
    root = Path(cwd)
    if resolve_command(cwd):
        return None
    found = _build_files(root)
    if not found:
        return None
    return (f"{root.name}: has {found[0]} but no check Asta may run — "
            f"add it to data/verify-commands.json to close the loop")


def _build_files(root: Path) -> list[str]:
    """Build files at the root OR one level down.

    Every repo in the booking workspace is a multi-module Maven build with the
    poms in `service/`, `componenttest/` and `perftest/` — nothing at the top at
    all. Looking only at the root found none of them and concluded, wrongly, that
    these repos have no test suite.
    """
    hits: list[str] = []
    try:
        for entry in sorted(root.iterdir()):
            if entry.name in _HEAVY_MARKERS:
                hits.append(entry.name)
            elif entry.is_dir() and not entry.name.startswith("."):
                for marker in _HEAVY_MARKERS:
                    if (entry / marker).is_file():
                        hits.append(f"{entry.name}/{marker}")
                        break
    except OSError:
        return []
    return hits


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


#: Output that means the CHECK could not run, not that the code is wrong. Kept
#: deliberately narrow: a false match here would hide a genuine test failure,
#: which is the one thing this gate exists to catch. Every entry is a build-system
#: or network error that cannot be produced by application code being incorrect.
_INFRA_FAILURE = re.compile(
    r"could not resolve dependencies|could not transfer artifact|"
    r"non-resolvable parent pom|could not find artifact|"
    r"connection refused|connection timed out|unknownhostexception|"
    r"network is unreachable|no route to host|temporary failure in name resolution|"
    r"401 unauthorized|403 forbidden|authentication failed|"
    r"suncertpathbuilderexception|pkix path building failed|"
    r"cannot connect to the docker daemon|docker daemon is not running|"
    # The compiler itself crashed rather than rejecting the code. Seen for real
    # on the booking repos: Lombok 1.18.30 pinned in the pom against a local
    # Corretto 21.0.4 gives "Fatal error compiling: ExceptionInInitializerError:
    # com.sun.tools.javac.code.TypeTag :: UNKNOWN" in 12 seconds. That is a
    # toolchain mismatch on the machine, not a mistake in the code — and it is
    # shaped nothing like a real compile error, which names a file and a line
    # ("Foo.java:[12,5] cannot find symbol"). Without this the gate would fix-loop
    # against a broken JDK and never converge, three times, then park.
    r"fatal error compiling|exceptionininitializererror|"
    r"unsupported class file major version|"
    r"has been compiled by a more recent version of the java runtime|"
    r"no compiler is provided in this environment|"
    # MapStruct refusing to regenerate over stale sources left by a previous
    # build. His own lessons.md documents this and says `clean` is mandatory —
    # and the email-service pin omits it, so this WILL happen. Stale build state,
    # not broken code, and `_with_clean` below retries it the way the lesson says.
    r"attempt to recreate a file for type|filerexception",
    re.I)


#: A build that failed because of state left by the previous one.
_STALE_BUILD = re.compile(r"attempt to recreate a file for type|filerexception", re.I)


def _with_clean(command: str) -> str:
    """The same Maven command with `clean` in front of its goals.

    Their own lesson, applied: "always prefix a `clean` — treat it as mandatory
    for every build/test invocation here."
    """
    parts = command.split()
    if "clean" in parts or not parts:
        return command
    for goal in ("test", "package", "verify", "install", "compile"):
        if goal in parts:
            parts.insert(parts.index(goal), "clean")
            return " ".join(parts)
    return command


async def run(cwd: str, command: str | None, _retried: bool = False) -> VerifyResult:
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
        # A timeout is evidence the CHECK is too slow, not that the code is
        # wrong — and looping on it is the most expensive possible mistake: three
        # rounds of a twenty-minute check is an hour spent proving nothing. It is
        # reported loudly instead, because a check that never finishes is a check
        # that is quietly not protecting anything.
        return VerifyResult(ran=False, ok=False, command=command, code=124,
                            tail=(f"check timed out after {timeout_seconds()}s and was "
                                  f"SKIPPED, so nothing was verified. Raise "
                                  f"ASTA_VERIFY_TIMEOUT or use a narrower command: {command}"))
    code = proc.returncode or 0
    out = raw.decode(errors="replace")
    # The one failure the workspace already tells us how to fix. Retrying with
    # `clean` is not a guess: `lessons.md` in these repos says it is mandatory and
    # explains why, and two of the three pinned commands omit it anyway.
    if code != 0 and _STALE_BUILD.search(out) and not _retried:
        cleaned = _with_clean(command)
        if cleaned != command:
            return await run(cwd, cleaned, _retried=True)
    # exit 127 = command not found: the oracle is misconfigured, not the code.
    # Skip rather than loop forever on a broken check.
    if code == 127:
        return VerifyResult(ran=False, ok=False, command=command, code=code,
                            tail=out[-_MAX_TAIL:])
    # The check ran and failed, but not because the code is wrong: it could not
    # reach the artifact repository, or the VPN is down, or a credential expired.
    # Maven exits 1 for all of these exactly as it does for a red suite, so
    # without this the gate reads "Arun is off the VPN" as "the tests fail",
    # loops to fix code that was never broken, escalates to a stronger brain, and
    # burns a paid run before parking. Infrastructure is not an oracle.
    if code != 0 and _INFRA_FAILURE.search(out):
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
