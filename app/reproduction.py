"""An explicitly requested local reproduction precedes the code-plan gate.

This is workflow scope and evidence validation, not an OS sandbox. Workers may
write tests locally; production-file changes are detected and stop progression.
Only trusted user entry points call request(), never a worker's task brief.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from . import store

ACTIVE = contextvars.ContextVar("asta_reproduction_task", default=None)


def asked(text: str) -> bool:
    text = (text or "").replace("’", "'").strip()
    text = re.sub(r"^task\s*#?\d+\s*[-:,]*\s*", "", text, flags=re.I)
    if re.match(r"(?:did|does|have|has|was|were|why|what|is|are)\b", text, re.I):
        return False
    if re.search(r"(?:don'?t|do not|never)\s+(?:try to\s+)?(?:replicate|reproduce|recreate)\b", text, re.I):
        return False
    return bool(re.search(r"\b(?:replicate|reproduce|recreate)\b[^.!?\n]{0,100}\b(?:first|scenario|issue|bug|failure)\b", text, re.I))


def request(task_id: int, words: str) -> None:
    task = store.get_task(task_id)
    if not task or task['kind'] != 'code':
        raise ValueError('Reproduction needs an existing code task')
    store.kv_set(f'reproduction:{task_id}', json.dumps(
        {'state': 'pending', 'words': words, 'requested_at': time.time()}))
    store.add_task_event(task_id, 'reproduction', 'Local reproduction requested before planning')


def state(task_id: int) -> dict:
    return json.loads(store.kv_get(f'reproduction:{task_id}') or '{}')


def pending(task_id: int) -> bool:
    return state(task_id).get('state') == 'pending'


def fix_authorized(task_id: int) -> bool:
    """Only the user's conditional fix instruction, after an observed failing test."""
    saved = state(task_id)
    words = saved.get('words', '').replace('’', "'")
    if saved.get('state') != 'confirmed' or re.search(r"\b(?:don'?t|do not|never|before|hold)\b[^.!?\n]{0,50}\bfix", words, re.I):
        return False
    return bool(re.search(r"\b(?:if|once)\b[^.!?\n]{0,90}\b(?:reproduc\w*|replicat\w*|confirm\w*)\b[^.!?\n]{0,70}\bfix\b", words, re.I))


def active(task_id: int) -> bool:
    return ACTIVE.get() == task_id and pending(task_id)


def production_snapshot(cwd: str) -> dict[str, str]:
    """Hash tracked production files in every task repo; test/build output excluded.

    Reads current files, not just the diff: a worker committing a production edit
    must not hide it. Existing dirty content is preserved and compared as-is.
    """
    root = Path(cwd)
    repos = [root] if (root / '.git').exists() else [d for d in root.iterdir() if (d / '.git').exists()]
    if not repos:
        raise ValueError('No task checkout found for reproduction')
    found = {}
    for repo in repos:
        out = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                             cwd=repo, check=True, capture_output=True).stdout
        for name in out.decode().split('\0'):
            if not name:
                continue
            f = Path(name)
            if ('test' in f.parts or 'tests' in f.parts or '__tests__' in f.parts
                    or f.name.startswith('test_') or f.name.endswith(('_test.py', '.test.js', '.test.ts', '.spec.ts'))):
                continue
            file = repo / f
            value = (('symlink:' + str(file.readlink())).encode() if file.is_symlink()
                     else file.read_bytes() if file.is_file() else b'<missing>')
            found[str(file)] = hashlib.sha256(value).hexdigest()
    return found


def evidence(text: str, cwd: str, since: float) -> dict:
    blocks = re.findall(r"```reproduction\s*(\{.*?\})\s*```", text, re.S)
    if not blocks:
        raise ValueError('No reproduction evidence supplied; code tracing is not a reproduction')
    row = json.loads(blocks[-1])
    if row.get('status') not in ('confirmed', 'not_reproduced'):
        raise ValueError(row.get('reason') or 'Reproduction could not run')
    if not all(isinstance(row.get(k), str) and row[k].strip() for k in ('report', 'command', 'expected', 'actual')):
        raise ValueError('Reproduction needs the test command, report, expected and actual behavior')
    root = Path(cwd).resolve()
    report = (root / row['report']).resolve()
    if root not in report.parents or not report.is_file():
        raise ValueError('Test report must be inside this task checkout')
    if report.stat().st_mtime < since or report.stat().st_size > 5_000_000:
        raise ValueError('Test report is stale or too large')
    suite = ET.parse(report).getroot()
    cases = list(suite.iter('testcase'))
    ran = [c for c in cases if c.find('skipped') is None]
    if not ran or any(c.find('error') is not None for c in ran):
        raise ValueError('No executed assertion results; build/setup errors do not reproduce the bug')
    failures = sum(c.find('failure') is not None for c in ran)
    if (row['status'] == 'confirmed') != bool(failures):
        raise ValueError('Reported reproduction disagrees with the test results')
    return {**row, 'tests': len(ran), 'failures': failures, 'report': str(report),
            'report_sha256': hashlib.sha256(report.read_bytes()).hexdigest()}


