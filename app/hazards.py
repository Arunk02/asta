"""The bugs a reviewer should never have to find in Asta's own code.

17 Nov, a booking-service PR: the first push diffed references through
`Set.of(...).contains(type)` — and `Set.of` throws NullPointerException on a
null lookup. A reference with no type exists in real data. The reviewer found it in
review; the self-review had read the same diff and said nothing.

So the classes he named — null pointers, backward compatibility, Java errors —
are checked twice: as a language-neutral checklist every code pipeline works
through before it commits (agents/solo.md, agents/micro.md), and as this plain
scan of the diff for the Java forms of the same mistakes, which needs no model
and cannot be talked out of what it sees. Business logic cannot be grepped; the
checklist makes the brain say what behaviour changes and prove the old
behaviour still holds.
"""
from __future__ import annotations

import re

_IMMUTABLE = re.compile(r"\b(\w+)\s*=\s*(?:\n\s*)?(?:Set|List|Map)\.of\(")
_TOMAP = re.compile(r"Collectors\.toMap\(")
_GETTER_EQUALS = re.compile(r"\.get\w+\(\)\.equals\(")
_OPTIONAL_GET = re.compile(r"\bOptional\b.*\.get\(\)|\.findFirst\(\)\.get\(\)|\.findAny\(\)\.get\(\)")
_NULL_GUARD = re.compile(r"Objects::nonNull|Objects\.nonNull|!=\s*null|Objects\.equals|"
                         r"Optional\.ofNullable|requireNonNull|StringUtils\.is")
_PUBLIC_SIG = re.compile(r"^\s*public\s+(?:static\s+)?[\w<>\[\], ?]+\s+(\w+)\s*\(")
_ENUM_CONST = re.compile(r"^\s*([A-Z][A-Z0-9_]{2,})\s*(?:\(|,|;)")
_FIELD_NAME = re.compile(r'"name"\s*:\s*"(\w+)"|@JsonProperty\("(\w+)"\)')
_WORKFLOW = re.compile(r"\bWorkflow\.(?:sleep|await|newActivityStub|newChildWorkflowStub|"
                       r"executeActivity|newTimer)\b|@WorkflowMethod|@SignalMethod")


def _files(diff: str) -> list[tuple[str, list[str], list[str]]]:
    """(path, added lines, removed lines) per file of a unified diff."""
    out: list[tuple[str, list[str], list[str]]] = []
    path, added, removed = "", [], []
    for line in (diff or "").splitlines():
        if line.startswith("diff --git "):
            if path:
                out.append((path, added, removed))
            path, added, removed = line.split(" b/", 1)[-1].strip(), [], []
        elif line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
    if path:
        out.append((path, added, removed))
    return out


def scan(diff: str) -> list[str]:
    """Hazards in a diff, one line each with the file. Empty when none are seen."""
    found: list[str] = []
    for path, added, removed in _files(diff):
        if path.endswith(("Test.java", "IT.java")) or "/test/" in path:
            continue
        name = path.rsplit("/", 1)[-1]
        body = "\n".join(added)
        guarded = bool(_NULL_GUARD.search(body))
        for m in _IMMUTABLE.finditer(body):
            const = m.group(1)
            if re.search(rf"\b{re.escape(const)}(?:\.(?:contains|get|containsKey)\(|"
                         rf"::(?:contains|get|containsKey)\b)", body) \
                    and not guarded:
                found.append(f"{name}: {const} is Set/List/Map.of and is looked up with no null "
                             f"guard — .contains(null)/.get(null) throws NullPointerException")
        if _TOMAP.search(body) and not guarded:
            found.append(f"{name}: Collectors.toMap with no null filter — a null value throws "
                         f"NullPointerException")
        if _GETTER_EQUALS.search(body):
            found.append(f"{name}: getX().equals(…) — throws when getX() is null; use Objects.equals")
        if _OPTIONAL_GET.search(body):
            found.append(f"{name}: Optional .get() with no presence check — throws when empty")
        # Backward compatibility: what was public and is gone from this file.
        kept = set()
        for line in added:
            m = _PUBLIC_SIG.match(line)
            if m:
                kept.add(m.group(1))
        for line in removed:
            m = _PUBLIC_SIG.match(line)
            if m and m.group(1) not in kept:
                found.append(f"{name}: public method {m.group(1)}(…) removed or renamed — "
                             f"callers outside this diff break")
        # Avro fields and JSON properties: a name that was there and is not now.
        now = set()
        for line in added:
            m = _FIELD_NAME.search(line)
            if m:
                now.add(m.group(1) or m.group(2))
        for line in removed:
            m = _FIELD_NAME.search(line)
            gone = m.group(1) or m.group(2) if m else ""
            if gone and gone not in now:
                found.append(f"{name}: field '{gone}' removed or renamed — consumers break")
        if name.endswith(".java") and re.search(r"\benum\b", "\n".join(removed + added)):
            now_consts = {m.group(1) for line in added if (m := _ENUM_CONST.match(line))}
            for line in removed:
                m = _ENUM_CONST.match(line)
                if m and m.group(1) not in now_consts and m.group(1) not in body:
                    found.append(f"{name}: enum constant {m.group(1)} removed — stored data and "
                                 f"other services may still send it")
        if name.endswith("WorkflowImpl.java") or ("Workflow" in name and _WORKFLOW.search(body)):
            if "getVersion" not in body:
                found.append(f"{name}: workflow code changed with no Workflow.getVersion — "
                             f"replay of running workflows can break")
    return list(dict.fromkeys(found))
