"""Versioned approved plans, independent of a model session.

The latest approval is authoritative. Older versions remain available for
review; prompt budgets never truncate the stored definition of done.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from . import store


def enabled() -> bool:
    return (os.environ.get('ASTA_TASK_SPEC') or '1').strip().lower() in ('1', 'true', 'yes', 'on')


def _key(task_id: int) -> str:
    return f'task_spec:{task_id}'


def capture(task_id: int, plan_text: str) -> None:
    """Keep the full approved revision atomically; identical approvals are no-ops."""
    if not enabled() or not (plan_text or '').strip():
        return
    text = plan_text.strip()
    key = _key(task_id)
    with store._connect() as conn:
        # Acquire the write lock before reading the current version. Another
        # process cannot overwrite the history between the read and the update.
        conn.execute('INSERT OR IGNORE INTO kv(key,value) VALUES (?,?)', (key, ''))
        prior = conn.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()['value']
        if prior == text:
            return
        history_key = f'task_spec_history:{task_id}'
        row = conn.execute('SELECT value FROM kv WHERE key=?', (history_key,)).fetchone()
        history = json.loads(row['value']) if row else []
        if prior and not history:
            history.append({'version': 1, 'text': prior, 'approved_at': None,
                            'sha256': hashlib.sha256(prior.encode()).hexdigest()})
        history.append({'version': len(history) + 1, 'text': text, 'approved_at': time.time(),
                        'sha256': hashlib.sha256(text.encode()).hexdigest()})
        for name, value in ((history_key, json.dumps(history)), (key, text),
                            (f'task_plan:{task_id}', text)):
            conn.execute('INSERT INTO kv(key,value) VALUES (?,?) ON CONFLICT(key) '
                         'DO UPDATE SET value=excluded.value', (name, value))


def history(task_id: int) -> list[dict]:
    return json.loads(store.kv_get(f'task_spec_history:{task_id}') or '[]')


def get(task_id: int) -> str:
    return store.kv_get(_key(task_id)) or ''


def artifact(task_id: int, text: str | None = None) -> Path:
    """An immutable file for workers to read on demand; the DB remains canonical."""
    text = get(task_id) if text is None else text
    digest = hashlib.sha256(text.encode()).hexdigest()
    folder = store.DB_PATH.parent / 'task-plans' / str(int(task_id)) / digest
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'plan.md'
    if not path.exists() or path.read_text() != text:
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', dir=folder, delete=False) as out:
            out.write(text)
            temp = Path(out.name)
        temp.replace(path)
    return path.resolve()


def prompt_block(task_id: int, text: str, limit: int = 3000) -> str:
    if len(text) <= limit:
        return text
    path = artifact(task_id, text)
    return (f'The full approved plan is in {path}. Read it before changing files or '
            'claiming completion. It was omitted from this prompt for size; this '
            'pointer is not a substitute for the plan.')


def preamble(task_id: int) -> str:
    spec = get(task_id) if enabled() else ''
    if not spec:
        return ''
    return ('[Definition of done — the latest plan Arun approved. Implement exactly this; '
            'report a material conflict rather than silently changing scope.]\n'
            + prompt_block(task_id, spec) + '\n\n')
