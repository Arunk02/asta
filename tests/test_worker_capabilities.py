import asyncio
import dataclasses
import json

import httpx
import pytest

from app import capabilities, main, mcp_server, store, task_fence, tasks, worker_auth


def work(workspace='booking'):
    conv = store.create_conversation('claude', workspace)
    task = store.create_task('Investigate service', 'analysis', 'Read the service', workspace)
    tasks.link_task(conv['id'], task['id'])
    return task, conv


async def invoke(token, body, path='/api/_invoke'):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as c:
        return await c.post(path, json=body, headers={'Authorization': 'Bearer ' + token})


def test_credentials_are_durable_hashed_expiring_and_revocable():
    task, conv = work()
    token, scope = worker_auth.issue(task['id'], conv['id'], now=100)
    assert worker_auth.authenticate(token, now=101) == scope
    with store._connect() as conn:
        rows = [tuple(r) for r in conn.execute("SELECT key,value FROM kv WHERE key LIKE 'worker_scope:%'")]
    assert token not in repr(rows)
    with pytest.raises(worker_auth.Refused):
        worker_auth.authenticate(token, now=100 + worker_auth.TTL_SECONDS)
    worker_auth.revoke(token)
    with pytest.raises(worker_auth.Refused):
        worker_auth.authenticate(token, now=101)


@pytest.mark.parametrize('change', [{'status': 'cancelled'}, {'status': 'done'}, {'workspace': 'other'}])
def test_task_end_or_scope_change_invalidates_existing_credential(change):
    task, conv = work()
    token, _ = worker_auth.issue(task['id'], conv['id'])
    store.update_task(task['id'], **change)
    with pytest.raises(worker_auth.Refused):
        worker_auth.authenticate(token)


@pytest.mark.asyncio
async def test_request_identity_is_bound_by_server_even_when_worker_asks_for_write_mode(monkeypatch):
    task, conv = work()
    token, _ = worker_auth.issue(task['id'], conv['id'])
    cap = capabilities.get('task_result')
    def observe(task_id):
        return {'task_id': task_id, 'origin_task': capabilities.FROM_TASK.get(),
                'conv_id': tasks.current_conversation(), 'read_only': capabilities.READ_ONLY_TURN.get(),
                'user_words': capabilities.said_this_turn()}
    monkeypatch.setitem(capabilities.registry(), 'task_result', dataclasses.replace(cap, fn=observe))
    response = await invoke(token, {'tool': 'task_result', 'args': {}, 'read_only': False})
    assert response.status_code == 200
    assert response.json()['result'] == {'task_id': task['id'], 'origin_task': str(task['id']),
                                        'conv_id': conv['id'], 'read_only': True, 'user_words': ''}


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    {'tool': 'task_result', 'task_id': 999},
    {'tool': 'task_result', 'conv_id': 'some-other-conversation'},
    {'tool': 'task_result', 'message_id': 1},
    {'tool': 'task_result', 'args': {'task_id': 999}},
    {'tool': 'search_memory', 'args': {'query': 'x', 'workspace': 'other'}},
    {'tool': 'merge_pr', 'args': {}},
    {'tool': 'remember', 'args': {}},
    {'tool': 'allow_contact_messages', 'args': {}},
    {'tool': 'prepare_to_send', 'args': {'what': 'hi'}},
    {'tool': 'prepare_to_send', 'args': {'what': 'hi', 'to': 'Komal', 'to_group': True}},
    {'tool': 'prepare_to_send', 'args': {'what': 'hi', 'to': 'k@x.com', 'channel': 'email'}},
    {'tool': 'delegate_task', 'args': {}},
])
async def test_forged_identity_and_ungranted_tools_are_refused(payload):
    task, conv = work()
    token, _ = worker_auth.issue(task['id'], conv['id'])
    assert (await invoke(token, payload)).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('tool,args', [
    ('prepare_to_send', {'what': 'Fixed in PR 12', 'to': 'Komal'}),
    ('propose_pr_review', {'pr': '12', 'notes': 'VERDICT: approve'}),
])
async def test_staging_tools_run_bound_to_the_task_and_not_read_only(monkeypatch, tool, args):
    task, conv = work()
    token, _ = worker_auth.issue(task['id'], conv['id'])
    cap = capabilities.get(tool)
    def observe(**kw):
        return {'args': kw, 'origin_task': capabilities.FROM_TASK.get(),
                'conv_id': tasks.current_conversation(), 'read_only': capabilities.READ_ONLY_TURN.get()}
    monkeypatch.setitem(capabilities.registry(), tool, dataclasses.replace(cap, fn=observe))
    response = await invoke(token, {'tool': tool, 'args': args, 'task_id': str(task['id'])})
    assert response.status_code == 200
    assert response.json()['result'] == {'args': args, 'origin_task': str(task['id']),
                                        'conv_id': conv['id'], 'read_only': False}


