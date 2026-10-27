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


#: Above this, a worker gets a pointer to the plan file instead of the plan.
PROMPT_LIMIT = 3000


def receipt(text: str) -> str:
    """The line at the END of a plan file: quoting it shows the whole file was read."""
    return 'Plan receipt: PLAN-' + hashlib.sha256(text.encode()).hexdigest()[:10]


def _write(folder: Path, name: str, content: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    if not path.exists() or path.read_text() != content:
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', dir=folder, delete=False) as out:
            out.write(content)
            temp = Path(out.name)
        temp.replace(path)
    return path.resolve()


def artifact(task_id: int, text: str | None = None) -> Path:
    """An immutable file for workers to read on demand; the DB remains canonical."""
    text = get(task_id) if text is None else text
    digest = hashlib.sha256(text.encode()).hexdigest()
    folder = store.DB_PATH.parent / 'task-plans' / str(int(task_id)) / digest
    return _write(folder, 'plan.md', f'{text}\n\n---\n{receipt(text)}\n')


def preview(task_id: int, text: str, title: str = '') -> Path:
    """The whole plan as a readable file for HIM, before he approves it.

    His phone gets a cut of the plan; this is the rest. One file per revision —
    a revised plan is a new file, never an overwrite of the one he read."""
    digest = hashlib.sha256(text.encode()).hexdigest()
    folder = store.DB_PATH.parent / 'task-plans' / str(int(task_id)) / digest
    head = f'# Plan for task #{task_id}' + (f' — {title.strip()}' if title.strip() else '')
    return _write(folder, f'plan-{int(task_id)}.md',
                  f'{head}\n\nRevision {digest[:10]}\n\n{text.strip()}\n')


def prompt_block(task_id: int, text: str, limit: int = PROMPT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    path = artifact(task_id, text)
    return (f'The full approved plan is in {path}. Read it before changing files or '
            'claiming completion. It was omitted from this prompt for size; this '
            'pointer is not a substitute for the plan. Its last line is a "Plan receipt"; '
            'quote that line exactly in your final report.')


def unread(task_id: int, result: str) -> str:
    """The receipt a worker should have quoted and did not, or ''.

    Only when it was handed a pointer: a plan short enough to sit in the prompt
    was read by construction."""
    spec = get(task_id) if enabled() else ''
    if len(spec) <= PROMPT_LIMIT:
        return ''
    expected = receipt(spec)
    return '' if expected in (result or '') else expected


def preamble(task_id: int) -> str:
    spec = get(task_id) if enabled() else ''
    if not spec:
        return ''
    return ('[Definition of done — the latest plan Arun approved. Implement exactly this; '
            'report a material conflict rather than silently changing scope.]\n'
            + prompt_block(task_id, spec) + '\n\n')
