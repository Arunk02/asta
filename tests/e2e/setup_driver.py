"""One setup step per OS process — so every call after the first is a real restart.

    python -m tests.e2e.setup_driver <db> <portal_url> start|tick|answer|resume <wid> [json]

Prints the workflow's status as JSON. Nothing is shared with the previous
process except the database and the checkpoint file beside it.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


def main(db: str, url: str, cmd: str, wid: str, extra: str = "{}") -> None:
    from app import sandbox, setup_flow, store
    from app.graph import setup
    from tests.e2e.http_portal import HttpPortal
    store.DB_PATH = Path(db)
    store.init()
    setup.PORTALS["http"] = HttpPortal(url)
    arg = json.loads(extra)
    with sandbox.sealed():
        if cmd == "start":
            out = asyncio.run(setup_flow.start(wid, "http", url + "/guide", "DH onboarding", arg))
        elif cmd == "tick":
            asyncio.run(setup_flow.tick_all())
            out = setup_flow.status(wid)
        elif cmd == "answer":
            out = asyncio.run(setup_flow.answer(wid, arg))
        else:
            out = asyncio.run(setup_flow.resume(wid))
    print(json.dumps(out))


if __name__ == "__main__":
    main(*sys.argv[1:])
