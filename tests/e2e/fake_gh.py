"""A stand-in `gh`, run as a real executable, answering from a real git remote.

Heads, diffs and changed files come from the bare repository itself; a merge
is a real squash merge pushed to it. CI and review state live in a JSON file
the test edits, with checks keyed by commit — CI on the old head is not CI on
the new one. Every invocation is logged, so a test reads exactly what Asta ran.

GitHub's own refusals are kept: a merge whose --match-head-commit is not the
current head fails, and an author cannot approve their own pull request.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time

STATE = os.environ["FAKE_GH_STATE"]


def load() -> dict:
    with open(STATE) as f:
        return json.load(f)


def save(state: dict) -> None:
    with open(STATE, "w") as f:
        json.dump(state, f, indent=1)


def git(*args: str, cwd: str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def head_of(state: dict, pr: dict) -> str:
    return git("--git-dir", state["origin"], "rev-parse", f"refs/heads/{pr['head']}")


def fail(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.exit(1)


def number_of(ref: str) -> str:
    m = re.search(r"(\d+)$", ref.rstrip("/"))
    if not m:
        fail(f"no pull requests found for {ref}")
    return m.group(1)


def view(state: dict, num: str, fields: list[str]) -> dict:
    pr = state["prs"].get(num) or fail(f"no pull requests found for #{num}")
    sha = head_of(state, pr)
    checks = pr.get("checks", {}).get(sha) or [{"name": "tests", "status": "IN_PROGRESS"}]
    green = all(c.get("conclusion") == "SUCCESS" for c in checks)
    names = git("--git-dir", state["origin"], "diff", "--name-only",
                f"{pr['base']}...{pr['head']}").splitlines()
    owner, repo = pr["repo"].split("/")
    full = {"number": int(num), "title": pr["title"], "state": pr.get("state", "OPEN"),
            "isDraft": False, "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN" if green else "BLOCKED",
            "reviewDecision": pr.get("review", "APPROVED"), "statusCheckRollup": checks,
            "headRefName": pr["head"], "headRefOid": sha, "baseRefName": pr["base"],
            "url": f"https://github.com/{pr['repo']}/pull/{num}", "mergedAt": pr.get("mergedAt"),
            "author": {"login": pr["author"]}, "body": pr.get("body", ""),
            "additions": 1, "deletions": 0, "changedFiles": len(names),
            "files": [{"path": n, "additions": 1, "deletions": 0} for n in names],
            "headRepositoryOwner": {"login": owner}, "headRepository": {"name": repo},
            "reviews": pr.get("reviews_list", []), "comments": [], "createdAt": "2026-10-28T00:00:00Z"}
    return {k: full[k] for k in fields if k in full}


def merge(state: dict, num: str, argv: list[str]) -> None:
    pr = state["prs"][num]
    pinned = argv[argv.index("--match-head-commit") + 1] if "--match-head-commit" in argv else ""
    if pinned != head_of(state, pr):
        fail("GraphQL: Head branch was modified. Review and try the merge again. (mergePullRequest)")
    work = tempfile.mkdtemp()
    git("clone", "-q", state["origin"], work)
    git("checkout", "-q", pr["base"], cwd=work)
    git("merge", "-q", "--squash", f"origin/{pr['head']}", cwd=work)
    git("-c", "user.email=gh@example.test", "-c", "user.name=gh", "commit", "-qm",
        f"{pr['title']} (#{num})", cwd=work)
    git("push", "-q", "origin", pr["base"], cwd=work)
    pr.update(state="MERGED", mergedAt="2026-10-28T12:00:00Z")
    state.setdefault("merges", []).append({"pr": num, "head": pinned})


def main(argv: list[str]) -> None:
    state = load()
    state.setdefault("log", []).append(argv)
    save(state)
    if argv[:2] == ["auth", "status"]:
        print(f"github.com\n  ✓ Logged in to github.com account {state['viewer']} (keyring)")
        return
    if argv[:2] == ["api", "user"]:
        print(state["viewer"])
        return
    if argv[0] == "api" and "--method" in argv and argv[argv.index("--method") + 1] == "POST":
        path = next(a for a in argv if a.startswith("repos/"))
        num = re.search(r"/pulls/(\d+)/reviews", path).group(1)
        body = json.loads(sys.stdin.read() or "{}")
        pr = state["prs"][num]
        if body.get("event") == "APPROVE" and pr["author"].lower() == state["viewer"].lower():
            fail("Review Can not approve your own pull request")
        state.setdefault("reviews", []).append({"pr": num, "event": body.get("event"),
                                                "comments": len(body.get("comments") or []),
                                                "head": head_of(state, pr)})
        save(state)
        print("{}")
        return
    if argv[0] == "api":
        print("[]")
        return
    if argv[0] != "pr":
        fail(f"fake gh: unsupported {argv}")
    sub, ref = argv[1], argv[2]
    num = number_of(ref)
    if sub == "view":
        fields = argv[argv.index("--json") + 1].split(",")
        print(json.dumps(view(state, num, fields)))
    elif sub == "diff":
        pr = state["prs"][num]
        print(git("--git-dir", state["origin"], "diff", f"{pr['base']}...{pr['head']}"))
    elif sub == "checks":
        pr = state["prs"][num]
        checks = pr.get("checks", {}).get(head_of(state, pr)) or []
        print(json.dumps([{"name": c["name"], "state": c.get("conclusion") or "PENDING"}
                          for c in checks]))
    elif sub == "merge":
        merge(state, num, argv)
        save(state)
        print(f"✓ Squashed and merged pull request #{num}")
    else:
        fail(f"fake gh: unsupported pr {sub}")


if __name__ == "__main__":
    main(sys.argv[1:])
