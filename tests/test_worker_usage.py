"""Worker usage survives session rotation, retries, failure and missing telemetry."""
import asyncio
import json

import pytest
from app import store, token_audit, worker_usage, tasks


def append(path, mid, *, output=5):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        stream.write(json.dumps({'type': 'assistant', 'message': {'id': mid,
            'usage': {'input_tokens': 2, 'cache_creation_input_tokens': 10,
                      'cache_read_input_tokens': 100, 'output_tokens': output}, 'content': []}})+'\n')


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(token_audit, 'CLAUDE_PROJECTS', tmp_path / 'logs')
    task = store.create_task('pilot', 'code', 'reproduce first then fix', None)
    cwd = str(tmp_path / 'checkout')
    def log(sid): return token_audit.CLAUDE_PROJECTS / token_audit._encode_project(cwd) / f'{sid}.jsonl'
    return task['id'], cwd, log


def test_resumed_stage_deltas_and_rotated_session_survive(world):
    tid, cwd, log = world
    with worker_usage.attempt(tid, 'claude', cwd, 'repro', stage='reproduce', model='small'):
        assert worker_usage.report(tid)['attempts'][0]['status'] == 'running'
        append(log('repro'), 'm1')
        append(log('repro'), 'm1')  # repeated content record, not another call
    store.kv_del(f'task_session:{tid}:claude')
    with worker_usage.attempt(tid, 'claude', cwd, 'build', stage='plan', model='small'):
        append(log('build'), 'm2')
    with worker_usage.attempt(tid, 'claude', cwd, 'build', stage='implement', model='large'):
        append(log('build'), 'm3', output=9)
    report = worker_usage.report(tid)
    assert report['recorded_usage_complete']
    assert report['known_subtotal'] == {'calls': 3, 'new_tokens': 36, 'cache_read': 300, 'out_tokens': 19}
    assert [row['stage'] for row in report['attempts']] == ['reproduce', 'plan', 'implement']
    assert [row['configured_model'] for row in report['attempts']] == ['small', 'small', 'large']


def test_failed_paid_attempt_is_kept_and_unknown_fallback_is_not_zero(world):
    tid, cwd, log = world
    with pytest.raises(RuntimeError):
        with worker_usage.attempt(tid, 'claude', cwd, 'one', stage='implement'):
            append(log('one'), 'm1')
            raise RuntimeError('quota ended the run')
    with worker_usage.attempt(tid, 'copilot', cwd, 'two', stage='implement'):
        pass
    report = worker_usage.report(tid)
    assert [row['status'] for row in report['attempts']] == ['failed', 'returned']
    assert report['measured_attempts'] == 1 and not report['recorded_usage_complete']
    assert report['attempts'][1]['usage'] is None
    assert report['monetary_cost'] is None
    assert 'incomplete' in worker_usage.summary(tid)


def test_missing_log_and_cancelled_attempt_remain_visible(world):
    tid, cwd, log = world
    with pytest.raises(asyncio.CancelledError):
        with worker_usage.attempt(tid, 'claude', cwd, 'missing', stage='plan'):
            raise asyncio.CancelledError()
    report = worker_usage.report(tid)
    assert report['attempts'][0]['status'] == 'cancelled'
    assert report['known_subtotal'] is None
    assert 'not zero' in worker_usage.summary(tid)


def test_counter_reset_is_not_reported_as_negative_or_free(world):
    tid, cwd, log = world
    append(log('one'), 'old', output=100)
    with worker_usage.attempt(tid, 'claude', cwd, 'one', stage='implement'):
        log('one').write_text('')
        append(log('one'), 'new1')
        append(log('one'), 'new2')
    row = worker_usage.report(tid)['attempts'][0]
    assert row['usage_state'] == 'counter_reset' and row['usage'] is None


def test_actual_cli_wrapper_records_stage_and_assigns_analysis_session(world, monkeypatch):
    tid, cwd, log = world
    store.kv_set(f'graph_leg:{tid}', 'reproduce')
    async def fake(prompt, **kwargs):
        append(log(kwargs['session_id']), 'm1')
        return 'result'
    monkeypatch.setattr('app.claude_cli.one_shot', fake)
    assert asyncio.run(tasks._observed_cli(tid, 'claude', 'prompt', cwd=cwd)) == 'result'
    report = worker_usage.report(tid)
    assert report['recorded_usage_complete'] and report['attempts'][0]['stage'] == 'reproduce'
    assert report['attempts'][0]['session_id']
    from app import agent
    assert '1/1 recorded worker attempts measured' in agent.task_result(tid)
