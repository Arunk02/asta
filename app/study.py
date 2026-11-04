"""A learning session he runs: a topic, objectives, exercises, checked answers.

Opening a page about a topic is not learning it. A session here holds what he
set out to learn, exercises written from a named source, and every answer he
gave with whether it held. It survives a restart because it lives in the store,
and it never becomes an instruction to any brain: nothing here is fed to the
skill catalog, memory or guardrails.

An answer is checked against the source as it reads NOW, not against what a
model remembers. An exercise carries the terms a right answer must contain, and
each term must appear in its source:
  - when the exercise is written, or it is refused (a key the source does not
    support is the model's opinion, not the material);
  - again when an answer is checked. If the source has gone, or no longer
    contains a term, the answer is "unverifiable", never passed or failed.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from . import store

KEY = "study_sessions"
MAX_SOURCE_CHARS = 400_000


def _load() -> list[dict]:
    try:
        rows = json.loads(store.kv_get(KEY) or "[]")
    except (ValueError, TypeError):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("id")]


def _save(rows: list[dict]) -> None:
    store.kv_set(KEY, json.dumps(rows))


def _get(rows: list[dict], session_id: int) -> dict:
    for r in rows:
        if r["id"] == int(session_id):
            return r
    raise ValueError(f"No learning session #{session_id}.")


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+(?:[._'-][a-z0-9]+)*", (text or "").lower()))


def _has(haystack: str, term: str) -> bool:
    t = _norm(term)
    return bool(t) and f" {t} " in f" {haystack} "


def _source(workspace: str, rel_path: str) -> str:
    """The source as it reads now: a file in a registered workspace, never outside it."""
    from .workspace import _root
    root = _root(workspace).resolve()
    path = (root / rel_path).resolve()
    if path != root and root not in path.parents:
        raise ValueError("Path escapes the workspace root.")
    if not path.is_file():
        raise ValueError(f"Not a file: {rel_path}")
    return path.read_text(errors="replace")[:MAX_SOURCE_CHARS]


def start(topic: str, objectives: list[str]) -> dict:
    objectives = [o.strip()[:200] for o in objectives or [] if o and o.strip()]
    if not (topic or "").strip() or not objectives:
        raise ValueError("a learning session needs a topic and at least one objective")
    rows = _load()
    now = time.time()
    row = {"id": max([r["id"] for r in rows], default=0) + 1, "topic": topic.strip()[:200],
           "objectives": objectives, "exercises": [], "status": "active",
           "created_at": now, "updated_at": now}
    rows.append(row)
    _save(rows)
    return row


def add_exercise(session_id: int, objective: int, question: str, answer_terms: list[str],
                 workspace: str, source_path: str) -> dict:
    """An exercise whose key the source supports, or a refusal saying which term it lacks."""
    rows = _load()
    s = _get(rows, session_id)
    if not 1 <= int(objective) <= len(s["objectives"]):
        raise ValueError(f"objective must be 1–{len(s['objectives'])}")
    terms = [t.strip() for t in answer_terms or [] if t and t.strip()]
    if not (question or "").strip() or not terms:
        raise ValueError("an exercise needs a question and the terms a right answer contains")
    text = _source(workspace, source_path)
    seen = _norm(text)
    absent = [t for t in terms if not _has(seen, t)]
    if absent:
        raise ValueError(f"{source_path} does not contain {', '.join(absent)}: the key must come "
                         "from the source")
    ex = {"id": len(s["exercises"]) + 1, "objective": int(objective),
          "question": question.strip()[:500], "terms": terms, "workspace": workspace,
          "source": source_path, "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
          "read_at": time.time(), "attempts": []}
    s["exercises"].append(ex)
    s["updated_at"] = time.time()
    _save(rows)
    return ex


def answer(session_id: int, exercise_id: int, text: str) -> dict:
    """Check one answer against the source as it reads now, and keep the attempt."""
    rows = _load()
    s = _get(rows, session_id)
    ex = next((e for e in s["exercises"] if e["id"] == int(exercise_id)), None)
    if ex is None:
        raise ValueError(f"No exercise #{exercise_id} in session #{session_id}.")
    attempt = {"at": time.time(), "answer": (text or "").strip()[:1000]}
    try:
        source = _source(ex["workspace"], ex["source"])
    except ValueError as exc:
        attempt.update(result="unverifiable", why=f"source unavailable: {exc}")
    else:
        now_sha = hashlib.sha256(source.encode()).hexdigest()
        gone = [t for t in ex["terms"] if not _has(_norm(source), t)]
        if gone:
            attempt.update(result="unverifiable",
                           why=f"the source changed and no longer contains {', '.join(gone)}")
        else:
            said = _norm(text)
            missing = [t for t in ex["terms"] if not _has(said, t)]
            attempt.update(result="failed" if missing else "passed", missing=missing,
                           source_changed=now_sha != ex["source_sha256"])
        attempt["source_sha256"] = now_sha
    ex["attempts"].append(attempt)
    s["updated_at"] = time.time()
    if all(any(a["result"] == "passed" for e in s["exercises"] if e["objective"] == n + 1
               for a in e["attempts"]) for n in range(len(s["objectives"]))):
        s["status"] = "complete"
    _save(rows)
    return attempt


def progress(session_id: int) -> dict:
    s = _get(_load(), session_id)
    objectives = []
    for n, goal in enumerate(s["objectives"], 1):
        exs = [e for e in s["exercises"] if e["objective"] == n]
        objectives.append({"objective": goal, "exercises": len(exs),
                           "passed": sum(any(a["result"] == "passed" for a in e["attempts"])
                                         for e in exs),
                           "met": any(a["result"] == "passed" for e in exs for a in e["attempts"])})
    return {"id": s["id"], "topic": s["topic"], "status": s["status"], "objectives": objectives,
            "attempts": sum(len(e["attempts"]) for e in s["exercises"])}


def sessions() -> list[dict]:
    return [{"id": r["id"], "topic": r["topic"], "status": r["status"]} for r in _load()]
