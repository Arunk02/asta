"""Run the real Asta API in its own process, on its own database, sealed.

    python -m tests.e2e.harness <port> <db_path>

Lifespan is off: the startup hook launches the chat watchers, the Teams browser
and the notification pushers, none of which belong in a test. What is served is
the same FastAPI app and the same route code. `sandbox.sealed()` turns every
outward function (Teams sends and calls, phone pushes, Jira writes) into a
tripwire for the life of the process.
"""
from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def serve(port: int, db_path: str) -> None:
    import uvicorn

    from app import memory, sandbox, store
    store.DB_PATH = Path(db_path)
    store.init()
    # Memory lives in its own folder beside the database, never the checkout's data/.
    root = Path(db_path).with_name("memory")
    memory.MEMORY_DIR, memory.FACTS_DIR = root, root / "facts"
    memory.EPISODES_DIR, memory.INDEX_FILE = root / "episodes", root / "MEMORY.md"
    memory.local_embed = lambda *a, **k: None          # no local model in a test
    memory.ensure_dirs()
    from app import main
    with sandbox.sealed():
        uvicorn.run(main.app, host="127.0.0.1", port=port, lifespan="off", log_level="warning")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def clean_env(**extra: str) -> dict:
    """No inherited secrets, no live bridge: only what the process needs."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(ROOT),
           "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
           "HOME": os.environ.get("HOME", "/tmp"), "WA_BRIDGE_URL": "http://127.0.0.1:9",
           "ASTA_TOKEN": "e2e-owner-token"}
    env.update(extra)
    return env


@contextlib.contextmanager
def server(db_path: Path):
    """The real API on a free port. Yields its base URL; stops it afterwards."""
    import httpx
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "tests.e2e.harness", str(port), str(db_path)],
                            cwd=str(ROOT), env=clean_env(), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while True:
            if proc.poll() is not None:
                raise RuntimeError("server exited: " + proc.stdout.read().decode()[-2000:])
            try:
                httpx.get(url + "/api/_invoke", timeout=1)
                break
            except httpx.HTTPError:
                if time.time() > deadline:
                    raise RuntimeError("server did not come up")
                time.sleep(0.2)
        yield url
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(5)
        if proc.poll() is None:
            proc.kill()


if __name__ == "__main__":
    serve(int(sys.argv[1]), sys.argv[2])
