import json

from app import context_pack, store, task_spec, tasks


def test_plan_evidence_survives_large_job_notes(monkeypatch):
    from app.graph import notes
    t = store.create_task('Flow', 'code', 'change flow', 'test')
    monkeypatch.setattr(notes, 'block', lambda tid: 'notes' * 860)
    monkeypatch.setattr(context_pack, '_how_it_works', lambda t: '[Project evidence]\n' + 'flow' * 200)
    text = context_pack.build(t['id'], 'plan')
    assert '[Project evidence]' in text
    assert 'omitted' in text and len(text) <= context_pack.CAPS['plan']
    manifest = json.loads(store.kv_get(f"context_manifest:{t['id']}:plan"))
    assert manifest['omitted'] and manifest['workspace'] == 'test'


def test_fresh_implementation_receives_latest_complete_plan(tmp_path):
    t = store.create_task('Flow', 'code', 'change flow', None)
    task_spec.capture(t['id'], 'old plan')
    full = 'new plan\n' + 'requirements\n' * 700 + 'last requirement'
    task_spec.capture(t['id'], full)
    prompt = context_pack.build(t['id'], 'implement')
    assert 'old plan' not in prompt
    assert str(task_spec.artifact(t['id'])) in prompt
    assert task_spec.artifact(t['id']).read_text() == full
    assert len(prompt) <= context_pack.CAPS['implement']
    task_spec.capture(t['id'], full)
    assert len(task_spec.history(t['id'])) == 2


def test_approved_change_proposal_updates_the_definition_of_done():
    t = store.create_task('Flow', 'code', 'change flow', None)
    task_spec.capture(t['id'], 'original')
    store.kv_set(f"task_proposal:{t['id']}", json.dumps({'plan': 'revised plan', 'status': 'done'}))
    tasks._apply_proposal(t['id'])
    assert task_spec.get(t['id']) == 'revised plan'
    assert task_spec.history(t['id'])[0]['text'] == 'original'


def test_disabled_history_cannot_override_a_newer_approved_plan(monkeypatch):
    t = store.create_task('Flow', 'code', 'change flow', None)
    task_spec.capture(t['id'], 'old approval')
    monkeypatch.setenv('ASTA_TASK_SPEC', '0')
    store.kv_set(f"task_plan:{t['id']}", 'new approval')
    assert context_pack._approved_plan(t['id']) == 'new approval'


def test_approval_amendment_survives_a_new_session():
    t = store.create_task('Flow', 'code', 'change flow', None)
    store.update_task(t['id'], result='Use exponential backoff')
    tasks.record_answer(t['id'], store.get_task(t['id']), 'PLAN APPROVED with a maximum of five attempts')
    assert 'maximum of five attempts' in task_spec.get(t['id'])
    assert 'maximum of five attempts' in context_pack.build(t['id'], 'implement')
