"""Recheck an activated lesson and restore only a revalidated predecessor.

The evaluator and mandatory checks must come from trusted application policy,
never from extracted instructions. Unrelated task failure is not a rollback vote.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from . import learn, skills, store


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _verdict(report, digest: str, checks: tuple[str, ...]) -> str:
    if not isinstance(report, dict):
        return 'unknown'
    outcomes = report.get('checks')
    if not (isinstance(report.get('run_id'), str) and report['run_id'].strip()
            and report.get('sha256') == digest
            and isinstance(outcomes, dict) and set(outcomes) == set(checks)
            and all(v in ('passed', 'failed') for v in outcomes.values())
            and type(report.get('total')) is int and report['total'] == len(checks)
            and type(report.get('passed')) is int
            and report['passed'] == sum(v == 'passed' for v in outcomes.values())
            and type(report.get('failed', 0)) is int
            and report.get('failed', 0) == sum(v == 'failed' for v in outcomes.values())
            and all(type(report.get(k, 0)) is int and report.get(k, 0) == 0
                    for k in ('errors', 'skipped'))):
        return 'unknown'
    return 'passed' if report['passed'] == len(checks) else 'failed'


async def recheck(name: str, evaluator, *, required_checks: tuple[str, ...] = ()) -> dict:
    """Return a durable decision; restore only after current fails and prior passes.

    No scheduler/extractor calls this without a task-specific trusted policy.
    Evaluators inspect immutable snapshots outside the active catalog, so testing
    a predecessor never temporarily activates it for other tasks.
    """
    if name != learn._slug(name):
        raise ValueError('invalid lesson name')
    if (not isinstance(required_checks, tuple) or not required_checks
            or any(not isinstance(c, str) or not c.strip() for c in required_checks)
            or len(set(required_checks)) != len(required_checks)):
        raise ValueError('an independent mandatory-check policy is required')
    usage = learn._usage()
    entry = usage.get(name, {})
    active = learn._existing_match(name)
    if not active or active.is_symlink():
        raise ValueError('active lesson is missing or is a symlink')
    body = active.read_bytes()
    digest = _digest(body)
    if entry.get('validated_sha256') != digest:
        raise ValueError('active lesson does not match its validated version')
    previous = entry.get('predecessor_sha256')
    decision = {'name': name, 'sha256': digest, 'at': time.time(),
                'required_checks': required_checks, 'status': 'unknown'}

    async def evaluate(content: bytes):
        with tempfile.TemporaryDirectory(prefix='asta-lesson-check-') as directory:
            snapshot = Path(directory) / f'{name}.md'
            snapshot.write_bytes(content)
            result = await evaluator(snapshot)
            if snapshot.read_bytes() != content:
                raise ValueError('evaluator changed the lesson snapshot')
            return result

    current_result = await evaluate(body)
    decision['current_result'] = current_result
    verdict = _verdict(current_result, digest, required_checks)
    decision['status'] = 'healthy' if verdict == 'passed' else 'unverified'
    if verdict == 'failed':
        decision['status'] = 'regression_without_verified_predecessor'
        if isinstance(previous, str) and len(previous) == 64 and all(c in '0123456789abcdef' for c in previous):
            archive = skills.SKILLS_DIR / '.history' / name / f'{previous}.md'
            if archive.is_file() and not archive.is_symlink():
                prior_body = archive.read_bytes()
                if _digest(prior_body) == previous:
                    prior_result = await evaluate(prior_body)
                    decision['predecessor_result'] = prior_result
                    if _verdict(prior_result, previous, required_checks) == 'passed':
                        # Re-read immediately before replacement: no intervening edit,
                        # promotion or metadata change may be silently overwritten.
                        latest = learn._usage()
                        if (active.read_bytes() != body or latest.get(name) != entry
                                or archive.read_bytes() != prior_body):
                            raise ValueError('lesson changed during monitoring')
                        with tempfile.NamedTemporaryFile(dir=active.parent, delete=False) as handle:
                            temporary = Path(handle.name)
                            handle.write(prior_body)
                        try:
                            os.replace(temporary, active)
                        finally:
                            temporary.unlink(missing_ok=True)
                        latest[name].update(validated_sha256=previous,
                                            validation_run_id=prior_result['run_id'],
                                            predecessor_sha256=None, updated=time.time(),
                                            rolled_back_from=digest)
                        learn._save_usage(latest)
                        decision['status'] = 'rolled_back'
                        decision['restored_sha256'] = previous
    store.kv_set(f'learning_monitor:{name}:{digest}', json.dumps(decision))
    store.record_outcome('skill', decision['status'], subject=name,
                         detail=f'independent lesson recheck: {digest}')
    return decision
