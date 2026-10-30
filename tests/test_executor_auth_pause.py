"""Missing CLI login preserves work and waits for explicit resume."""
import asyncio
import json

import pytest

from app import claude_cli, store, tasks

_REAL_ONE_SHOT = claude_cli.one_shot


@pytest.mark.parametrize('resume', [False, True])
def test_missing_login_preserves_only_an_existing_session(monkeypatch, resume):
    task = store.create_task('fix', 'code', 'reproduce first then fix', None)
    tid = task['id']
    store.kv_set(f'task_executor:{tid}', 'claude')
    if resume:
        store.kv_set(f'task_session:{tid}:claude', 'existing-session')

    async def missing(*args, **kwargs):
        raise claude_cli.AuthenticationRequired('Not logged in · Please run /login')

    monkeypatch.setattr(claude_cli, 'one_shot', missing)
    with pytest.raises(tasks._LimitPaused) as caught:
        asyncio.run(tasks._run_code_leg(tid, 'prompt', '.', resume=resume, effort='high'))
    notes = []

    async def notify(message, **kwargs):
        notes.append(message)

    monkeypatch.setattr('app.notify.notify', notify)
    store.kv_set(f'task_resume_at:{tid}', '123')
    asyncio.run(tasks._pause_task(tid, task, caught.value))
    assert store.get_task(tid)['status'] == 'paused'
    assert not store.kv_get(f'task_resume_at:{tid}')
    assert store.kv_get(f'task_session:{tid}:claude') == ('existing-session' if resume else None)
    assert json.loads(store.kv_get(f'task_paused:{tid}'))['reason'] == 'authentication'
    assert len(notes) == 1 and '/login' in notes[0] and f'resume task {tid}' in notes[0]
    seen = {}

    async def signed_in(*args, **kwargs):
        seen.update(kwargs)
        return 'continued'

    monkeypatch.setattr(claude_cli, 'one_shot', signed_in)
    assert asyncio.run(tasks._run_code_leg(tid, 'continue', '.', resume=True, effort='high')) == 'continued'
    assert seen['resume'] is resume
    if resume:
        assert seen['session_id'] == 'existing-session'


def test_quota_fallback_to_missing_login_pauses(monkeypatch):
    task = store.create_task('fix', 'code', 'prompt', None)
    tid = task['id']
    store.kv_set(f'task_executor:{tid}', 'copilot')

    async def quota(*args, **kwargs):
        raise RuntimeError('You have exceeded your monthly quota')

    async def login(*args, **kwargs):
        raise claude_cli.AuthenticationRequired('Not logged in · Please run /login')

    monkeypatch.setattr('app.copilot_cli.one_shot', quota)
    monkeypatch.setattr(claude_cli, 'one_shot', login)
    monkeypatch.setattr(claude_cli, 'available', lambda: True)
    with pytest.raises(tasks._LimitPaused) as caught:
        asyncio.run(tasks._run_code_leg(tid, 'prompt', '.', resume=False, effort='high'))
    assert caught.value.reason == 'authentication'
    assert not store.kv_get(f'task_session:{tid}:claude')
    assert not store.kv_get(f'task_session:{tid}:copilot')


@pytest.mark.parametrize('output,auth', [
    ('Not logged in · Please run /login\n', True),
    ('Test failed: expected Not logged in · Please run /login', False),
    ('Unexpected internal error', False),
])
def test_cli_auth_failure_is_typed_only_for_login_response(monkeypatch, output, auth):
    class Process:
        returncode = 1
        stdout = None
        async def wait(self):
            return 1
    process = Process()

    async def spawn(*args, **kwargs):
        process.stdout = asyncio.StreamReader()
        process.stdout.feed_data(output.encode())
        process.stdout.feed_eof()
        return process

    monkeypatch.setattr(claude_cli, 'available', lambda: True)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(_REAL_ONE_SHOT('prompt'))
    assert isinstance(caught.value, claude_cli.AuthenticationRequired) is auth


def test_analysis_login_failure_uses_same_pause(monkeypatch):
    task = store.create_task('investigate', 'analysis', 'prompt', None)
    tid = task['id']
    store.kv_set(f'task_executor:{tid}', 'claude')

    async def missing(*args, **kwargs):
        raise claude_cli.AuthenticationRequired('Not logged in · Please run /login')

    monkeypatch.setattr(claude_cli, 'one_shot', missing)
    with pytest.raises(tasks._LimitPaused) as caught:
        asyncio.run(tasks._simple_leg(tid, task, 'prompt'))
    assert caught.value.reason == 'authentication'