@pytest.mark.asyncio
async def test_worker_token_cannot_use_other_owner_routes_or_query_auth():
    task, conv = work()
    token, _ = worker_auth.issue(task['id'], conv['id'])
    assert (await invoke(token, {}, '/api/memory')).status_code == 401
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as c:
        assert (await c.post('/api/_invoke', params={'token': token}, json={'tool': 'task_result'})).status_code == 401


def test_config_and_process_env_contain_only_the_worker_api_credential(monkeypatch):
    monkeypatch.setenv('ASTA_TOKEN', 'owner-secret')
    task, conv = work()
    with worker_auth.session(task['id']) as (token, scope):
        config = mcp_server.config_entry(task_id=str(task['id']), conv_id='forged')
        env = config['mcpServers']['asta']['env']
        assert 'owner-secret' not in json.dumps(config)
        assert env['ASTA_TOKEN'] == token and env['ASTA_MCP_CONV'] == conv['id']
        assert set(env['ASTA_MCP_TOOLS'].split(',')) == worker_auth.READ_TOOLS | worker_auth.STAGE_TOOLS
        assert 'ASTA_MCP_READ_ONLY' not in env
        process = task_fence.env({'ASTA_TOKEN': 'owner-secret', 'PATH': '/bin'}, mode='code')
        assert process['ASTA_TOKEN'] == token
    with pytest.raises(worker_auth.Refused):
        worker_auth.authenticate(token)
    assert 'ASTA_TOKEN' not in task_fence.env({'ASTA_TOKEN': 'owner-secret'}, mode='none')
    assert task_fence.env({'ASTA_TOKEN': 'owner-secret'}, mode='')['ASTA_TOKEN'] == 'owner-secret'


def test_task_config_cannot_fall_back_to_owner_secret():
    with pytest.raises(worker_auth.Refused):
        mcp_server.config_entry(task_id='99')


def test_scoped_mcp_tools_cannot_be_enlarged_by_chat_floor(monkeypatch):
    monkeypatch.setenv('ASTA_MCP_SCOPED', '1')
    monkeypatch.setenv('ASTA_MCP_TOOLS', 'search_memory,load_skill')
    monkeypatch.setenv('ASTA_MCP_READ_ONLY', '1')
    server = mcp_server.build_server()
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == {'search_memory', 'load_skill'}


def test_scoped_worker_config_advertises_its_staging_tools(monkeypatch):
    task, _ = work()
    with worker_auth.session(task['id']):
        env = mcp_server.config_entry(task_id=str(task['id']))['mcpServers']['asta']['env']
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('ASTA_MCP_READ_ONLY', raising=False)
    names = {t.name for t in asyncio.run(mcp_server.build_server().list_tools())}
    assert worker_auth.STAGE_TOOLS <= names and 'remember' not in names and 'merge_pr' not in names


@pytest.mark.asyncio
async def test_execution_wrapper_revokes_on_cancellation_and_does_not_leak_context(monkeypatch):
    task, conv = work()
    captured = []
    async def leg(*args):
        captured.append(worker_auth.CURRENT.get()[0])
        assert worker_auth.authenticate(captured[0])['task_id'] == task['id']
        raise asyncio.CancelledError()
    monkeypatch.setattr(tasks, '_simple_leg', leg)
    with pytest.raises(asyncio.CancelledError):
        await tasks._run_simple(task['id'], task, 'Read it')
    assert worker_auth.CURRENT.get() is None
    with pytest.raises(worker_auth.Refused):
        worker_auth.authenticate(captured[0])


