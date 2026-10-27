import pytest

from app import main, ops, prname, store, tasks, walkthrough

A = 'https://github.com/team-one/atlas-service/pull/42'
B = 'https://github.com/team-two/atlas-service/pull/42'
C = 'https://github.com/team-one/other-service/pull/42'


def task(*urls, status='shipped'):
    t = store.create_task('Fix the service', 'code', 'Fix it', None)
    store.update_task(t['id'], status=status, pr_urls='\n'.join(urls))
    return t['id']


@pytest.mark.parametrize('ref', [A, A + '/files', A + '#discussion_r123', 'team-one/atlas-service#42'])
def test_full_identity_never_routes_to_same_number_in_another_owner_or_repo(ref):
    expected = task(A)
    task(B)
    task(C)
    assert tasks.task_for_pr(ref) == expected
    assert tasks.owner_of_pr('Fix ' + ref)['id'] == expected


@pytest.mark.parametrize('ref', ['PR 42', 'atlas-service PR 42', 'PR 42 — atlas-service'])
def test_ambiguous_number_or_repository_does_not_choose_latest_task(ref):
    task(A)
    task(B)
    assert tasks.task_for_pr(ref) is None
    assert tasks.owner_of_pr(ref) is None
    assert walkthrough.wants_to_start('walk me through PR 42') == ''


def test_explicit_unknown_repository_never_falls_back_to_number():
    task(A)
    assert tasks.task_for_pr(B) is None
    assert tasks.task_for_pr('missing-service PR 42') is None
    assert tasks.task_for_pr('https://example.com/github.com/team-one/atlas-service/pull/42') is None


def test_single_number_remains_supported_and_duplicate_owners_are_ambiguous():
    expected = task(A)
    assert tasks.task_for_pr('PR 42') == expected
    assert walkthrough.wants_to_start('walk me through PR 42') == f'task {expected}'
    assert walkthrough.wants_to_start('walk me through PR #42') == f'task {expected}'
    task(A)
    assert tasks.task_for_pr(A) is None


def test_multiple_explicit_targets_are_not_collapsed():
    task(A, C)
    assert tasks.task_for_pr(A + ' and ' + C) is None


def test_closed_task_is_readable_but_cannot_take_new_fix_work():
    tid = task(A, status='merged')
    assert tasks.task_for_pr(A) == tid
    assert tasks.owner_of_pr(A) is None


def test_spoken_service_name_resolves_a_number_shared_across_repos():
    tid = task('https://github.com/team-one/telikos-booking-service/pull/42')
    task(C)
    assert walkthrough.wants_to_start('walk me through booking PR number 42') == f'task {tid}'


def test_recorded_draft_binds_the_exact_pr_within_multi_repo_task():
    tid = task(A, C)
    op = main._mechanical_send({'channel': 'pr', 'to': C, 'what': 'Follow-up tracked in ' + A})
    assert op['args']['task_id'] == tid and op['args']['pr_url'] == C
    assert main._mechanical_send({'channel': 'pr', 'to': B, 'what': 'See ' + A}) is None


@pytest.mark.asyncio
async def test_pr_note_updates_the_selected_repo_and_verifies_full_text(monkeypatch):
    tid = task(A, C)
    calls = []
    async def git(cwd, *args, **kwargs):
        calls.append(args)
        return 0, 'Original\nFull note\nSecond line'
    monkeypatch.setattr(tasks.repo_ops, 'git', git)
    result = await ops._pr_note(tid, 'Full note\nSecond line', pr_url=C, where='comment')
    assert C in result and calls[0][3] == C
    calls.clear()
    with pytest.raises(ValueError, match='multiple PRs'):
        await tasks.pr_note(tid, 'Full note')
    with pytest.raises(ValueError, match='not recorded'):
        await tasks.pr_note(tid, 'Full note', pr_url=B)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('readback', [(1, 'unavailable'), (0, 'Full note only, missing second line')])
async def test_failed_or_partial_readback_never_claims_updated(monkeypatch, readback):
    tid = task(A)
    responses = iter([(0, 'Original'), (0, ''), readback])
    async def git(*args, **kwargs):
        return next(responses)
    monkeypatch.setattr(tasks.repo_ops, 'git', git)
    with pytest.raises(RuntimeError, match='could not be verified'):
        await tasks.pr_note(tid, 'Full note\nSecond line', pr_url=A)


@pytest.mark.asyncio
async def test_http_and_agent_preserve_the_selected_pr(monkeypatch):
    import httpx
    from app import agent
    monkeypatch.setenv('ASTA_TOKEN', 'test-secret')
    calls = []
    async def note(task_id, text, where, *, pr_url=''):
        calls.append((task_id, text, where, pr_url))
        return 'verified'
    monkeypatch.setattr(tasks, 'pr_note', note)
    assert await agent.update_task_pr(9, 'comment', 'Note', pr_url=C) == 'verified'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
        response = await client.post('/api/tasks/9/pr-note',
                                     json={'text': 'Note', 'where': 'comment', 'pr_url': C},
                                     headers={'Authorization': 'Bearer test-secret'})
        assert response.status_code == 200
    assert calls == [(9, 'Note', 'comment', C)] * 2
