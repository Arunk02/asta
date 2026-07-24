"""Guard the skills that actually ship in ./skills — a malformed frontmatter means
the catalog silently drops the skill and no brain can load it.
"""

from __future__ import annotations

from app import skills


def test_every_shipped_skill_parses():
    found = skills.discover()
    assert found, "no skills discovered"
    for s in found:
        assert s["name"], s["path"]
        assert s["description"], s["path"]
    names = {s["name"] for s in found}
    assert {"bug-clarification", "workspace-context"} <= names


def test_bug_clarification_orchestrates_the_real_tools():
    """The playbook is only useful if it names tools that exist and stages sends."""
    body = skills.load("bug-clarification") or ""
    for tool in ("resolve_context", "ask_user", "prepare_to_send", "delegate_task"):
        assert tool in body, tool
    # it appears in the always-in-prompt index too (progressive disclosure)
    assert "bug-clarification" in skills.index_block()
