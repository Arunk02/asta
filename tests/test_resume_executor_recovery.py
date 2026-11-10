from app import tasks, store


def test_sticky_executor_switches_only_when_unavailable(monkeypatch):
    store.kv_set('task_executor:71', 'copilot')
    store.kv_set('task_session:71:copilot', 'old-session')
    monkeypatch.setattr(tasks, '_can_take_over', lambda ex: ex == 'claude')
    assert tasks._resume_executor(71) == ('claude', True)
    assert store.kv_get('task_executor:71') == 'claude'
    assert store.kv_get('task_session:71:copilot') == 'old-session'
    assert not store.kv_get('task_session:71:claude')


def test_available_sticky_executor_is_preserved(monkeypatch):
    store.kv_set('task_executor:71', 'copilot')
    monkeypatch.setattr(tasks, '_can_take_over', lambda ex: True)
    assert tasks._resume_executor(71) == ('copilot', False)


def test_no_available_executor_preserves_state(monkeypatch):
    store.kv_set('task_executor:71', 'copilot')
    monkeypatch.setattr(tasks, '_can_take_over', lambda ex: False)
    assert tasks._resume_executor(71) == ('', False)
    assert store.kv_get('task_executor:71') == 'copilot'
