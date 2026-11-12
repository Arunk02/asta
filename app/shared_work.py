"""What Arun and one colleague have worked on together — so "that X thing" means it.

12 Nov: a colleague asked whether "that vessel ETA thing for one-way empty" was
merged. He meant a PR Arun had sent him the day before (skip the VTS call and the
vessel ETA check for one-way empty orders, merged). The brief carried only the
day's chat, which was about another PR, so the worker searched PR titles for the
words and found two others — one for "vessel ETA", one for "one-way empty" —
and still drafted "merged". His one-line clarification changed nothing.

A person's "that X thing" points at something THEY know: the work in their 1:1
chat with him and the tasks that name them. That set is small, so it is read
first; a PR found only by searching titles for their words is a guess, and a
guess is never told to them as "merged".

    rows(chat, who)          the shared items, newest first, ≤ MAX_ROWS
    resolve(text, rows)      which of them their words mean (one, several, none)
    block(...)               the SHARED WORK section of a colleague-ask brief
    check_draft(...)         a staged reply that asserts state on a guess → a question
"""
from __future__ import annotations

import json
import re
import time

from . import prname, store

WINDOW_SECONDS = 14 * 86400
MAX_ROWS = 10
#: PRs looked up with gh per brief, for links nothing local knows the title of.
MAX_LOOKUPS = 4
#: An open PR's state is re-read after this; merged/closed never change.
META_TTL_SECONDS = 3600

_PR_LINK = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", re.I)
_PR_WORD = re.compile(r"(?:\bPR\s*#?\s*|/pull/)(\d{2,6})\b", re.I)
_TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,14}-\d+\b")

#: "that vessel ETA thing", "the OWE one", "that fix" — a pointer, not a name.
_VAGUE = re.compile(
    r"\b(?:that|the|this|same)\b[^.?!\n]{0,60}?\b(?:thing|one|stuff|change|changes|fix|"
    r"pr|story|feature|work|ticket|item|issue)\b", re.I)

_STOP = frozenset("""
a an and are as at be been bro bhai da did do does done for from got had has have hi
hello how i in is it its just k kindly me merge merged my na naa no not now of ok okay on
one or our pls please pr pull request right so status still that the their them then there
these thing this those to u update ur us was we were what when where which who why will
with yeah yes you your sir ji anna dude man stuff change changes fix story feature work
ticket item issue same can could would should know tell any already yet also
clarified added said they he she asked
""".split())


# --- the shared items ------------------------------------------------------------

def _first_name(who: str) -> str:
    first = (who or "").strip().split()[0].lower() if (who or "").strip() else ""
    return first if len(first) >= 3 else ""


def _names(who: str, text: str) -> bool:
    first = _first_name(who)
    return bool(first) and re.search(rf"\b{re.escape(first)}\b", (text or "").lower()) is not None


def _meta_key(owner: str, repo: str, number: str) -> str:
    return f"pr_meta:{owner}/{repo}#{number}".lower()


def _cached_meta(owner: str, repo: str, number: str, now: float) -> dict | None:
    try:
        d = json.loads(store.kv_get(_meta_key(owner, repo, number)) or "{}")
    except ValueError:
        return None
    if not d:
        return None
    if d.get("state") == "OPEN" and now - float(d.get("at") or 0) > META_TTL_SECONDS:
        return None
    return d


def pr_meta(owner: str, repo: str, number: str) -> dict:
    """Title and state of one PR, read with the office gh. {} when gh cannot say."""
    import subprocess
    try:
        out = subprocess.run(
            [prname.GH, "pr", "view", str(number), "-R", f"{owner}/{repo}", "--json",
             "title,state"], capture_output=True, text=True, timeout=8, check=False)
        d = json.loads(out.stdout or "{}") if out.returncode == 0 else {}
    except Exception:                                           # noqa: BLE001
        return {}
    return {"title": str(d.get("title") or ""), "state": str(d.get("state") or "")} if d else {}


def _remember_meta(owner: str, repo: str, number: str, d: dict, now: float) -> None:
    store.kv_set(_meta_key(owner, repo, number),
                 json.dumps({"title": d.get("title", ""), "state": d.get("state", ""), "at": now}))


def _task_prs(t: dict) -> list[tuple[str, str, str]]:
    return [(m.group(1), m.group(2), m.group(3)) for m in _PR_LINK.finditer(t.get("pr_urls") or "")]


def _is_their_question(task_id: int) -> bool:
    """An investigation of something they ASKED is not work shared with them."""
    return bool(store.kv_get(f"responder_task:{task_id}"))


