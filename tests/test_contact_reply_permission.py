import asyncio
import json

import pytest

from app import agent, answers, authority, capabilities, chat_watch, guardrails, loop, ops, store
from test_round4_phase_d import phone  # noqa: F401


@pytest.fixture
def delivery(monkeypatch):
    calls = []
    async def no_reply(*args):
        return False
    async def send(**kwargs):
        calls.append(kwargs)
        await asyncio.sleep(0)
        return '✅ Sent to Test Person.'
    monkeypatch.setattr(chat_watch, 'he_replied_since', no_reply)
    monkeypatch.setattr(guardrails, 'section', lambda name: '')
    monkeypatch.setitem(ops.REGISTRY, 'teams_send', {'run': send})
    return calls


def intent(**kwargs):
    return dict(to='Test Person', who='Test Person', what='The QA service is healthy now.',
                need='Check the service', task_id=42, to_group=False, **kwargs)


@pytest.mark.asyncio
async def test_user_standing_instruction_reaches_the_real_reply_path(phone, delivery):
    token = capabilities.TURN_TEXT.set('Handle replies with Test Person going forward')
    try:
        assert 'Active until' in agent.allow_contact_messages('Test Person')
    finally:
        capabilities.TURN_TEXT.reset(token)
    assert await answers.present(who='Test Person', need='Check the service',
                                 chat='Test Person', group=False, analysis='Checked QA.',
                                 reply='The QA service is healthy now.', task_id=42)
    assert len(delivery) == 1
    assert loop.awaiting(phone['cid']) is None
    assert authority.used_today(authority.grants()[0].id) == 1
    assert not authority.auto_reply_to('Test Person', group=True)
    assert not authority.auto_reply_to('Test Person Two')


@pytest.mark.asyncio
async def test_daily_cap_is_reserved_before_concurrent_replies(phone, delivery):
    g = authority.grant('send', 'Test Person', 1)
    results = await asyncio.gather(authority.send_reply('Test Person', 'One'),
                                   authority.send_reply('Test Person', 'Two'), return_exceptions=True)
    assert len(delivery) == 1
    assert sum(isinstance(r, authority.ReplyNotAuthorized) for r in results) == 1
    assert authority.used_today(g.id) == 1


@pytest.mark.parametrize('revoked', [False, True])
def test_explicit_limit_or_revocation_cannot_fall_back_to_legacy_allowlist(monkeypatch, revoked):
    monkeypatch.setattr(guardrails, 'section', lambda name: '- Test Person')
    g = authority.grant('send', 'Test Person', 1)
    if revoked:
        authority.revoke(g.id)
    else:
        assert authority.claim_use(g)
    assert not authority.auto_reply_to(' test person ')


@pytest.mark.asyncio
async def test_revocation_during_final_preflight_never_calls_transport(phone, delivery, monkeypatch):
    g = authority.grant('send', 'Test Person', 2)
    claim = authority.claim_use
    def revoke_then_claim(permission):
        authority.revoke(g.id)
        return claim(permission)
    monkeypatch.setattr(authority, 'claim_use', revoke_then_claim)
    assert not await answers._deliver_automatic_reply(intent())
    assert delivery == []
    assert receipts() == []


def receipts():
    with store._connect() as conn:
        return [json.loads(r['value']) for r in conn.execute(
            "SELECT value FROM kv WHERE key LIKE 'answer_delivery:%'")]


@pytest.mark.asyncio
async def test_same_delivery_is_claimed_once_across_concurrent_runs(phone, delivery):
    authority.grant('send', 'Test Person', 20)
    assert all(await asyncio.gather(answers._deliver_automatic_reply(intent()),
                                    answers._deliver_automatic_reply(intent())))
    assert len(delivery) == 1
    assert receipts()[0]['state'] == 'sent'
    assert await answers._deliver_automatic_reply(intent())
    assert len(delivery) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_uncertain_send_is_retained_without_replay_or_sendable_draft(phone, delivery, monkeypatch, cancel):
    g = authority.grant('send', 'Test Person', 20)
    calls = []
    async def uncertain(**kwargs):
        calls.append(kwargs)
        if cancel:
            raise asyncio.CancelledError()
        raise RuntimeError('Receipt lost after transport accepted message')
    monkeypatch.setitem(ops.REGISTRY, 'teams_send', {'run': uncertain})
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await answers._deliver_automatic_reply(intent())
    else:
        assert await answers._deliver_automatic_reply(intent())
    assert receipts()[0]['state'] == 'uncertain'
    assert await answers._deliver_automatic_reply(intent())
    assert len(calls) == 1 and authority.used_today(g.id) == 1
    assert loop.awaiting(phone['cid']) is None


@pytest.mark.asyncio
async def test_interrupted_process_receipt_is_not_replayed(phone, delivery):
    authority.grant('send', 'Test Person', 20)
    assert await answers._deliver_automatic_reply(intent())
    with store._connect() as conn:
        conn.execute("UPDATE kv SET value=? WHERE key LIKE 'answer_delivery:%'",
                     (json.dumps({'state': 'sending'}),))
    assert await answers._deliver_automatic_reply(intent())
    assert len(delivery) == 1


def test_global_send_hold_and_policy_still_apply(monkeypatch):
    from app import steward, policy
    authority.grant('send', 'Test Person', 20)
    monkeypatch.setattr(steward, 'sends_held', lambda: True)
    assert not authority.auto_reply_to('Test Person')
    monkeypatch.setattr(steward, 'sends_held', lambda: False)
    policy.add('never', act='send', target='Test Person')
    assert not authority.auto_reply_to('Test Person')