@pytest.mark.asyncio
async def test_concurrent_task_sessions_do_not_exchange_credentials():
    a, _ = work()
    b, _ = work('other')
    async def leg(task):
        with worker_auth.session(task['id']) as (token, scope):
            await asyncio.sleep(0)
            assert worker_auth.CURRENT.get()[0] == token
            assert worker_auth.authenticate(token)['task_id'] == task['id']
            return token
    tokens = await asyncio.gather(leg(a), leg(b))
    assert tokens[0] != tokens[1] and worker_auth.CURRENT.get() is None


def test_workspace_reader_rejects_sibling_prefix_and_symlink_escape(tmp_path, monkeypatch):
    from app import workspace
    root = tmp_path / 'project'
    root.mkdir()
    sibling = tmp_path / 'project-private'
    sibling.mkdir()
    (sibling / 'secret').write_text('not available')
    (root / 'link').symlink_to(sibling, target_is_directory=True)
    monkeypatch.setattr(workspace, '_root', lambda name: root)
    for path in ('../project-private/secret', 'link/secret'):
        with pytest.raises(ValueError, match='escapes'):
            workspace.read_workspace_file('booking', path)


def test_prune_removes_only_records_that_can_no_longer_authenticate():
    live, conv = work()
    ended, _ = work('other')
    live_token, _ = worker_auth.issue(live['id'], conv['id'], now=1000)
    old_token, _ = worker_auth.issue(live['id'], conv['id'], now=0)
    ended_token, _ = worker_auth.issue(ended['id'], now=1000)
    store.update_task(ended['id'], status='done')
    store.kv_set('worker_scope:garbage', 'not json')
    assert worker_auth.prune(now=worker_auth.TTL_SECONDS + 1) == 3
    assert worker_auth.authenticate(live_token, now=worker_auth.TTL_SECONDS + 1)['task_id'] == live['id']
    assert set(store.kv_like('worker_scope:%')) == {worker_auth._key(live_token)}


def test_server_only_secrets_never_reach_a_task_process(monkeypatch):
    base = {name: 'secret' for name in task_fence.SERVER_ONLY_SECRETS}
    base.update({'PATH': '/bin', 'ANTHROPIC_API_KEY': 'model-key'})
    for mode in ('code', 'none'):
        env = task_fence.env(dict(base), mode=mode)
        assert not set(task_fence.SERVER_ONLY_SECRETS) & set(env), mode
        assert env['ANTHROPIC_API_KEY'] == 'model-key', 'the brain still needs its model key'
    assert task_fence.env(dict(base), mode='')['JIRA_API_TOKEN'] == 'secret', 'chat path unchanged'


@pytest.mark.asyncio
@pytest.mark.parametrize('task_ws, asked, code', [
    ('', 'booking', 200),        # a colleague's question: any registered workspace
    ('', 'not-registered', 403),
    ('billing', 'booking', 403), # a task with a workspace stays pinned to it
    ('billing', 'billing', 200),
])
async def test_a_task_without_a_workspace_may_read_his_registered_ones(monkeypatch, task_ws, asked, code):
    from app import workspace
    monkeypatch.setattr(workspace, 'available_workspaces',
                        lambda: {'booking': {}, 'billing': {}})
    conv = store.create_conversation('claude', None)
    task = store.create_task('Colleague question', 'analysis', 'what is sent to TMS', task_ws or None)
    tasks.link_task(conv['id'], task['id'])
    token, _ = worker_auth.issue(task['id'], conv['id'])
    cap = capabilities.get('resolve_context')
    monkeypatch.setitem(capabilities.registry(), 'resolve_context',
                        dataclasses.replace(cap, fn=lambda workspace, task: f'ctx:{workspace}'))
    r = await invoke(token, {'tool': 'resolve_context', 'args': {'workspace': asked, 'task': 'x'}})
    assert r.status_code == code
    if code == 200:
        assert r.json()['result'] == f'ctx:{asked}'
