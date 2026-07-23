"""Workspace setup from the terminal.

    python -m app.workspace detect  ~/some-dir
    python -m app.workspace add     myproj ~/some-dir --repos api,web --jira PROJ
    python -m app.workspace list
    python -m app.workspace resolve myproj "where is the retry policy"
    python -m app.workspace provision myproj
    python -m app.workspace remove   myproj

`detect` is the one to run first: it says which provider would serve the
directory and lists the repos it can see, without registering anything.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import (add, all_workspaces, available_workspaces, detect, enrich, drift,
               provision, remove, resolve_context, update)


def _print_detect(path: str) -> int:
    info = detect(path)
    if not info["ok"]:
        print(f"✗ {info['error']}")
        return 1
    print(f"  path      {info['root']}")
    print(f"  provider  {info['provider']}  ({info['provider_label']})")
    if not info["indexed"]:
        print("            no project-context index found — keyword search only.")
        print("            Bootstrap an index later and it upgrades automatically.")
    print(f"  repos     {len(info['repos'])}")
    for r in info["repos"]:
        print(f"              · {r}")
    if info["repos"]:
        print("\n  register all:")
        print(f"    python -m app.workspace add <name> {info['root']}")
        print("  or just one:")
        print(f"    python -m app.workspace add <name> {info['root']} --repos {info['repos'][0]}")
    return 0


def _print_list() -> int:
    spaces = available_workspaces()
    if not spaces:
        print("No workspaces registered.  python -m app.workspace detect <path>")
        return 0
    for name, info in spaces.items():
        mark = "✓" if info.get("exists") else "✗"
        print(f"{mark} {name}")
        print(f"    root      {info['root']}")
        print(f"    provider  {info.get('provider_label') or info.get('provider')}")
        if info.get("repos") is not None:
            print(f"    repos     {info['repos']}")
        if info.get("indexes"):
            print(f"    indexes   {', '.join(info['indexes'])}")
        if info.get("jira_projects"):
            print(f"    jira      {', '.join(info['jira_projects'])}")
        if info.get("note"):
            print(f"    note      {info['note']}")
        if info.get("error"):
            print(f"    error     {info['error']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.workspace")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("detect", help="inspect a directory without registering it")
    p.add_argument("path")

    p = sub.add_parser("add", help="register a workspace")
    p.add_argument("name")
    p.add_argument("path")
    p.add_argument("--repos", default="", help="comma-separated subset (default: all)")
    p.add_argument("--jira", default="", help="comma-separated Jira project keys")

    sub.add_parser("list", help="show registered workspaces")

    p = sub.add_parser("remove", help="unregister a workspace")
    p.add_argument("name")

    p = sub.add_parser("resolve", help="ask which files answer a task")
    p.add_argument("name")
    p.add_argument("task", nargs="+")

    p = sub.add_parser("provision", help="build/refresh the project-context index")
    p.add_argument("name")

    p = sub.add_parser("drift", help="check whether the index is stale")
    p.add_argument("name")

    args = ap.parse_args(argv)

    try:
        if args.cmd == "detect":
            return _print_detect(args.path)

        if args.cmd == "add":
            repos = [r.strip() for r in args.repos.split(",") if r.strip()]
            jira = [j.strip() for j in args.jira.split(",") if j.strip()]
            ws = add(args.name, args.path, repos=repos, jira_projects=jira)
            info = available_workspaces()[args.name]
            print(f"✓ registered '{ws.name}'")
            print(f"    root      {ws.root}")
            print(f"    provider  {info.get('provider_label') or info.get('provider')}")
            print(f"    repos     {', '.join(repos) if repos else 'all (' + str(info.get('repos', 0)) + ')'}")
            if jira:
                print(f"    jira      {', '.join(jira)} → routes here automatically")
            return 0

        if args.cmd == "list":
            return _print_list()

        if args.cmd == "remove":
            print("✓ removed" if remove(args.name) else f"no workspace '{args.name}'")
            return 0

        if args.cmd == "resolve":
            print(asyncio.run(resolve_context(args.name, " ".join(args.task))))
            return 0

        if args.cmd == "provision":
            print(asyncio.run(provision(args.name)))
            return 0

        if args.cmd == "drift":
            stale, detail = asyncio.run(drift(args.name))
            print(("⚠ stale\n" if stale else "✓ in sync\n") + detail)
            return 0
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
