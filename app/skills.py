"""Skills: reusable expert playbooks, loaded on demand to keep the prompt lean.

Token design (mirrors Claude Code's skill architecture):
- Only NAME + one-line description of each skill sit in the system prompt (~30 tokens/skill).
- The full playbook enters the conversation only when the agent calls load_skill(name),
  and only once per conversation (the tool result stays in history from then on).

Sources, in order:
- asta/skills/*.md (files or symlinks — symlink a repo's SKILL.md to stay in sync)
- asta/skills/*/SKILL.md
- extra paths in ASTA_SKILL_PATHS (colon-separated files or dirs)
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"

_FRONT_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _parse(path: Path) -> dict | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    name, desc, source = path.parent.name if path.name == "SKILL.md" else path.stem, "", ""
    m = _FRONT_RE.match(text)
    if m:
        front = m.group(1)
        sm = re.search(r"^source:\s*(\S+)", front, re.MULTILINE)
        if sm:
            source = sm.group(1).strip()
        nm = re.search(r"^name:\s*(.+)$", front, re.MULTILINE)
        if nm:
            name = nm.group(1).strip()
        dm = re.search(r"^description:\s*>-?\n((?:\s+.+\n?)+)|^description:\s*(.+)$", front, re.MULTILINE)
        if dm:
            desc = " ".join((dm.group(1) or dm.group(2) or "").split())
    body = _FRONT_RE.sub("", text).strip()
    # `source` is set on what Asta learned (extracted, teacher, correction,
    # evolution); a hand-written playbook has none.
    return {"name": name, "description": desc[:300], "body": body, "path": str(path),
            "source": source}


def discover() -> list[dict]:
    SKILLS_DIR.mkdir(exist_ok=True)
    paths: list[Path] = []
    for p in sorted(SKILLS_DIR.iterdir()):
        if p.name.lower() == "readme.md":
            continue                       # the dir's own docs, not a skill — was riding
                                           # the catalog as an empty-description entry
        if p.suffix == ".md" and (p.is_file() or p.is_symlink()):
            paths.append(p)
        elif p.is_dir() and (p / "SKILL.md").exists():
            paths.append(p / "SKILL.md")
    for extra in os.environ.get("ASTA_SKILL_PATHS", "").split(":"):
        if not extra.strip():
            continue
        ep = Path(extra.strip()).expanduser()
        if ep.is_dir():
            paths += sorted(ep.glob("*/SKILL.md")) + sorted(ep.glob("*.md"))
        elif ep.is_file():
            paths.append(ep)
    skills, seen = [], set()
    for p in paths:
        s = _parse(p)
        if s and s["name"] not in seen:
            seen.add(s["name"])
            skills.append(s)
    return skills


def index_block() -> str:
    """The cheap always-in-prompt part: names + descriptions only."""
    skills = discover()
    if not skills:
        return ""
    lines = [f"- **{s['name']}** — {s['description']}" for s in skills]
    return (
        "## Skills (playbooks — load before use)\n"
        "Call load_skill(name) ONCE per conversation before working in that skill's area, then "
        "follow it strictly:\n" + "\n".join(lines)
    )


#: Playbooks that live in Asta's code rather than in skills/: the on-demand
#: chat keeps them out of its prompt and loads them like any other.
def _builtin() -> dict[str, str]:
    from . import copilot_cli
    return {"code-work": copilot_cli.CODE_WORK_PLAYBOOK}


def load(name: str) -> str | None:
    for s in discover():
        if s["name"] == name:
            return s["body"]
    return _builtin().get(name)
