import asyncio
import time
from collections import OrderedDict

import pytest
from fastapi.testclient import TestClient

from app import attention, ci_watch, delivery, followup, main, notify, pet, policy, store, tasks, teams_bridge, voice_mode


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ASTA_TOKEN", "synthetic-pet-token")
    monkeypatch.delenv("ASTA_PET", raising=False)
    monkeypatch.setattr(pet, "_speech", OrderedDict())
    return TestClient(main.app, headers={"Authorization": "Bearer synthetic-pet-token"})


def test_pet_endpoint_is_authenticated_and_off_by_default(client, monkeypatch):
    assert client.get("/api/pet/activity").status_code == 404
    assert client.get("/api/notifications").json() == {"items": [], "unseen": 0}
    monkeypatch.setenv("ASTA_PET", "1")
    assert client.get("/api/pet/activity", headers={"Authorization": ""}).status_code == 401
    response = client.get("/api/pet/activity")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["items"] == []
    assert client.get("/api/pet/activity?after=-1").status_code == 422


def test_pet_cursor_recovers_more_than_one_page_without_marking_seen(client, monkeypatch):
    monkeypatch.setenv("ASTA_PET", "1")
    first = store.add_notification("old synthetic alert")
    seed = client.get("/api/pet/activity").json()
    assert seed["cursor"] == first
    ids = [store.add_notification(f"synthetic alert {i}", "teams") for i in range(205)]
    seen = []
    cursor = first
    while cursor < ids[-1]:
        page = client.get("/api/pet/activity", params={"after": cursor}).json()
        seen += [item["id"] for item in page["items"]]
        assert page["unseen"] == 206
        assert page["seen_through"] == 0
        cursor = page["cursor"]
    assert seen == ids
    assert page["items"][0]["kind"] == "message"
    assert "synthetic alert" not in str(page)
    assert page["speech"] == []
    assert client.get("/api/pet/activity", params={"after": cursor + 100}).json()["cursor"] == cursor
    regular = client.get("/api/notifications").json()
    assert len(regular["items"]) == 30
    picked = client.get("/api/notifications", params={"notification": first}).json()
    assert picked["items"][0]["id"] == first
    assert not picked["items"][0]["seen"]
    client.post("/api/notifications/seen")
    page = client.get("/api/pet/activity", params={"after": cursor}).json()
    assert page["unseen"] == 0
    assert page["seen_through"] == cursor


def test_pet_reactions_follow_task_state_and_notification_type(client, monkeypatch):
    monkeypatch.setenv("ASTA_PET", "1")
    task = store.create_task("synthetic test", "code", "synthetic prompt", None)
    task_id = task["id"]
    task_alert = store.add_notification(f"📋 #{task_id} plan available", "task")
    ci_fail = store.add_notification("🔴 CI failed\nhttps://github.com/example/project/actions/runs/22", "ci")
    ci_pass = store.add_notification("🟢 CI recovered", "ci")
    reminder = store.add_notification("synthetic reminder", "reminder")
    question = store.add_notification("❓ Question: synthetic", "action")

    initial = client.get("/api/pet/activity").json()
    assert initial["tasks"][0]["kind"] == "coding"
    store.update_task(task_id, status="awaiting_approval")
    activity = client.get("/api/pet/activity").json()
    events = {item["id"]: item for item in activity["items"]}
    assert activity["tasks"][0]["kind"] == "approval"
    assert events[task_alert]["kind"] == "approval"
    assert events[task_alert]["task_id"] == task_id
    assert events[ci_fail]["kind"] == "ci_failed"
    assert events[ci_fail]["run_url"].endswith("/actions/runs/22")
    assert events[ci_pass]["kind"] == "ci_passed"
    assert events[reminder]["kind"] == "reminder"
    assert events[question]["kind"] == "question"
    store.update_task(task_id, status="done", finished_at=time.time())
    assert client.get("/api/pet/activity").json()["tasks"][0]["kind"] == "ready"
    store.update_task(task_id, status="failed", finished_at=time.time())
    assert client.get("/api/pet/activity").json()["tasks"][0]["kind"] == "blocked"


def test_pet_distinguishes_coding_investigation_and_other_work(client, monkeypatch):
    monkeypatch.setenv("ASTA_PET", "1")
    code = store.create_task("Implement change", "code", "synthetic prompt", None)
    debug = store.create_task("Trace what failed", "analysis", "Debug the synthetic failure", None)
    report = store.create_task("Summarize notes", "analysis", "synthetic prompt", None)
    page = client.get("/api/pet/activity").json()
    assert {row["id"]: row["kind"] for row in page["tasks"]} == {
        code["id"]: "coding", debug["id"]: "debugging", report["id"]: "running",
    }
    assert "synthetic" not in str(page)