def rows(chat: str, who: str, now: float | None = None, lookups: int = MAX_LOOKUPS) -> list[dict]:
    """The PRs linked in their 1:1 chat and the tasks naming them, last 14 days.

    Each row: label ("booking PR 2101" or "task 12"), title, state, url, task
    (id or 0), at (newest sighting). Titles and states come from the task table
    and a small cache; gh is asked only for a link nothing local knows."""
    now = time.time() if now is None else now
    since = now - WINDOW_SECONDS
    found: dict[str, dict] = {}

    def _pr(owner: str, repo: str, number: str, at: float, **known) -> None:
        key = f"{owner}/{repo}#{number}".lower()
        row = found.setdefault(key, {"label": prname.label(repo, number), "title": "",
                                     "state": "", "task": 0, "at": 0.0,
                                     "url": f"https://github.com/{owner}/{repo}/pull/{number}",
                                     "_ref": (owner, repo, number)})
        row["at"] = max(row["at"], at)
        for k, v in known.items():
            if v and not row.get(k):
                row[k] = v

    chat_l = (chat or "").strip().lower()
    for t in store.list_tasks(limit=300):
        created = float(t.get("created_at") or 0)
        if created < since or _is_their_question(t["id"]):
            continue
        mine = chat_l and (t.get("teams_chat") or "").strip().lower() == chat_l
        if not (mine or _names(who, f"{t.get('title') or ''}\n{(t.get('prompt') or '')[:800]}")):
            continue
        prs = _task_prs(t)
        for owner, repo, number in prs:
            _pr(owner, repo, number, created, title=(t.get("title") or "")[:120],
                state=(t.get("pr_state") or "").upper(), task=t["id"])
        if not prs:
            found[f"task:{t['id']}"] = {"label": f"task {t['id']}", "url": f"task {t['id']}",
                                        "title": (t.get("title") or "")[:120],
                                        "state": t.get("status") or "", "task": t["id"],
                                        "at": created}
    if chat:
        for r in store.teams_messages(chat=chat, since=since, limit=600):
            for m in _PR_LINK.finditer(r.get("text") or ""):
                _pr(m.group(1), m.group(2), m.group(3), float(r.get("sent_at") or 0))
    for row in found.values():
        ref = row.pop("_ref", None)
        if not ref or (row["title"] and row["state"]):
            continue
        d = _cached_meta(*ref, now)
        if d is None and lookups > 0:
            lookups -= 1
            d = pr_meta(*ref)
            if d:
                _remember_meta(*ref, d, now)
        if d:
            row["title"] = row["title"] or d.get("title", "")
            row["state"] = row["state"] or d.get("state", "")
    ordered = sorted(found.values(), key=lambda r: (bool(r["title"]), r["at"]), reverse=True)
    return ordered[:MAX_ROWS]


# --- which one their words mean --------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"\bone[\s-]+way\b", "oneway", (text or "").lower())


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _norm(text))


def terms(text: str, drop: tuple[str, ...] = ()) -> list[str]:
    """The content words of what they said — the X in "that X thing"."""
    out = []
    for w in _words(text):
        if len(w) < 2 or w in _STOP or w.isdigit() or w in out or w in drop:
            continue
        out.append(w)
    return out


def _acronyms(text: str) -> set[str]:
    return {a.lower() for a in re.findall(r"\b[A-Z]{2,5}\b", text or "")}


def _initials(title: str, size: int) -> set[str]:
    # "one-way empty" → one, way, empty → "owe"
    toks = re.findall(r"[a-z0-9]+", (title or "").lower())
    return {"".join(t[0] for t in toks[i:i + size]) for i in range(len(toks) - size + 1)}


def _score(qterms: list[str], acronyms: set[str], title: str) -> int:
    words = set(_words(title))
    n = 0
    for q in qterms:
        if q in words or (q in acronyms and q in _initials(title, len(q))):
            n += 1
    return n


def explicit(text: str) -> bool:
    """They named the thing — a PR link or number, a ticket, a booking."""
    from . import booking_case
    t = text or ""
    return bool(_PR_LINK.search(t) or _PR_WORD.search(t) or _TICKET.search(t)
                or booking_case.ids(t))


def vague(text: str) -> bool:
    """"that X thing" / "the X one" with nothing named."""
    return bool(_VAGUE.search(text or "")) and not explicit(text)


