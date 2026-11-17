"""Replay of the 17 Nov 15:28 turn: Arun's draft feedback, after a colleague's VTS question
was put in front of him. Synthetic names and ids."""

import json, time
from app import claude_cli, store

def test_the_1528_turn_now_carries_the_question_the_shape_and_the_kit(monkeypatch):
    monkeypatch.setenv("ASTA_CLI_MCP", "1"); monkeypatch.setenv("ASTA_ON_DEMAND", "1")
    monkeypatch.setattr(claude_cli, "_version", (2, 1, 311))
    monkeypatch.setattr(claude_cli, "workspace_roots", lambda: ["/tmp/ws-booking"])
    conv = store.create_conversation(model="claude_cli", workspace=None)
    store.add_ui_message(conv["id"], "assistant", "*Dev Colleague* wants you to check VTS for the booking.", {"via": "answer"})
    now = time.time()
    store.save_teams_messages([
        {"key": "a", "chat": "Dev Colleague", "sender": "Dev Colleague", "sent_at": now - 300,
         "text": "Booking UI https://ui.example.test/booking/MXAAAA1111BB"},
        {"key": "b", "chat": "Dev Colleague", "sender": "Dev Colleague", "sent_at": now - 290,
         "text": "Bro, for this booking, can you see from VTS? I think we got the status as pending, but still, it's processed."}])
    store.add_ui_message(conv["id"], "user", "check in sit", {})
    wrapped = ("Arun did NOT approve sending the draft as-is. His feedback:\n"
               "sit booking number it is check on the request he asked\n\nRevise accordingly — change only what he asked for.")
    cmd = claude_cli._build_cmd(conv, wrapped)
    umsg = cmd[cmd.index("-p") + 1]
    assert "THEIR QUESTION" in umsg and "status as pending, but still, it's processed" in umsg
    assert "MXAAAA1111BB" in umsg and "sit environment" in umsg
    assert "Confidence: CONFIRMED" in umsg and "find the code that decides it" in umsg.lower()
    assert cmd[cmd.index("--plugin-dir") + 1] == str(claude_cli.ON_DEMAND_INVESTIGATE_PLUGIN)
    env = json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]["asta"]["env"]
    assert env.get("ASTA_CHAT_INVESTIGATE") == "1" and "--add-dir" in cmd
