"""A fake documentation / access / setup portal for the setup workflow.

The backend is the portal's own state — requests, granted access, what is set
up. A "restart" is a NEW FakePortal over the SAME backend, exactly as a new Asta
process would talk to the same real portal. Every failure the workflow must
survive is a switch on the backend.
"""
from __future__ import annotations

from app.graph import setup

GUIDE = """Decision Hub onboarding (sanitized)
Prerequisite: DH-READ access group (request it in the access portal).
1. Create your rule profile.
2. Add the NAM billing rule.
Contact: password: hunter2 (do not share)"""


class Backend:
    def __init__(self):
        self.access: set[str] = set()
        self.requests: dict[str, dict] = {}
        self.submitted: list[str] = []
        self.steps_done: set[str] = set()
        self.done_calls: list[str] = []
        self.validate_ok = True
        self.login_expired = False
        self.page_changed = False
        self.redirect_off_site = False
        self.lost_receipt = ""            # "" | "made" | "not_made"
        self.no_effect: set[str] = set()
        self.signins = 0                  # the portal's own sign-in counter; Asta never calls it

    def approve(self, name: str) -> None:
        self.requests[name]["state"] = "approved"
        self.access.add(name)

    def reject(self, name: str) -> None:
        self.requests[name]["state"] = "rejected"

    def sign_in(self) -> None:            # what HE does in the browser
        self.login_expired = False
        self.signins += 1


class FakePortal:
    def __init__(self, backend: Backend):
        self.b = backend

    def _session(self):
        if self.b.login_expired:
            raise setup.LoginExpired("session cookie=abc123 rejected")

    def read_document(self, doc: str) -> dict:
        if self.b.redirect_off_site:
            raise setup.OffSite("guide redirected to https://login.example-evil.test")
        self._session()
        return {"text": GUIDE, "url": doc}

    def extract(self, text: str) -> dict:
        return {"needs": ["DH-READ"] if "DH-READ" in text else [],
                "steps": [{"id": "create-profile", "once": True}, {"id": "add-rule"}],
                "goal": "Decision Hub NAM billing rule"}

    def has_access(self, name: str) -> bool:
        self._session()
        return name in self.b.access

    def find_request(self, name: str) -> dict | None:
        self._session()
        return dict(self.b.requests[name]) if name in self.b.requests else None

    def request_access(self, name: str, why: str) -> dict:
        self._session()
        if self.b.page_changed:
            raise setup.PageChanged("no 'Request access' button on the access page")
        self.b.submitted.append(name)
        if self.b.lost_receipt == "not_made":
            self.b.submitted.pop()
            raise setup.LostReceipt("timed out waiting for confirmation")
        req = {"id": f"REQ-{len(self.b.requests) + 1}", "state": "pending"}
        self.b.requests[name] = req
        if self.b.lost_receipt == "made":
            raise setup.LostReceipt("confirmation page did not load")
        return dict(req)

    def step_done(self, step: dict) -> bool:
        self._session()
        return step["id"] in self.b.steps_done

    def do_step(self, step: dict) -> dict:
        self._session()
        if self.b.page_changed:
            raise setup.PageChanged(f"form for {step['id']} has no Save button")
        self.b.done_calls.append(step["id"])
        if step["id"] in self.b.no_effect:
            return {"detail": "clicked Save; nothing changed"}
        self.b.steps_done.add(step["id"])
        return {"detail": "saved"}

    def validate(self, goal: str) -> dict:
        self._session()
        ok = self.b.validate_ok and {"create-profile", "add-rule"} <= self.b.steps_done
        return {"ok": ok, "detail": "rule evaluates a sample NAM booking to ACTUAL_BILLING"
                if ok else "sample booking still evaluates to NO_MATCH"}