async def run(task_id: int, task: dict, leg) -> dict:
    from . import tasks, notify
    from .graph import notes
    cwd = tasks.task_cwd(task_id, task['workspace'])
    saved = state(task_id)
    before = saved.get('production_before')
    if before is None:
        before = production_snapshot(cwd)
        store.kv_set(f'reproduction:{task_id}', json.dumps({**saved, 'production_before': before}))
    # A planning session carries instructions to ask for approval. Start a fresh
    # reproduction session once; quota retries retain that session and scope.
    if store.kv_get(f'graph_leg:{task_id}') != 'reproduce':
        for ex in tasks._executor_names():
            store.kv_del(f'task_session:{task_id}:{ex}')
    prompt = (
        'REPRODUCTION FIRST. Do not write a solution plan or implement a fix. '
        'You are authorized to add/edit LOCAL TESTS and fixtures and run scoped tests in this task checkout. '
        'Do not edit production code, build configuration, commit, push, create a PR, or send messages. '
        'Exercise the real relevant method/path with synthetic inputs, not a mock of the behavior under test. '
        'A code trace is not reproduction. Preserve existing files. If compilation/dependencies/access fail, '
        'report blocked; never call those failures the target bug. Generate a fresh JUnit XML report '
        '(Maven Surefire or pytest --junitxml). Do not fabricate or edit the report. '
        'Report the actual assertion, expected/actual result and command. If tests pass, say not reproduced. '
        'End with a fenced reproduction JSON object: '
        '{"status":"confirmed|not_reproduced|blocked","report":"relative/path/to/junit.xml",'
        '"command":"exact scoped command","expected":"expected behavior","actual":"observed behavior",'
        '"reason":"if blocked"}. This evidence block is the completion of this stage; do not produce a generic coding outcome.\n\n'
        + task['prompt'] + '\n\nLatest user instruction:\n' + state(task_id)['words'])
    since = time.time()
    token = ACTIVE.set(task_id)
    try:
        text, _ = await leg(task_id, 'reproduce', prompt, effort=tasks._effort_for('code', tasks._resolve_executor(task_id)),
                            resume_prompt=prompt)
    finally:
        ACTIVE.reset(token)
    try:
        if production_snapshot(cwd) != before:
            raise ValueError('Production files changed during reproduction; inspect the retained diff before continuing')
        result = evidence(text, cwd, since)
    except (ValueError, OSError, ET.ParseError) as exc:
        result = {'status': 'blocked', 'reason': str(exc)}
    record = {**state(task_id), 'state': result['status'], 'evidence': result, 'at': time.time()}
    store.kv_set(f'reproduction:{task_id}', json.dumps(record))
    store.add_task_event(task_id, 'reproduction', json.dumps(result)[:2000])
    if result['status'] == 'confirmed':
        notes.add(task_id, 'Reproduction test failed: ' + json.dumps(result))
        store.kv_del(f'graph_leg:{task_id}')
        for ex in tasks._executor_names():
            store.kv_del(f'task_session:{task_id}:{ex}')
        await notify.notify(f"Task #{task_id}: reproduction assertion failed; now planning from this evidence.\n{text}", 'task')
        return {'reproduction_next': 'plan', 'answer': {}, 'text': text}
    status = 'done' if result['status'] == 'not_reproduced' else 'failed'
    detail = ('Scenario tested but not reproduced; no production fix made.' if status == 'done'
              else 'Reproduction blocked: ' + result['reason'])
    store.update_task(task_id, status=status, result=detail + '\n\n' + text,
                      error='' if status == 'done' else detail, finished_at=time.time())
    store.kv_del(f'task_gate:{task_id}')
    await notify.notify(f'Task #{task_id}: {detail}\n{text}', 'task')
    return {'reproduction_next': 'end', 'answer': {}, 'text': text}
