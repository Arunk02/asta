import hashlib
import pytest
from app import learn, skills

CHECKS = ('outcome', 'authority')


def report(path, **changes):
    return {'run_id': 'fixture', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'total': 2, 'passed': 2, 'checks': dict.fromkeys(CHECKS, 'passed'), **changes}


DATA = {'title': 'Check result', 'procedure': ['Read back the changed setting'],
        'confidence': 0.99, 'verification': ['Observed setting equals requested setting']}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, 'SKILLS_DIR', tmp_path / 'skills')
    monkeypatch.setattr(learn, 'USAGE_FILE', tmp_path / 'skills' / '.usage.json')


def test_model_confidence_and_fake_validated_field_do_not_activate_instructions():
    candidate = learn.write_skill({**DATA, 'validated': True})
    assert candidate.exists()
    assert skills.discover() == []
    assert skills.load('check-result') is None


@pytest.mark.asyncio
async def test_independent_checks_promote_the_exact_candidate():
    candidate = learn.write_skill(DATA)
    async def evaluate(path):
        assert path == candidate and skills.discover() == []
        return report(path)
    promoted = await learn.validate_candidate('check-result', evaluate, required_checks=CHECKS)
    assert promoted.parent == skills.SKILLS_DIR
    assert 'Read back' in skills.load('check-result')


@pytest.mark.parametrize('report', [{}, {'run_id': 'empty', 'total': 0, 'passed': 0},
                                    {'run_id': 'failed', 'total': 2, 'passed': 1}])
@pytest.mark.asyncio
async def test_missing_or_failed_evidence_leaves_candidate_inactive(report):
    learn.write_skill(DATA)
    async def evaluate(path):
        return report
    with pytest.raises(ValueError):
        await learn.validate_candidate('check-result', evaluate, required_checks=CHECKS)
    assert skills.discover() == []


@pytest.mark.asyncio
async def test_validation_cannot_promote_a_changed_candidate():
    learn.write_skill(DATA)
    async def evaluate(path):
        evidence = report(path)
        path.write_text('new instructions that were never evaluated')
        return evidence
    with pytest.raises(ValueError, match='changed'):
        await learn.validate_candidate('check-result', evaluate, required_checks=CHECKS)
    assert skills.discover() == []


def test_an_extracted_update_preserves_the_current_active_skill():
    active = learn.write_skill(DATA, validated=True)
    original = active.read_text()
    learn.write_skill({**DATA, 'procedure': ['Unproven extra action']})
    assert active.read_text() == original
    assert 'Unproven' not in skills.load('check-result')


def test_candidate_does_not_lower_active_confidence_or_prune_it():
    active = learn.write_skill(DATA, validated=True)
    learn.write_skill({**DATA, 'confidence': 0.61})
    assert learn.stats()['check-result']['confidence'] == 0.99
    assert learn.prune() == []
    assert active.exists()


@pytest.mark.asyncio
async def test_preexisting_candidate_metadata_mismatch_cannot_activate_untested_text():
    candidate = learn.write_skill(DATA)
    candidate.write_text('A harmless document that the evaluator inspected')
    async def evaluate(path):
        assert path.read_text() == 'A harmless document that the evaluator inspected'
        return report(path)
    with pytest.raises(ValueError, match='content'):
        await learn.validate_candidate('check-result', evaluate, required_checks=CHECKS)
    assert skills.discover() == []


@pytest.mark.asyncio
async def test_aggregate_success_with_skipped_checks_is_not_validation():
    learn.write_skill(DATA)
    async def evaluate(path):
        return report(path, skipped=3)
    with pytest.raises(ValueError):
        await learn.validate_candidate('check-result', evaluate, required_checks=CHECKS)
    assert skills.discover() == []


@pytest.mark.parametrize('changes', [
    {'checks': {'outcome': 'passed'}},
    {'checks': {'outcome': 'passed', 'different-check': 'passed'}},
    {'checks': {'outcome': 'passed', 'authority': 'skipped'}},
    {'checks': {'outcome': 'passed', 'authority': 'failed'}},
    {'checks': {'outcome': 'passed', 'authority': 'passed', 'extra': 'failed'}},
    {'total': 1, 'passed': 1}, {'total': True, 'passed': True},
    {'sha256': 'some-other-candidate'}, {'errors': 1}, {'failed': 1},
    {'skipped': True}, {'run_id': '  '},
])
@pytest.mark.asyncio
async def test_complete_named_suite_is_required(changes):
    learn.write_skill(DATA)
    async def evaluate(path):
        return report(path, **changes)
    with pytest.raises(ValueError, match='independent validation'):
        await learn.validate_candidate('check-result', evaluate, required_checks=CHECKS)
    assert skills.discover() == []


@pytest.mark.parametrize('policy', [(), ('outcome', 'outcome'), ('',), 'outcome'])
@pytest.mark.asyncio
async def test_missing_or_invalid_policy_never_runs_evaluator(policy):
    learn.write_skill(DATA)
    async def evaluate(path):
        pytest.fail('No independently configured suite')
    with pytest.raises(ValueError, match='policy'):
        await learn.validate_candidate('check-result', evaluate, required_checks=policy)


@pytest.mark.asyncio
async def test_update_promotes_identical_bytes_and_retains_exact_predecessor(monkeypatch):
    active = learn.write_skill(DATA, validated=True)
    predecessor = active.read_bytes()
    candidate = learn.write_skill({**DATA, 'procedure': ['Then verify the persisted value']})
    expected = candidate.read_bytes()
    async def evaluate(path):
        assert active.read_bytes() == predecessor
        return report(path)
    # Promotion must not call the writer that merges/re-renders instructions.
    monkeypatch.setattr(learn, 'write_skill', lambda *a, **kw: pytest.fail('re-rendered'))
    promoted = await learn.validate_candidate('check-result', evaluate, required_checks=CHECKS)
    assert promoted.read_bytes() == expected
    digest = hashlib.sha256(predecessor).hexdigest()
    assert (skills.SKILLS_DIR / '.history' / 'check-result' / f'{digest}.md').read_bytes() == predecessor
    assert learn.stats()['check-result']['validated_sha256'] == hashlib.sha256(expected).hexdigest()


@pytest.mark.asyncio
async def test_active_update_during_validation_is_preserved():
    active = learn.write_skill(DATA, validated=True)
    learn.write_skill({**DATA, 'procedure': ['Candidate step']})
    async def evaluate(path):
        active.write_text('A newer approved procedure')
        return report(path)
    with pytest.raises(ValueError, match='active skill changed'):
        await learn.validate_candidate('check-result', evaluate, required_checks=CHECKS)
    assert active.read_text() == 'A newer approved procedure'
