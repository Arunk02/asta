"""The JDK every build Asta runs uses — the one CI uses, not whatever brew installed last.

14 Oct: Homebrew's openjdk 25 became Maven's default on this Mac. Under it the
email service and the event-router library fail to compile with Lombok errors
("TypeTag :: UNKNOWN", "cannot find symbol" on every @Slf4j log) while CI, on JDK
21, is green — so a code task's own check, or a local build, said "broken" about
code that was fine. JAVA_HOME pins it for task legs and for the verify check.
"""

from __future__ import annotations

import functools
import os
import subprocess

JAVA_VERSION = os.environ.get("ASTA_JAVA_VERSION", "21").strip()


@functools.lru_cache(maxsize=1)
def java_home() -> str:
    """ASTA_JAVA_HOME, else the installed JDK of ASTA_JAVA_VERSION, else ''."""
    explicit = os.environ.get("ASTA_JAVA_HOME", "").strip()
    if explicit:
        return explicit
    try:
        out = subprocess.run(["/usr/libexec/java_home", "-v", JAVA_VERSION],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def with_java(env: dict) -> dict:
    """env with JAVA_HOME pinned and its bin first on PATH — unchanged if no JDK found."""
    home = java_home()
    if not home:
        return env
    env = dict(env)
    env["JAVA_HOME"] = home
    env["PATH"] = f"{home}/bin{os.pathsep}{env.get('PATH', '')}"
    return env
