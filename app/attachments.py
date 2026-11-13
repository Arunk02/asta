"""Which local files may ride along with a Teams message.

A file sent to a colleague cannot be taken back, so the check is strict and in
code, not in a prompt: a real file, in his home folder, not hidden config, not
anything shaped like a secret, and small enough for Teams to take. Every
attachment is shown by name in the draft and goes only on his "send".
"""
from __future__ import annotations

import os
import re
from pathlib import Path

MAX_FILES = 5
MAX_BYTES = 25 * 1024 * 1024

#: Names that hold credentials or config values — never sent, whoever asks.
_SECRET_NAME = re.compile(
    r"(^\.env($|\.)|\.env$|\.pem$|\.key$|\.p12$|\.pfx$|\.jks$|\.keystore$|\.truststore$|"
    r"^id_(rsa|dsa|ecdsa|ed25519)|\.kdbx$|jaas|secret|credential|password|token|"
    r"\.npmrc$|\.netrc$|\.pgpass$|kubeconfig|vault)", re.I)


#: Hidden folders that hold his work, not config: task worktrees live here.
_WORK_DIRS = {".asta-worktrees"}


class Refused(ValueError):
    """The file may not be attached; the message says why."""


def check(paths, home: Path | None = None) -> list[str]:
    """The absolute paths to attach, or Refused naming the first bad one."""
    if isinstance(paths, str):
        paths = [paths]
    paths = [str(p).strip() for p in (paths or []) if str(p).strip()]
    if not paths:
        return []
    if len(paths) > MAX_FILES:
        raise Refused(f"at most {MAX_FILES} files per message")
    home = (home or Path.home()).resolve()
    out: list[str] = []
    for raw in paths:
        p = Path(os.path.expanduser(raw)).resolve()
        if not p.is_file():
            raise Refused(f"{raw} is not a file on this Mac")
        try:
            rel = p.relative_to(home)
        except ValueError:
            raise Refused(f"{p.name} is outside his home folder") from None
        if any(part.startswith(".") and part not in _WORK_DIRS for part in rel.parts):
            raise Refused(f"{p.name} sits in hidden config ({rel.parent}) — not sent")
        if any(_SECRET_NAME.search(part) for part in rel.parts):
            raise Refused(f"{p.name} looks like it holds secrets or config values — not sent")
        if p.stat().st_size > MAX_BYTES:
            raise Refused(f"{p.name} is over {MAX_BYTES // (1024 * 1024)} MB")
        if str(p) not in out:
            out.append(str(p))
    return out


def names(paths) -> str:
    return ", ".join(Path(p).name for p in (paths or []))
