"""Regressions reproduced during the Oct 26 audit; no live services involved."""
import asyncio
import os
import subprocess
from pathlib import Path

import pytest
from starlette.requests import Request

from app import app_tasks, evolve, memory, settings, store, tasks


def test_missing_server_token_never_disables_auth(monkeypatch):
    from app import main
    monkeypatch.delenv('ASTA_TOKEN', raising=False)
    request = Request({'type': 'http', 'headers': [], 'query_string': b''})
    assert not main._token_ok(request)
    monkeypatch.setenv('ASTA_TOKEN', 'test-secret')
    assert not main._token_ok(request)
    request = Request({'type': 'http', 'headers': [(b'authorization', b'Bearer test-secret')],
                       'query_string': b''})
    assert main._token_ok(request)


@pytest.mark.asyncio
async def test_login_and_both_websockets_refuse_unconfigured_auth(monkeypatch):
    from app import main
    from fastapi import HTTPException
    monkeypatch.delenv('ASTA_TOKEN', raising=False)

    class Connection:
        query_params = {}
        cookies = {}
        closed = None
        async def json(self):
            return {'token': ''}
        async def close(self, code):
            self.closed = code
        async def accept(self):
            pytest.fail('unauthenticated socket accepted')
    with pytest.raises(HTTPException):
        await main.api_login(Connection())
    for handler in (main.ws_chat, main.ws_voice_mode):
        connection = Connection()
        await handler(connection)
        assert connection.closed == 4401


@pytest.mark.asyncio
async def test_graph_files_require_the_same_auth(monkeypatch):
    from app import main
    monkeypatch.setenv('ASTA_TOKEN', 'test-secret')
    request = Request({'type': 'http', 'path': '/graph/project/index.html',
                       'headers': [], 'query_string': b''})
    async def next_handler(request):
        pytest.fail('private graph served without authentication')
    assert (await main._no_stale_ui(request, next_handler)).status_code == 401


def test_colliding_memory_titles_preserve_both_facts(tmp_path, monkeypatch):
    root = tmp_path / 'memory'
    monkeypatch.setattr(memory, 'MEMORY_DIR', root)
    monkeypatch.setattr(memory, 'FACTS_DIR', root / 'facts')
    monkeypatch.setattr(memory, 'EPISODES_DIR', root / 'episodes')
    monkeypatch.setattr(memory, 'INDEX_FILE', root / 'MEMORY.md')
    first = memory.remember('Queue retry?', 'first fact')
    second = memory.remember('Queue retry!', 'second fact')
    assert first != second
    assert 'first fact' in memory.read_memory_file(first)
    assert 'second fact' in memory.read_memory_file(second)
    assert first in memory.index_text() and second in memory.index_text()


def test_memory_reader_rejects_sibling_prefix_and_symlink_escape(tmp_path, monkeypatch):
    root = tmp_path / 'memory'
    root.mkdir()
    other = tmp_path / 'memory-other'
    other.mkdir()
    (other / 'note.md').write_text('outside')
    (root / 'link.md').symlink_to(other / 'note.md')
    monkeypatch.setattr(memory, 'MEMORY_DIR', root)
    assert memory.read_memory_file('../memory-other/note.md') is None
    assert memory.read_memory_file('link.md') is None


@pytest.mark.parametrize('args', [
    ['api', '-XPOST', 'repos/example/repo/issues/1/comments'],
    ['api', '--method=POST', 'repos/example/repo/issues/1/comments'],
    ['api', '-X', 'POST', 'repos/example/repo/issues/1/comments'],
    ['api', 'repos/example/repo/issues/1/comments', '-fbody=hello'],
])
def test_compact_gh_options_cannot_bypass_read_only_fence(tmp_path, args):
    fake = tmp_path / 'gh'
    fake.write_text('#!/bin/sh\necho downstream-reached\n')
    fake.chmod(0o755)
    shim = Path(__file__).resolve().parents[1] / 'app/shims/gh'
    env = {'PATH': f'{tmp_path}:/usr/bin:/bin', 'ASTA_TASK_WRITES': 'none'}
    run = subprocess.run([str(shim), *args], env=env, capture_output=True, text=True)
    assert run.returncode == 3
    assert 'downstream-reached' not in run.stdout


def summary(pushes=10, constitution=None):
    return {'pass_rate': 1.0, 'day': {'pushes': pushes, 'missed': 0},
            'sets': {'constitution': constitution if constitution is not None else {'total': 5, 'passed': 5}}}


@pytest.mark.asyncio
async def test_candidate_is_never_visible_to_live_readers(monkeypatch):
    knob = 'ASTA_COALESCE_SECONDS'
    settings.set_override(knob, 135)
    monkeypatch.setenv(knob, '140')
    entered, release = asyncio.Event(), asyncio.Event()
    async def measure(k=1, *, overrides=None):
        assert overrides == {knob: 180}
        entered.set()
        await release.wait()
        return summary(5)
    monkeypatch.setattr(evolve, 'measure', measure)
    candidate = evolve.Candidate('L1', knob, 180, 'noise', 'batch')
    task = asyncio.create_task(evolve.prove(candidate, baseline=summary()))
    await entered.wait()
    assert settings.value(knob) == 135
    assert os.environ[knob] == '140'
    release.set()
    assert (await task)['ok']
    assert settings.value(knob) == 135


