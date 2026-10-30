"""A requested reproducer can write tests before a solution plan, never fake proof."""
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from app import claude_cli, frontdesk, main, reproduction, store, tasks, task_fence
from app.graph import code_graph, runner


@pytest.mark.parametrize('text', [
    'Don’t plan , replicate the scenarios first .',
    'try to recreate this scenario; if confirmed fix it',
    'reproduce the issue before planning',
])
def test_explicit_reproduction_request(text):
    assert reproduction.asked(text)


@pytest.mark.parametrize('text', [
    'did you replicate the issue .?.', 'do not reproduce the issue',
    'replicate the configuration', 'fix the bug', 'why did you reproduce the issue?',
])
def test_non_reproduction_authority(text):
    assert not reproduction.asked(text)


def test_status_question_is_not_gate_feedback():
    t = store.create_task('Bug', 'code', 'p', None)
    store.update_task(t['id'], status='awaiting_approval')
    store.kv_set(f"task_gate:{t['id']}", 'plan')
    for suffix in ['did you able to replicate the issue .?.', 'did it reproduce']:
        assert main._task_awaiting_answer('c', f"task {t['id']}, {suffix}") is None
    assert frontdesk.task_intent('task , did you able to replicate the issue .?.') == 'read'


@pytest.fixture
def world(monkeypatch, tmp_path):
    tmp_path = tmp_path / 'checkout'
    tmp_path.mkdir()
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'app.py').write_text('value = 1\n')
    (tmp_path / '.gitignore').write_text('target/\n')
    monkeypatch.setenv('ASTA_GRAPH', '1')
    monkeypatch.setattr(tasks, 'task_cwd', lambda *a: str(tmp_path))
    monkeypatch.setattr(tasks, '_cwd', lambda *a: str(tmp_path))
    monkeypatch.setattr(tasks, '_resolve_executor', lambda *a: 'claude')
    monkeypatch.setattr(tasks, 'task_tools', lambda *a: '')
    async def nothing(*a, **k): return []
    monkeypatch.setattr(tasks, '_prepare_branches', nothing)
    from app import notify
    messages = []
    async def send(msg, *a, **k): messages.append(msg)
    monkeypatch.setattr(notify, 'notify', send)
    t = store.create_task('Reproduce failed amend', 'code', 'Test the real amend method', None)
    store.kv_set(f"task_engine:{t['id']}", 'graph')
    reproduction.request(t['id'], 'reproduce the issue first')
    return t, tmp_path, messages


def report(root, status='not_reproduced', failure=False):
    path = root / 'target' / 'report.xml'
    path.parent.mkdir(exist_ok=True)
    path.write_text('<testsuite><testcase name="real_path">' +
                    ('<failure message="expected ready, got failed"/>' if failure else '') +
                    '</testcase></testsuite>')
    return '```reproduction\n' + json.dumps(dict(status=status, report='target/report.xml',
        command='pytest tests/test_amend.py --junitxml=target/report.xml',
        expected='ready', actual='failed' if failure else 'ready')) + '\n```'


def test_not_reproduced_runs_writable_test_leg_without_plan_or_code_approval(world, monkeypatch):
    t, root, messages = world
    calls = []
    async def brain(prompt, **kw):
        calls.append(kw)
        assert reproduction.active(t['id'])
        assert Path(kw['agent_file']).name == 'reproduce.md'
        assert 'After the reproduction test runs, STOP' in prompt
        assert '[How this run ends]' not in prompt
        assert task_fence.MODE.get() == 'none'
        (root / 'tests').mkdir()
        (root / 'tests' / 'test_amend.py').write_text('def test_amend(): assert True\n')
        return report(root)
    monkeypatch.setattr(claude_cli, 'one_shot', brain)
    asyncio.run(runner._drive(t['id'], {'task_id': t['id']}))
    assert len(calls) == 1 and calls[0]['plan_only'] is False
    assert not tasks.plan_approved(t['id'])
    assert store.get_task(t['id'])['status'] == 'done', store.get_task(t['id'])['error']
    assert reproduction.state(t['id'])['state'] == 'not_reproduced'
    assert not any('PLAN #' in m for m in messages)
    assert reproduction.ACTIVE.get() is None


def test_confirmed_assertion_precedes_plan_and_restores_readonly(world, monkeypatch):
    t, root, messages = world
    calls = []
    async def brain(prompt, **kw):
        calls.append(kw)
        return report(root, 'confirmed', True) if len(calls) == 1 else 'STRUCTURE\nFLOW\nPLAN READY: two classes in two repos'
    monkeypatch.setattr(claude_cli, 'one_shot', brain)
    from app import go
    monkeypatch.setattr(go, 'no_ask', lambda *a: '')
    asyncio.run(runner._drive(t['id'], {'task_id': t['id']}))
    assert [c['plan_only'] for c in calls] == [False, True]
    assert reproduction.state(t['id'])['evidence']['failures'] == 1
    assert store.get_task(t['id'])['status'] == 'awaiting_approval'


