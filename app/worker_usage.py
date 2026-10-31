"""Durable worker attempts and measured session deltas; never infer missing usage.

Records survive session rotation, fallback and failure. This is token accounting,
not a price calculator or proof that a finished task was independently verified.
"""
from __future__ import annotations

import contextlib
import json
import time
import uuid

from . import store, token_audit

_FIELDS = ('calls', 'new_tokens', 'cache_read', 'out_tokens')


def _snapshot(executor: str, cwd: str, session_id: str) -> dict | None:
    if executor != 'claude':
        # Copilot's current parser does not establish complete input/cache usage.
        return None
    path = token_audit.CLAUDE_PROJECTS / token_audit._encode_project(cwd) / f'{session_id}.jsonl'
    if not path.is_file():
        return dict.fromkeys(_FIELDS, 0)
    try:
        parsed = token_audit._parse_claude(path)
        return {field: parsed[field] for field in _FIELDS}
    except (OSError, ValueError, TypeError):
        return None


@contextlib.contextmanager
def attempt(task_id: int, executor: str, cwd: str, session_id: str, *,
            stage: str, model: str = ''):
    """One actual CLI invocation; cumulative session counters become leg deltas."""
    run_id = uuid.uuid4().hex
    key = f'worker_usage:{task_id}:{run_id}'
    before = _snapshot(executor, cwd, session_id)
    row = {'version': 1, 'run_id': run_id, 'task_id': task_id,
           'executor': executor, 'cwd': cwd, 'session_id': session_id,
           'stage': stage, 'configured_model': model or 'default',
           'started_at': time.time(), 'finished_at': None,
           'status': 'running', 'usage': None, 'usage_state': 'pending'}
    store.kv_set(key, json.dumps(row))
    try:
        yield run_id
    except BaseException as exc:
        row['status'] = 'cancelled' if type(exc).__name__ == 'CancelledError' else 'failed'
        row['error_type'] = type(exc).__name__
        raise
    else:
        row['status'] = 'returned'
    finally:
        after = _snapshot(executor, cwd, session_id)
        row['finished_at'] = time.time()
        row['usage_state'] = 'unavailable'
        if before is not None and after is not None and after['calls'] > before['calls']:
            delta = {field: after[field] - before[field] for field in _FIELDS}
            if all(value >= 0 for value in delta.values()):
                row['usage'] = delta
                row['usage_state'] = 'provider_reported'
            else:
                row['usage_state'] = 'counter_reset'
        store.kv_set(key, json.dumps(row))


def report(task_id: int) -> dict:
    """Known subtotal and explicit coverage; missing attempts never count as zero."""
    rows = [json.loads(raw) for raw in store.kv_like(f'worker_usage:{int(task_id)}:%').values()]
    rows.sort(key=lambda row: (row['started_at'], row['run_id']))
    known = [row for row in rows if row.get('usage') is not None]
    subtotal = {field: sum(row['usage'][field] for row in known) for field in _FIELDS} if known else None
    return {'task_id': task_id, 'attempts': rows, 'measured_attempts': len(known),
            'total_attempts': len(rows), 'recorded_usage_complete': bool(rows) and len(known) == len(rows)
            and all(row['status'] == 'returned' for row in rows),
            'known_subtotal': subtotal, 'monetary_cost': None,
            'note': 'New input includes cache creation. Cached input is separate. No price or missing usage is inferred.'}


def summary(task_id: int) -> str:
    data = report(task_id)
    if not data['attempts']:
        return ''
    counts = data['known_subtotal']
    coverage = f"{data['measured_attempts']}/{data['total_attempts']} recorded worker attempts measured"
    if counts is None:
        return f'Worker usage: unavailable ({coverage}); not zero.'
    return (f"Worker usage: known subtotal {counts['new_tokens']:,} new input "
            f"(includes cache creation), {counts['cache_read']:,} cached input, "
            f"{counts['out_tokens']:,} output tokens; {coverage}. "
            + ('' if data['recorded_usage_complete'] else 'Total usage is incomplete. ')
            + 'Monetary cost is unavailable.')
