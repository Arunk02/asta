"""Every name the startup handler uses must actually exist.

This is written from a real outage of my own making. A one-line addition to the
startup handler called `meetings.warm_the_voice()` — and `meetings` is not in
main's import list. The result was a NameError raised inside FastAPI's lifespan,
so the application refused to boot at all: not a degraded server, no server.

The whole test suite was green while that was true, and stayed green, because
nothing anywhere exercises the startup handler. 1,965 passing tests and the thing
would not start.

Running the real handler in a test is not the answer — it launches a dozen
supervised loops, a browser and an MCP stack. What IS checkable, statically and
in milliseconds, is the property that actually broke: a module-qualified name
used in startup must be imported somewhere it can see.
"""

from __future__ import annotations

import ast
from pathlib import Path

MAIN = Path(__file__).resolve().parent.parent / "app" / "main.py"

#: Builtins and locals that are legitimately not imports.
_IGNORE = {"self", "app", "request", "os", "asyncio", "contextlib", "json", "re",
           "time", "secrets", "httpx", "print", "len", "str", "int", "float",
           "dict", "list", "set", "tuple", "bool", "range", "open", "getattr",
           "setattr", "hasattr", "isinstance", "Exception", "RuntimeError",
           "ValueError", "OSError", "TypeError", "KeyError", "super", "type"}


def _module_level_names(tree: ast.Module) -> set[str]:
    """Everything importable or defined at module scope in main.py."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


def _local_names(fn: ast.AST) -> set[str]:
    """Names bound INSIDE the function — local imports, assignments, args."""
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
            for a in node.args.args + node.args.kwonlyargs:
                names.add(a.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.comprehension,)):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def _startup_functions(tree: ast.Module):
    """The handlers FastAPI calls on boot — where a NameError is fatal."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in ("startup", "shutdown") or any(
                    "on_event" in ast.dump(d) or "lifespan" in ast.dump(d)
                    for d in node.decorator_list):
                yield node


def test_startup_uses_no_name_it_cannot_see():
    tree = ast.parse(MAIN.read_text())
    available = _module_level_names(tree) | _IGNORE
    problems: list[str] = []
    for fn in _startup_functions(tree):
        known = available | _local_names(fn)
        for node in ast.walk(fn):
            # `meetings.warm_the_voice()` — the shape that broke the server.
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                name = node.value.id
                if name not in known:
                    problems.append(
                        f"{fn.name}() line {node.lineno}: '{name}.{node.attr}' — "
                        f"'{name}' is not imported anywhere it can see")
    assert not problems, (
        "the server will not boot:\n  " + "\n  ".join(problems))


def test_the_check_would_actually_catch_it():
    """A linter that cannot fail is decoration. This plants the exact bug that
    took the server down and proves the check sees it."""
    broken = ast.parse(
        "from . import daemon\n"
        "@app.on_event('startup')\n"
        "async def startup():\n"
        "    meetings.warm_the_voice()\n")
    available = _module_level_names(broken) | _IGNORE
    caught = []
    for fn in _startup_functions(broken):
        known = available | _local_names(fn)
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id not in known:
                    caught.append(node.value.id)
    assert "meetings" in caught, "the check does not see the bug it was written for"


def test_a_local_import_satisfies_it():
    """The fix that was actually applied must read as correct to the check."""
    fixed = ast.parse(
        "from . import daemon\n"
        "@app.on_event('startup')\n"
        "async def startup():\n"
        "    from . import meetings as _meetings\n"
        "    _meetings.warm_the_voice()\n")
    available = _module_level_names(fixed) | _IGNORE
    for fn in _startup_functions(fixed):
        known = available | _local_names(fn)
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert node.value.id in known, f"false positive on {node.value.id}"
