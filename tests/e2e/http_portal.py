"""A setup portal served over real HTTP, and the adapter that talks to it.

The portal is a threaded HTTP server holding its own state (requests, access,
what is set up). Its failures are real network behaviour: a submit that
creates the request and then answers too slowly is a timeout at the client; an
expired session is a 401; a redesigned page is a response without the fields
the adapter knows; a redirect points at another host.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import httpx

from app.graph import setup

GUIDE = ("Decision Hub onboarding (sanitized)\nNEEDS: DH-READ\nSTEP: create-profile once\n"
         "STEP: add-rule\nGOAL: Decision Hub NAM billing rule\nadmin password: hunter2")


class Portal:
    def __init__(self):
        self.access, self.requests, self.done = set(), {}, set()
        self.posts: list[str] = []
        self.slow_submit = 0.0          # seconds to stall AFTER creating the request
        self.session_ok = True
        self.redesigned = False
        self.redirect_to = ""
        self.validate_ok = True
        self.lock = threading.Lock()

    def start(self):
        portal = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=None, headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                data = json.dumps(body).encode() if body is not None else b""
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _gate(self):
                if portal.redirect_to:
                    self._send(302, headers={"Location": portal.redirect_to})
                    return True
                if not portal.session_ok:
                    self._send(401, {"error": "session expired"})
                    return True
                return False

            def do_GET(self):
                if self._gate():
                    return
                path = urlparse(self.path).path
                if path == "/guide":
                    return self._send(200, {"text": GUIDE})
                if path.startswith("/access/"):
                    return self._send(200, {"held": path.rsplit("/", 1)[1] in portal.access})
                if path.startswith("/requests/"):
                    name = path.rsplit("/", 1)[1]
                    req = portal.requests.get(name)
                    if portal.redesigned:
                        return self._send(200, {"items": []})
                    return self._send(200, {"request": req})
                if path.startswith("/steps/"):
                    return self._send(200, {"done": path.rsplit("/", 1)[1] in portal.done})
                if path == "/validate":
                    ok = portal.validate_ok and {"create-profile", "add-rule"} <= portal.done
                    return self._send(200, {"ok": ok, "detail": "sample booking → ACTUAL_BILLING"
                                            if ok else "sample booking → NO_MATCH"})
                self._send(404, {"error": "not found"})

            def do_POST(self):
                if self._gate():
                    return
                path = urlparse(self.path).path
                portal.posts.append(path)
                if path.startswith("/requests/"):
                    name = path.rsplit("/", 1)[1]
                    with portal.lock:
                        req = {"id": f"REQ-{len(portal.requests) + 1}", "state": "pending"}
                        portal.requests[name] = req
                    if portal.slow_submit:
                        time.sleep(portal.slow_submit)          # created, then too slow to say so
                    return self._send(200 if not portal.redesigned else 200,
                                      {"request": req} if not portal.redesigned else {"ok": 1})
                if path.startswith("/steps/"):
                    portal.done.add(path.rsplit("/", 1)[1])
                    return self._send(200, {"saved": True})
                self._send(404, {"error": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def stop(self):
        self.server.shutdown()

    def approve(self, name):
        self.requests[name]["state"] = "approved"
        self.access.add(name)


class HttpPortal:
    """The adapter: plain HTTP, no retries, every surprise named for the workflow."""

    TIMEOUT = 1.0

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.host = urlparse(base).netloc

    def _go(self, method: str, path: str) -> dict:
        try:
            r = httpx.request(method, self.base + path, timeout=self.TIMEOUT, follow_redirects=False)
        except httpx.TimeoutException as exc:
            if method == "POST" and path.startswith("/requests/"):
                raise setup.LostReceipt(f"no answer to the submit within {self.TIMEOUT}s") from exc
            raise
        if r.status_code in (301, 302, 303, 307, 308):
            where = urlparse(r.headers.get("location", ""))
            if where.netloc and where.netloc != self.host:
                raise setup.OffSite(f"redirected to {where.scheme}://{where.netloc}")
        if r.status_code == 401:
            raise setup.LoginExpired(f"{path}: 401 {r.text[:80]}")
        r.raise_for_status()
        return r.json()

    def read_document(self, doc):
        return {"text": self._go("GET", "/guide")["text"], "url": self.base + "/guide"}

    def extract(self, text):
        needs, steps, goal = [], [], ""
        for line in text.splitlines():
            if line.startswith("NEEDS:"):
                needs = [n.strip() for n in line[6:].split(",") if n.strip()]
            elif line.startswith("STEP:"):
                parts = line[5:].split()
                steps.append({"id": parts[0], "once": "once" in parts[1:]})
            elif line.startswith("GOAL:"):
                goal = line[5:].strip()
        return {"needs": needs, "steps": steps, "goal": goal}

    def has_access(self, name):
        return bool(self._go("GET", f"/access/{name}")["held"])

    def find_request(self, name):
        got = self._go("GET", f"/requests/{name}")
        if "request" not in got:
            raise setup.PageChanged("the requests page no longer lists requests the way it did")
        return got["request"]

    def request_access(self, name, why):
        got = self._go("POST", f"/requests/{name}")
        if "request" not in got:
            raise setup.PageChanged("the submit answered without a request id")
        return got["request"]

    def step_done(self, step):
        return bool(self._go("GET", f"/steps/{step['id']}")["done"])

    def do_step(self, step):
        self._go("POST", f"/steps/{step['id']}")
        return {"detail": "saved"}

    def validate(self, goal):
        return self._go("GET", "/validate")
