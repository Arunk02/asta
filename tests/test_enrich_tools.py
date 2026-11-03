"""The refresh brief names index tools that exist and commands that run.

3 Nov: the live booking refresh stopped and asked where generate-indexes.js
was. The contract names it, but the run works inside the workspace, and the
scripts live in Asta's checkout. It also read the contract's maintainer header
as an instruction to run a script that isn't there."""
from __future__ import annotations

import json
import shlex
import shutil
import subprocess

import pytest

from app import refresh


def test_every_tool_the_brief_names_exists(tmp_path):
    block = refresh.index_tools(tmp_path, ".asta-context")
    for name in ("generate-indexes.js", "generate-symbols.js", "reconcile-router.js",
                 "validate-indexes.js"):
        assert str(refresh.INDEX_TOOLS / name) in block
        assert (refresh.INDEX_TOOLS / name).is_file()
    assert block.index("generate-indexes.js") < block.index("generate-symbols.js") \
        < block.index("reconcile-router.js") < block.index("validate-indexes.js")
    assert "ASTA_CONTEXT_DIR=.asta-context" in block and "resolve-task.js" in block


def test_the_contract_loses_its_maintainer_header_and_nothing_else():
    text = refresh._contract()
    assert "build_enrich.py" not in text and not text.lstrip().startswith("<!--")
    assert text.strip() and text.strip() in refresh.ENRICH_CONTRACT.read_text()


def test_the_brief_says_it_is_headless():
    assert "HEADLESS" in refresh.ENRICH_BRIEF and "Never stop to ask" in refresh.ENRICH_BRIEF


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_symbols_command_runs_as_written(tmp_path):
    ctx = tmp_path / ".asta-context"
    ctx.mkdir()
    (ctx / "_global_index.json").write_text(json.dumps({"files": []}))
    line = next(l.strip() for l in refresh.index_tools(tmp_path, ".asta-context").splitlines()
                if "generate-symbols.js" in l)
    env_part, cmd = line.split(" ", 1)
    key, value = env_part.split("=", 1)
    argv = [a for a in shlex.split(cmd) if a != "--write"]          # dry run: writes nothing
    assert argv[0] == "node"
    argv[0] = shutil.which("node")                                   # wherever this machine keeps it
    import os
    out = subprocess.run(argv, capture_output=True, text=True, env={**os.environ, key: value})
    assert out.returncode == 0, out.stderr
    assert not (ctx / "_symbols.json").exists()
