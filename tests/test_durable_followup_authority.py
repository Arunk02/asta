import asyncio
import datetime as dt

import pytest

from app import agent, authority, capabilities, followup, store, tasks

URL = 'https://github.com/example/repo/pull/1'


def now():
    return dt.datetime(2026, 10, 26, 11).timestamp()  # Monday, local working hours


def stalled(**kwargs):
    row = followup.track('Finish the review', [URL], 'Test Person', **kwargs)
    row['last_moved_at'] = now() - followup.STALL_SECONDS - 60
    followup._save([row])
    return row


def test_explicit_standing_permission_is_immediate_and_exact():
    token = capabilities.TURN_TEXT.set('Handle messages and follow-ups with Test Person going forward')
    try:
        assert 'Active until' in agent.allow_contact_messages('Test Person')
    finally:
        capabilities.TURN_TEXT.reset(token)
    assert authority.may('send', 'Test Person')
    assert not authority.may('send', 'Test Person Two')
    assert not authority.may('call', 'Test Person')
    authority.revoke(authority.grants()[0].id)
    assert not authority.may('send', 'Test Person')


def test_model_arguments_alone_cannot_grant_standing_permission():
    token = capabilities.TURN_TEXT.set('Show me the messages from Test Person')
    try:
        assert 'No explicit' in agent.allow_contact_messages('Test Person')
    finally:
        capabilities.TURN_TEXT.reset(token)
    assert authority.grants() == []


def test_tracking_records_his_followup_instruction_and_survives_new_turn():
    token = capabilities.TURN_TEXT.set(f'Send Test Person this and follow up with Test Person until resolved: {URL}')
    try:
        agent.track_until_done('Finish review', [URL], 'Test Person')
    finally:
        capabilities.TURN_TEXT.reset(token)
    row = followup.list_open()[0]
    assert followup.authorized(row)
    changed = {**row, 'person': 'Different Person'}
    assert not followup.authorized(changed)
    assert not followup.authorized({**row, 'urls': ['https://example.com/other']})


@pytest.mark.asyncio
async def test_authorized_chase_uses_existing_durable_send_without_another_approval(monkeypatch):
    from app import notify, teams_bridge
    sent = []
    async def send(to, text):
        sent.append((to, text))
        return to
    async def quiet(*a, **k):
        pass
    monkeypatch.setattr(teams_bridge, 'send_message', send)
    monkeypatch.setattr(notify, 'notify', quiet)
    row = stalled(authorized_words='Follow up with Test Person on this PR')
    assert 'Sent to' in await followup._nudge(row, 1, now())
    # A restart can reload a stale row. The durable daily claim still wins.
    row = followup.list_open()[0]
    assert await followup._nudge(row, 1, now()) == ''
    assert len(sent) == 1 and store.list_tasks()[0]['status'] == 'sent'
    followup.stop(row['id'])
    assert await followup._nudge(row, 1, now() + 86400) == ''


@pytest.mark.asyncio
async def test_authorized_chase_waits_for_working_hours_without_asking(monkeypatch):
    row = stalled(authorized_words='Follow up with Test Person')
    evening = dt.datetime(2026, 10, 26, 22).timestamp()
    assert await followup._nudge(row, 1, evening) == ''
    assert store.list_tasks() == []


def test_daily_and_total_reminder_limits_are_durable():
    row = stalled(authorized_words='Follow up with Test Person')
    for day in range(3):
        assert followup._reserve_authorized_nudge(row, now() + 86400 * day)
        assert not followup._reserve_authorized_nudge(row, now() + 86400 * day)
    assert not followup._reserve_authorized_nudge(row, now() + 86400 * 3)


@pytest.mark.asyncio
async def test_revoked_permission_cannot_send_after_it_was_scheduled(monkeypatch):
    g = authority.grant('send', 'Test Person', 1)
    authority.revoke(g.id)
    with pytest.raises(PermissionError):
        await authority.send_now(g, 'Test Person', 'hello')


def test_permission_capacity_is_claimed_before_any_transport_runs():
    g = authority.grant('send', 'Test Person', 1)
    assert authority.claim_use(g)
    assert not authority.claim_use(g)
    assert authority.used_today(g.id) == 1


@pytest.mark.asyncio
async def test_graph_warns_and_continues_under_existing_authority(monkeypatch):
    from app.graph import follow_through
    called = []
    async def watcher(state):
        return {'done': False, 'moved': False}
    async def nudger(state):
        called.append('nudge')
    async def warner(state):
        called.append('warn')
    monkeypatch.setattr(follow_through, 'WATCHER', watcher)
    monkeypatch.setattr(follow_through, 'NUDGER', nudger)
    monkeypatch.setattr(follow_through, 'WARNER', warner)
    graph = follow_through.build().compile()
    result = await graph.ainvoke({'goal': 'review', 'due_at': 1, 'authorized': True})
    assert called == ['warn', 'nudge']
    assert '__interrupt__' not in result


def test_negated_or_other_recipient_clause_cannot_grant_followup():
    assert not authority.requested_followup("Don't message Test Person; follow up with Other Person", 'Test Person')
    assert not authority.requested_followup('Do not follow up with Test Person', 'Test Person')
    token = capabilities.TURN_TEXT.set('Message Test Person once; handle Other Person replies going forward')
    try:
        assert 'No explicit' in agent.allow_contact_messages('Test Person')
    finally:
        capabilities.TURN_TEXT.reset(token)
    assert authority.grants() == []


@pytest.mark.asyncio
async def test_contact_permission_is_reachable_through_authenticated_cli_api(monkeypatch):
    import httpx
    from app import main
    monkeypatch.setenv('ASTA_TOKEN', 'test-secret')
    cid = store.create_conversation('claude', None)['id']
    origin = store.add_ui_message(cid, 'user', 'Handle conversations with Test Person going forward')
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        body = {'tool': 'allow_contact_messages', 'args': {'person': 'Test Person'},
                'conv_id': cid, 'message_id': origin}
        response = await client.post('/api/_invoke', json=body,
                                     headers={'Authorization': 'Bearer test-secret'})
        assert response.status_code == 200
        assert 'Active until' in response.json()['result']
        wrong = {**body, 'message_id': origin + 1000}
        response = await client.post('/api/_invoke', json=wrong,
                                     headers={'Authorization': 'Bearer test-secret'})
        assert response.status_code == 400
    assert len(authority.grants()) == 1
