"""No module may reference a name that does not exist.

Twice in one day, and both times the whole suite stayed green:

  * `main.py` called `meetings.warm_the_voice()` with no import. The server
    refused to boot — a NameError inside the lifespan — while 1,965 tests passed.
  * `teams_bridge.rail_diagnostic` kept calling `_row_is_unread` after the dead
    detector around it was deleted, and `voice.ensure_unmuted` was moved into
    `voice.py` without `import asyncio`. 2,105 tests and two green CI runs later,
    the first was a 500 on a live endpoint and the second would have raised in the
    middle of a call.

`test_startup_names.py` already guards the startup path, which is why the first
one cannot recur. This is the same idea widened to every module: a name that is
never resolved until the line runs is a crash waiting for the one caller nobody
wrote a test for, and unit tests cannot be relied on to reach every line.

pyflakes rather than a hand-rolled AST walk: scope rules, comprehensions,
walrus, star-imports and `__all__` are all places a naive version gets wrong, and
a guard that is subtly wrong is worse than none.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_module_references_an_undefined_name():
    files = sorted(str(p) for p in (ROOT / "app").rglob("*.py"))
    assert files, "found no application modules to check"
    out = subprocess.run([sys.executable, "-m", "pyflakes", *files],
                         capture_output=True, text=True).stdout
    # Only undefined names. Unused imports and f-string nits do not crash, and a
    # gate that fails on style would be turned off the first time it was noisy.
    bad = [ln for ln in out.splitlines() if "undefined name" in ln]
    assert not bad, "these will raise NameError when the line runs:\n  " + "\n  ".join(bad)