@pytest.mark.parametrize('bad', ['trace', 'production', 'stale', 'error', 'zero', 'outside', 'mismatch'])
def test_no_plan_without_valid_test_evidence(world, monkeypatch, bad):
    t, root, messages = world
    async def brain(prompt, **kw):
        text = report(root, 'confirmed', True)
        path = root / 'target/report.xml'
        if bad == 'trace': return 'The code should fail. PLAN READY'
        if bad == 'production': (root / 'app.py').write_text('value = 2\n')
        if bad == 'stale': os.utime(path, (1, 1))
        if bad == 'error': path.write_text('<testsuite><testcase><error/></testcase></testsuite>')
        if bad == 'zero': path.write_text('<testsuite tests="0"/>')
        if bad == 'outside': text = text.replace('target/report.xml', '../elsewhere.xml')
        if bad == 'mismatch': text = text.replace('"confirmed"', '"not_reproduced"')
        return text
    monkeypatch.setattr(claude_cli, 'one_shot', brain)
    asyncio.run(runner._drive(t['id'], {'task_id': t['id']}))
    assert store.get_task(t['id'])['status'] == 'failed'
    assert reproduction.state(t['id'])['state'] == 'blocked'
    assert not tasks.plan_approved(t['id'])
    assert not any('PLAN #' in m for m in messages)


def test_paused_correction_durable_without_retrying_quota(world, monkeypatch):
    t, root, messages = world
    store.update_task(t['id'], status='paused')
    store.kv_set(f"task_resume_at:{t['id']}", '12345')
    calls = []
    monkeypatch.setattr(runner, 'restart_reproduction', lambda tid: calls.append(tid))
    text = 'Don’t plan, replicate the scenario first'
    result = asyncio.run(tasks.reproduce_first(t['id'], text))
    assert 'saved' in result and calls == []
    assert reproduction.state(t['id'])['words'] == text
    assert store.kv_get(f"task_resume_at:{t['id']}") == '12345'
    asyncio.run(tasks.resume_task(t['id']))
    assert calls == [t['id']]
    assert not tasks.plan_approved(t['id'])


def test_quota_preserves_scope_and_production_baseline(world, monkeypatch):
    t, root, messages = world
    async def brain(prompt, **kw):
        (root / 'app.py').write_text('value = 2\n')
        raise RuntimeError("You've hit your session limit · resets 7:30pm (Asia/Calcutta)")
    monkeypatch.setattr(claude_cli, 'one_shot', brain)
    asyncio.run(runner._drive(t['id'], {'task_id': t['id']}))
    assert store.get_task(t['id'])['status'] == 'paused'
    assert reproduction.ACTIVE.get() is None
    async def retry(prompt, **kw): return report(root)
    monkeypatch.setattr(claude_cli, 'one_shot', retry)
    asyncio.run(runner._drive(t['id'], {'task_id': t['id']}))
    assert reproduction.state(t['id'])['state'] == 'blocked'
    assert 'Production files changed' in store.get_task(t['id'])['error']


def test_conditional_fix_is_not_authorized_until_failing_evidence(world):
    from app import go
    t, root, _ = world
    reproduction.request(t['id'], 'Try to reproduce the issue; if confirmed go ahead and fix it')
    assert not reproduction.fix_authorized(t['id'])
    saved = reproduction.state(t['id'])
    store.kv_set(f"reproduction:{t['id']}", json.dumps({**saved, 'state': 'confirmed'}))
    assert reproduction.fix_authorized(t['id'])
    assert 'reproduction test failed' in go.no_ask(t['id'], 'Change the failing predicate')
    reproduction.request(t['id'], 'Reproduce the issue first. Do not fix it yet')
    saved = reproduction.state(t['id'])
    store.kv_set(f"reproduction:{t['id']}", json.dumps({**saved, 'state': 'confirmed'}))
    assert not reproduction.fix_authorized(t['id'])


def test_owner_endpoint_saves_paused_instruction_worker_cannot(world, monkeypatch):
    from fastapi.testclient import TestClient
    from app import worker_auth
    t, _, _ = world
    store.update_task(t['id'], status='paused')
    monkeypatch.setenv('ASTA_TOKEN', 'owner-test')
    token, _ = worker_auth.issue(t['id'])
    client = TestClient(main.app)
    url = f"/api/tasks/{t['id']}/reproduce"
    assert client.post(url, json={'text': 'reproduce first'}, headers={'Authorization': 'Bearer '+token}).status_code == 401
    result = client.post(url, json={'text': 'reproduce first'}, headers={'Authorization': 'Bearer owner-test'})
    assert result.status_code == 200 and 'saved' in result.json()['detail']


