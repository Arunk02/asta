import pytest
from app import learn, skills

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
        return {'run_id': 'fixture-regression-1', 'total': 2, 'passed': 2}
    promoted = await learn.validate_candidate('check-result', evaluate)
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
        await learn.validate_candidate('check-result', evaluate)
    assert skills.discover() == []


@pytest.mark.asyncio
async def test_validation_cannot_promote_a_changed_candidate():
    learn.write_skill(DATA)
    async def evaluate(path):
        path.write_text('new instructions that were never evaluated')
        return {'run_id': 'fixture', 'total': 1, 'passed': 1}
    with pytest.raises(ValueError, match='changed'):
        await learn.validate_candidate('check-result', evaluate)
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