def resolve(text: str, shared: list[dict], who: str = "") -> dict:
    """{"resolved": row|None, "candidates": [rows], "conf": 0..1}.

    One row clearly fits (most of their words, and more than any other) →
    resolved. Several fit equally → candidates, no resolution. Nothing fits →
    neither; whatever a search turns up is a guess."""
    q = terms(text, tuple(re.findall(r"[a-z]+", (who or "").lower())))
    acr = _acronyms(text)
    if not q or not shared:
        return {"resolved": None, "candidates": [], "conf": 0.0}
    scored = sorted(((_score(q, acr, r.get("title") or ""), i, r) for i, r in enumerate(shared)),
                    key=lambda x: (-x[0], x[1]))
    top = scored[0][0]
    if top == 0:
        return {"resolved": None, "candidates": [], "conf": 0.0}
    tops = [r for s, _, r in scored if s == top]
    second = next((s for s, _, _ in scored if s < top), 0)
    enough = max(1, min(len(q), 2), -(-len(q) * 3 // 5))           # ≥ 60% of their words
    conf = round(top / len(q), 2)
    if len(tops) == 1 and top >= enough and top > second:
        return {"resolved": tops[0], "candidates": tops, "conf": conf}
    near = [r for s, _, r in scored if s >= max(1, top - 1)][:3]
    return {"resolved": None, "candidates": near if len(near) > 1 else tops, "conf": min(conf, 0.4)}


def _short(row: dict, limit: int = 60) -> str:
    title = " ".join((row.get("title") or "").split())
    if len(title) > limit:
        title = title[:limit].rsplit(" ", 1)[0] + "…"
    return f"{row['label']} ({title})" if title else row["label"]


def question(candidates: list[dict]) -> str:
    """One short question naming only the shared items that could fit."""
    if not candidates:
        return "Which one do you mean — can you share the PR link?"
    if len(candidates) == 1:
        return f"Just to check — do you mean {_short(candidates[0])}?"
    named = [_short(c, 48) for c in candidates[:3]]
    return f"Just to check, which one do you mean — {', '.join(named[:-1])} or {named[-1]}?"


def must_ask(text: str, res: dict) -> bool:
    """A vague pointer that resolves to more than one shared item: ask, don't work."""
    return vague(text) and not res.get("resolved") and len(res.get("candidates") or []) >= 2


# --- the brief -------------------------------------------------------------------

def block(who: str, shared: list[dict], res: dict, text: str) -> str:
    first = (who or "they").split()[0]
    if not shared:
        if not vague(text):
            return ""
        return ("\n\nSHARED WORK: nothing linked between Arun and {0} in the last 14 days. "
                "Their words point at something (\"that X thing\") without naming it: a PR you "
                "find only by searching titles for their words is a GUESS. Never tell them it is "
                "merged, deployed or done on a guess — the REPLY is one short question asking "
                "which one they mean.").format(first)
    lines = [f"\n\nSHARED WORK — what Arun and {first} worked on in the last 14 days (PRs "
             f"linked in their 1:1 chat, Asta tasks naming them; state as last known — "
             f"confirm with gh before stating it):"]
    for r in shared:
        state = f" — {r['state']}" if r.get("state") else ""
        task = f" (task {r['task']})" if r.get("task") and not r["label"].startswith("task") else ""
        lines.append(f"- {r['label']} — {r.get('title') or '(title unknown)'}{state}{task}")
    lines.append(f"\"That X thing\" / \"the X one\" means an item above FIRST. A PR you find only "
                 f"by searching titles for their words is a GUESS: never tell {first} it is "
                 f"merged, deployed or done on a guess.")
    if res.get("resolved"):
        lines.append(f"Their words most likely mean {_short(res['resolved'], 90)}. Answer about "
                     f"it; name it in the REPLY so they can correct you.")
    elif len(res.get("candidates") or []) >= 2:
        lines.append("Their words fit more than one item above: "
                     + "; ".join(_short(c, 70) for c in res["candidates"])
                     + ". The REPLY is one short question asking which one — nothing else.")
    elif vague(text):
        lines.append("Their words fit nothing above. If you are not sure which PR they mean, the "
                     "REPLY is one short question asking which one, naming what you found.")
    return "\n".join(lines)


def remember(task_id: int, text: str, res: dict) -> None:
    store.kv_set(f"shared_work:{task_id}", json.dumps({
        "vague": vague(text), "explicit": explicit(text), "conf": res.get("conf", 0.0),
        "resolved": (res.get("resolved") or {}).get("url", ""),
        "candidates": [{"label": c["label"], "title": c.get("title", ""), "url": c["url"]}
                       for c in res.get("candidates") or []]}))


def for_brief(chat: str, who: str, text: str) -> tuple[str, dict]:
    """The brief block and the resolution, for one colleague ask."""
    shared = rows(chat, who)
    res = resolve(text, shared, who)
    return block(who, shared, res, text), res


# --- the staged draft --------------------------------------------------------------

_ASSERTS_STATE = re.compile(
    r"\b(?:merged|deployed|released|went\s+in|gone\s+in|is\s+(?:already\s+)?in\s+"
    r"(?:develop|main|master|prod|production|uat|preprod)|already\s+in|is\s+live)\b", re.I)


def check_draft(task_id: int, analysis: str, reply: str) -> str:
    """A reply that states a PR's state on a guess, replaced by a question. '' = fine.

    A guess: they pointed vaguely, and the PR the draft is about is not the one
    their words resolved to among the shared items."""
    try:
        info = json.loads(store.kv_get(f"shared_work:{task_id}") or "{}")
    except ValueError:
        return ""
    if not info or not info.get("vague") or info.get("explicit"):
        return ""
    r = " ".join((reply or "").split())
    if not r or not _ASSERTS_STATE.search(r) or r.endswith("?"):
        return ""
    resolved = info.get("resolved") or ""
    if resolved:
        number = resolved.rstrip("/").rsplit("/", 1)[-1]
        in_reply, in_analysis = set(_PR_WORD.findall(reply)), set(_PR_WORD.findall(analysis or ""))
        if number in in_reply or (not in_reply and (not in_analysis or number in in_analysis)):
            return ""
    q = question(info.get("candidates") or [])
    store.record_outcome("answer", "guess replaced", subject=str(task_id),
                         detail=f"{r[:120]} → {q[:120]}")
    return q