@pytest.mark.parametrize('constitution', [{}, {'total': 0, 'passed': 0},
                                        {'total': '5', 'passed': '5'}, {'total': 5, 'passed': 4}])
@pytest.mark.asyncio
async def test_missing_empty_or_failed_safety_suite_cannot_promote(monkeypatch, constitution):
    async def measure(k=1, *, overrides=None):
        return summary(5, constitution)
    monkeypatch.setattr(evolve, 'measure', measure)
    result = await evolve.prove(evolve.Candidate('L1', 'ASTA_COALESCE_SECONDS', 180, 'noise', 'batch'),
                                baseline=summary())
    assert not result['ok'] and not result['constitution_ok']


def draft():
    t = store.create_task('Reply', 'teams_draft', 'send reply', None, teams_chat='Test Person')
    store.update_task(t['id'], status='awaiting_approval', result='Here is the requested answer.')
    return t['id']


@pytest.mark.asyncio
async def test_concurrent_approvals_deliver_exactly_one_draft(monkeypatch):
    from app import notify, teams_bridge
    sends = []
    entered, release = asyncio.Event(), asyncio.Event()
    async def send(chat, text):
        sends.append((chat, text))
        entered.set()
        await release.wait()
    async def quiet(*args, **kwargs):
        pass
    monkeypatch.setattr(teams_bridge, 'send_message', send)
    monkeypatch.setattr(notify, 'notify', quiet)
    tid = draft()
    first = asyncio.create_task(tasks.approve(tid))
    await entered.wait()
    await tasks.approve(tid)
    release.set()
    await first
    await tasks.approve(tid)
    assert len(sends) == 1 and store.get_task(tid)['status'] == 'sent'


@pytest.mark.asyncio
async def test_uncertain_delivery_retains_claim_and_is_not_retried(monkeypatch):
    from app import notify, teams_bridge
    sends = []
    async def send(chat, text):
        sends.append(text)
        raise RuntimeError('connection lost after click')
    async def quiet(*args, **kwargs):
        pass
    monkeypatch.setattr(teams_bridge, 'send_message', send)
    monkeypatch.setattr(notify, 'notify', quiet)
    tid = draft()
    assert 'unconfirmed' in await tasks.approve(tid)
    await tasks.approve(tid)
    assert len(sends) == 1 and store.get_task(tid)['status'] == 'send_uncertain'


@pytest.mark.parametrize('check', [(1, 'check failed'), (0, ''), (0, 'false')])
@pytest.mark.asyncio
async def test_failed_app_verification_is_never_done_or_repeated(monkeypatch, check):
    monkeypatch.setenv('ASTA_APPS', '1')
    monkeypatch.setattr(app_tasks, 'resolve', lambda name: (Path('/Applications/Notes.app'), ''))
    monkeypatch.setattr(app_tasks, 'dictionary', lambda *a: 'scripting dictionary')
    calls = []
    async def ask(brief):
        return {'script': 'tell application "Notes" to activate', 'check': 'return true', 'changes': 'open notes'}
    async def script(text):
        calls.append(text)
        return (0, '') if len(calls) == 1 else check
    monkeypatch.setattr(app_tasks, '_ask_model', ask)
    monkeypatch.setattr(app_tasks, 'osascript', script)
    result = await app_tasks.do('Notes', 'open notes')
    assert "couldn't verify" in result and 'Done in' not in result
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cancelled_send_remains_uncertain_and_is_not_replayed(monkeypatch):
    from app import teams_bridge
    entered = asyncio.Event()
    async def send(*args):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(teams_bridge, 'send_message', send)
    tid = draft()
    job = asyncio.create_task(tasks.approve(tid))
    await entered.wait()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert store.get_task(tid)['status'] == 'send_uncertain'
    assert 'will not be sent again' in await tasks.approve(tid)


@pytest.mark.asyncio
async def test_restart_reconciles_inflight_draft_without_sending():
    tid = draft()
    assert store.claim_draft(tid, 'Test Person', 'Here is the requested answer.')
    assert await tasks.recover_orphans() == []
    assert store.get_task(tid)['status'] == 'send_uncertain'


def test_canary_records_the_pre_promotion_value_for_rollback():
    import json
    settings.set_override('ASTA_COALESCE_SECONDS', 135)
    eid = evolve.promote(evolve.Candidate('L1', 'ASTA_COALESCE_SECONDS', 180, 'noise', 'batch'), 'verified')
    record = json.loads(store.kv_get(f'evolve_canary:{eid}'))
    assert record['before'] == 135


def test_separate_database_connections_can_claim_a_draft_only_once():
    from concurrent.futures import ThreadPoolExecutor
    tid = draft()
    def claim(_):
        try:
            return store.claim_draft(tid, 'Test Person', 'Here is the requested answer.')
        finally:
            store.close_connection()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(claim, range(12)))
    assert results.count(True) == 1