def test_real_test_runner_report_is_validated(tmp_path):
    import sys
    (tmp_path / 'test_actual.py').write_text('def test_actual():\n    assert 1 == 2\n')
    since = time.time()
    proc = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
        'test_actual.py', '--junitxml=report.xml'], cwd=tmp_path, capture_output=True,
        env={**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'})
    assert proc.returncode == 1
    text = '```reproduction\n'+json.dumps(dict(status='confirmed', report='report.xml',
        command='pytest test_actual.py --junitxml=report.xml', expected='2', actual='1'))+'\n```'
    proof = reproduction.evidence(text, str(tmp_path), since)
    assert proof['tests'] == proof['failures'] == 1


def test_existing_plan_gate_switches_to_reproduction_without_approval(world, monkeypatch):
    t, root, messages = world
    store.kv_del(f"reproduction:{t['id']}")
    from app import go
    monkeypatch.setattr(go, 'no_ask', lambda *a: '')
    calls = []
    async def brain(prompt, **kw):
        calls.append(kw)
        return 'PLAN READY: change the real method' if len(calls) == 1 else report(root)
    monkeypatch.setattr(claude_cli, 'one_shot', brain)
    async def run():
        await runner._drive(t['id'], {'task_id': t['id']})
        assert store.get_task(t['id'])['status'] == 'awaiting_approval'
        await tasks.reproduce_first(t['id'], 'replicate the scenario first')
        await tasks._running[t['id']]
    asyncio.run(run())
    assert [c['plan_only'] for c in calls] == [True, False]
    assert store.get_task(t['id'])['status'] == 'done'
    assert not tasks.plan_approved(t['id'])


def test_copilot_reproduction_omits_the_planning_pipeline(world, monkeypatch):
    from app import copilot_cli
    t, root, _ = world
    monkeypatch.setattr(tasks, '_resolve_executor', lambda *a: 'copilot')
    def forbidden(*a): raise AssertionError('Reproduction inherited the solution planning pipeline')
    monkeypatch.setattr(tasks, '_with_pipeline', forbidden)
    async def brain(prompt, **kw):
        assert not kw['plan_only']
        assert task_fence.MODE.get() == 'none'
        return report(root)
    monkeypatch.setattr(copilot_cli, 'one_shot', brain)
    asyncio.run(runner._drive(t['id'], {'task_id': t['id']}))
    assert store.get_task(t['id'])['status'] == 'done'


def test_reproduction_does_not_inherit_old_plan_or_generic_done_contract(world, monkeypatch):
    t, root, _ = world
    store.kv_set(f"task_plan:{t['id']}", 'STALE PLAN: implement the entire solution now')
    reproduction.request(t['id'], 'Reproduce first; if confirmed fix it and commit')
    async def brain(prompt, **kw):
        assert 'STALE PLAN' not in prompt
        assert '[How this run ends]' not in prompt
        assert 'Those are later stages controlled by Asta' in prompt
        assert prompt.rstrip().endswith('Asta owns the transition from this evidence to planning and implementation.')
        return report(root)
    monkeypatch.setattr(claude_cli, 'one_shot', brain)
    asyncio.run(runner._drive(t['id'], {'task_id': t['id']}))
    assert store.get_task(t['id'])['status'] == 'done'


@pytest.mark.parametrize('existing,change_source', [(False, False), (True, False), (False, True)])
def test_validated_new_report_is_evidence_not_a_production_edit(world, monkeypatch, existing, change_source):
    t, root, _ = world
    path = root / 'reports' / 'junit.xml'
    path.parent.mkdir()
    if existing:
        path.write_text('<original-production-xml/>')
    async def brain(prompt, **kw):
        text = report(root)
        path.write_bytes((root / 'target/report.xml').read_bytes())
        if change_source:
            (root / 'app.py').write_text('value = 99\n')
        return text.replace('target/report.xml', 'reports/junit.xml')
    monkeypatch.setattr(claude_cli, 'one_shot', brain)
    asyncio.run(runner._drive(t['id'], {'task_id': t['id']}))
    assert store.get_task(t['id'])['status'] == ('failed' if existing or change_source else 'done')


def test_repo_relative_report_resolves_only_when_unambiguous(tmp_path):
    since = time.time() - 1
    for name in ('one', 'two'):
        repo = tmp_path / name
        repo.mkdir()
        (repo / '.git').mkdir()
    text = report(tmp_path / 'one')
    proof = reproduction.evidence(text, str(tmp_path), since)
    assert proof['report'] == str(tmp_path / 'one/target/report.xml')
    report(tmp_path / 'two')
    with pytest.raises(ValueError, match='Ambiguous'):
        reproduction.evidence(text, str(tmp_path), since)
    explicit = text.replace('target/report.xml', 'one/target/report.xml')
    assert reproduction.evidence(explicit, str(tmp_path), since)['tests'] == 1