@pytest.mark.parametrize("confirmed", [False, True])
def test_pet_tracks_real_followup_draft_and_delivery_transitions(client, monkeypatch, confirmed):
    monkeypatch.setenv("ASTA_PET", "1")
    monkeypatch.setattr(followup, "authorized", lambda row: False)
    tracker = followup.track("Synthetic follow-up", ["https://github.com/example/demo/pull/1"], "Example Person")
    tracker["last_moved_at"] -= followup.STALL_SECONDS + 1
    note = asyncio.run(followup._nudge(tracker, 1, time.time()))
    assert "Draft ready" in note
    draft = store.list_tasks()[0]
    task_id = draft["id"]
    store.add_notification(note, "task")
    ready = client.get("/api/pet/activity").json()
    assert [t["kind"] for t in ready["tasks"]] == ["draft_approval"]
    assert ready["items"][0]["kind"] == "draft_approval"

    async def capture_send(person, message):
        sending = client.get("/api/pet/activity").json()
        assert [t["kind"] for t in sending["tasks"]] == ["sending"]
        if not confirmed:
            raise RuntimeError("synthetic missing receipt")

    monkeypatch.setattr(teams_bridge, "send_message", capture_send)
    asyncio.run(tasks.approve(task_id))
    result = client.get("/api/pet/activity").json()
    assert "Example Person" not in str(result)
    if not confirmed:
        assert [t["kind"] for t in result["tasks"]] == ["send_uncertain"]
        assert result["items"][-1]["kind"] == "send_uncertain"
        return

    assert [t["kind"] for t in result["tasks"]] == ["waiting_reply"]
    assert result["items"][-1]["kind"] == "message_sent"
    monkeypatch.setattr(followup, "their_word", lambda row: ("reply", time.time(), "synthetic reply"))
    assert client.get("/api/pet/activity").json()["tasks"] == []


def test_pet_speech_uses_delivery_eligibility_without_changing_channels(client, monkeypatch):
    sent = []

    async def deliver(text, **kwargs):
        sent.append(text)
        return {"bell": True, "whatsapp": True, "telegram": False}

    monkeypatch.setattr(notify, "deliver", deliver)
    monkeypatch.setattr(delivery, "should_batch", lambda priority: False)
    monkeypatch.setattr(delivery, "quiet_now", lambda: False)
    monkeypatch.setattr(attention, "consider", lambda *args, **kwargs: True)
    monkeypatch.setattr(notify, "already_on_phone", lambda text: False)
    inactive = asyncio.run(notify.notify("Flag off"))
    monkeypatch.setenv("ASTA_PET", "1")
    active = asyncio.run(notify.notify("Flag on"))
    assert inactive == active
    assert sent == ["Flag off", "Flag on"]
    policy.add("mute", "push", target="synthetic service", words="mute synthetic service alerts")
    asyncio.run(notify.notify("Synthetic service is unhealthy", "ci"))
    page = client.get("/api/pet/activity").json()
    assert len(page["items"]) == 3
    assert page["speech"] == [{"id": page["items"][1]["id"], "kind": "alert"}]

    # Fetching the feed before the eligibility decision must not lose the speech.
    late = store.add_notification("synthetic later speech")
    before = client.get("/api/pet/activity", params={"after": page["cursor"]}).json()
    assert before["items"][0]["id"] == late
    assert all(event["id"] != late for event in before["speech"])
    pet.allow_speech(late, "")
    after = client.get("/api/pet/activity", params={"after": before["cursor"]}).json()
    assert after["items"] == []
    assert after["speech"][-1]["id"] == late

    monkeypatch.setattr(delivery, "quiet_now", lambda: True)
    assert client.get("/api/pet/activity").json()["speech"] == []
    monkeypatch.setattr(delivery, "quiet_now", lambda: False)
    monkeypatch.setattr(voice_mode, "on", lambda: True)
    assert client.get("/api/pet/activity").json()["speech"] == []


def test_pet_can_announce_green_ci_without_adding_a_phone_push(client, monkeypatch):
    monkeypatch.setenv("ASTA_PET", "1")
    monkeypatch.setattr(delivery, "quiet_now", lambda: False)

    async def unexpected_push(*args, **kwargs):
        pytest.fail("green CI attempted a phone push")

    monkeypatch.setattr(notify, "notify", unexpected_push)
    asyncio.run(ci_watch.announce("✅ CI passed for a synthetic pull request"))
    page = client.get("/api/pet/activity").json()
    assert page["items"][0]["kind"] == "ci_passed"
    assert page["speech"] == [{"id": page["items"][0]["id"], "kind": "ci_passed"}]
