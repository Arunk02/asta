"""Consolidation preserves evidence, and recall respects recorded project scope."""
import hashlib
import json

import pytest

from app import memory, store


@pytest.fixture
def mem(tmp_path, monkeypatch):
    root = tmp_path / 'memory'
    for name, value in {'MEMORY_DIR':root, 'FACTS_DIR':root/'facts',
                        'EPISODES_DIR':root/'episodes', 'INDEX_FILE':root/'MEMORY.md'}.items():
        monkeypatch.setattr(memory, name, value)
    monkeypatch.setattr(memory, 'local_llm_complete', lambda *a, **k: None)
    monkeypatch.setattr(memory, 'local_embed', lambda *a: None)
    memory.ensure_dirs()
    return root


def test_same_prefix_different_facts_survive_nightly_consolidation(mem):
    title = 'Deployment settings for the primary shared runtime '
    first = memory.remember(title + 'booking', 'Booking uses namespace alpha.', workspace='booking')
    second = memory.remember(title + 'billing', 'Billing uses namespace beta.', workspace='billing')
    before = {p: memory.read_memory_file(p) for p in (first, second)}
    memory.consolidate()
    assert {p: memory.read_memory_file(p) for p in (first, second)} == before
    manifest, = list((mem/'.history').glob('*.json'))
    for path, digest in json.loads(manifest.read_text()).items():
        assert (mem/'.history'/f'{digest}.md').read_text() == before[path]


def test_more_than_thirty_episodes_remain_searchable(mem):
    for i in range(40):
        (memory.EPISODES_DIR/f'2025-{i:03}.md').write_text(
            f'---\ntitle: Historical rootcause {i}\ntype: episode\n---\nRareFailure{i} fixed.')
    memory.consolidate()
    assert len(list(memory.EPISODES_DIR.glob('*.md'))) == 40
    assert store.memory_search('RareFailure0')
    assert len(memory.index_text()) <= memory.INDEX_MAX_CHARS


def test_changed_conversation_digest_preserves_previous_version(mem, monkeypatch):
    conv = store.create_conversation('claude', 'booking')
    messages = [{'id':1, 'role':'user', 'content':'Initial diagnosis'}]
    monkeypatch.setattr(store, 'list_ui_messages', lambda cid: list(messages))
    first = memory.write_episode(conv)
    before = memory.read_memory_file(first)
    messages.append({'id':2, 'role':'assistant', 'content':'Corrected diagnosis'})
    second = memory.write_episode(conv)
    assert first != second and memory.read_memory_file(first) == before
    assert 'Corrected diagnosis' in memory.read_memory_file(second)
    assert f"source: conversation:{conv['id']}" in before
    assert 'workspace: booking' in before
    assert memory.write_episode(conv) == second
    assert len(list(memory.EPISODES_DIR.glob('*.md'))) == 2


def test_workspace_recall_excludes_other_project_and_labels_unknown_scope(mem):
    memory.remember('Deploy gateway alpha', 'Gateway needs alpha namespace', workspace='alpha', source='task:15')
    memory.remember('Deploy gateway beta', 'Gateway needs beta namespace', workspace='beta', source='task:16')
    memory.remember('Gateway common', 'Gateway checks need evidence')
    hits = memory.recall('gateway', workspace='alpha')
    assert {h['title'] for h in hits} == {'Deploy gateway alpha', 'Gateway common'}
    block = memory.recall_block('gateway', workspace='alpha')
    assert 'source: task:15' in block and 'workspace: not recorded' in block
    assert 'beta' not in block


def test_snapshot_versions_are_immutable_and_corruption_is_detected(mem):
    path = memory.remember('Prior evidence', 'Original')
    memory.consolidate()
    original = memory.read_memory_file(path).encode()
    digest = hashlib.sha256(original).hexdigest()
    (mem/path).write_text(memory.read_memory_file(path).replace('Original', 'Revised'))
    memory.consolidate()
    assert (mem/'.history'/f'{digest}.md').read_bytes() == original
    assert len(list((mem/'.history').glob('*.json'))) == 2
    current = (mem/path).read_bytes()
    (mem/'.history'/f'{hashlib.sha256(current).hexdigest()}.md').write_text('corrupt')
    with pytest.raises(RuntimeError, match='Corrupt memory history'):
        memory.consolidate()


def test_metadata_cannot_add_a_fake_workspace(mem):
    path = memory.remember('Title\nworkspace: beta', 'Fact', workspace='alpha')
    assert memory._parse_frontmatter(memory.read_memory_file(path))['workspace'] == 'alpha'


def test_http_memory_tools_preserve_workspace_and_source(mem, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setenv('ASTA_TOKEN', 'fixture-token')
    client = TestClient(main.app)
    headers = {'Authorization':'Bearer fixture-token'}
    saved = client.post('/api/memory', headers=headers, json={
        'title':'Quasar protocol', 'fact':'Quasar requires token refresh',
        'workspace':'alpha', 'source':'task:18'})
    assert saved.status_code == 200
    entry, = list(memory.FACTS_DIR.glob('*.md'))
    assert memory._parse_frontmatter(entry.read_text())['source'] == 'task:18'
    found = client.get('/api/memory/search', headers=headers, params={'q':'quasar', 'workspace':'alpha'})
    excluded = client.get('/api/memory/search', headers=headers, params={'q':'quasar', 'workspace':'beta'})
    assert 'Quasar protocol' in found.json()['result']
    assert excluded.json()['result'] == 'No matching memories.'
