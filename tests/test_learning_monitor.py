import hashlib
import pytest
from app import learn, learning_monitor, skills, store

CHECKS = ('behavior', 'authority')
DATA = {'title': 'Monitor test', 'procedure': ['Original behavior'], 'confidence': .99}

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, 'SKILLS_DIR', tmp_path / 'skills')
    monkeypatch.setattr(learn, 'USAGE_FILE', tmp_path / 'skills' / '.usage.json')


def evidence(path, fail=False, **extra):
    return dict(run_id='independent', sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                total=2, passed=1 if fail else 2, failed=1 if fail else 0,
                checks={'behavior': 'failed' if fail else 'passed', 'authority': 'passed'}, **extra)

async def promote():
    active = learn.write_skill(DATA, validated=True)
    original = active.read_bytes()
    learn.write_skill({**DATA, 'procedure': ['Broken extra behavior']})
    async def good(path): return evidence(path)
    await learn.validate_candidate('monitor-test', good, required_checks=CHECKS)
    return active, original

@pytest.mark.asyncio
async def test_verified_regression_restores_exact_retested_predecessor():
    active, original = await promote()
    calls = []
    async def evaluator(path):
        assert path != active
        calls.append(path.read_bytes())
        return evidence(path, fail=b'Broken extra' in path.read_bytes())
    result = await learning_monitor.recheck('monitor-test', evaluator, required_checks=CHECKS)
    assert result['status'] == 'rolled_back'
    assert active.read_bytes() == original
    assert len(calls) == 2
    assert learn.stats()['monitor-test']['validated_sha256'] == hashlib.sha256(original).hexdigest()
    assert store.kv_get('learning_monitor:monitor-test:' + result['sha256'])

@pytest.mark.parametrize('mode', ['healthy', 'skipped', 'wrong_hash', 'both_fail'])
@pytest.mark.asyncio
async def test_uncertain_or_unhelpful_evidence_never_changes_active(mode):
    active, _ = await promote()
    before = active.read_bytes()
    async def evaluator(path):
        result = evidence(path, fail=mode != 'healthy')
        if mode == 'skipped': result['skipped'] = 1
        if mode == 'wrong_hash': result['sha256'] = '0' * 64
        return result
    result = await learning_monitor.recheck('monitor-test', evaluator, required_checks=CHECKS)
    assert result['status'] != 'rolled_back'
    assert active.read_bytes() == before

@pytest.mark.asyncio
async def test_concurrent_edit_is_preserved():
    active, _ = await promote()
    async def evaluator(path):
        failed = b'Broken extra' in path.read_bytes()
        if not failed: active.write_text('New human edit')
        return evidence(path, fail=failed)
    with pytest.raises(ValueError, match='changed'):
        await learning_monitor.recheck('monitor-test', evaluator, required_checks=CHECKS)
    assert active.read_text() == 'New human edit'

@pytest.mark.asyncio
async def test_no_policy_cannot_run_evaluator():
    await promote()
    async def evaluator(path): raise AssertionError('must not execute')
    with pytest.raises(ValueError, match='policy'):
        await learning_monitor.recheck('monitor-test', evaluator)

@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
@pytest.mark.asyncio
async def test_untrusted_archive_cannot_be_restored(damage):
    active, _ = await promote()
    before = active.read_bytes()
    previous = learn.stats()['monitor-test']['predecessor_sha256']
    archive = skills.SKILLS_DIR / '.history' / 'monitor-test' / f'{previous}.md'
    if damage == 'missing': archive.unlink()
    else: archive.write_text('corrupt instructions')
    async def evaluator(path): return evidence(path, fail=True)
    result = await learning_monitor.recheck('monitor-test', evaluator, required_checks=CHECKS)
    assert result['status'] == 'regression_without_verified_predecessor'
    assert active.read_bytes() == before

@pytest.mark.asyncio
async def test_modified_active_lesson_cannot_be_rolled_back():
    active, _ = await promote()
    active.write_text('human replacement')
    async def evaluator(path): raise AssertionError('must not execute')
    with pytest.raises(ValueError, match='validated version'):
        await learning_monitor.recheck('monitor-test', evaluator, required_checks=CHECKS)

@pytest.mark.asyncio
async def test_evaluator_mutating_snapshot_does_not_activate_it():
    active, _ = await promote()
    before = active.read_bytes()
    async def evaluator(path):
        result = evidence(path, fail=True)
        path.write_text('injected')
        return result
    with pytest.raises(ValueError, match='snapshot'):
        await learning_monitor.recheck('monitor-test', evaluator, required_checks=CHECKS)
    assert active.read_bytes() == before
