"""One task instruction survives the CI wait; merges bind to current evidence."""
import asyncio
import json

import pytest

from app import go, review, store, tasks

URL = 'https://github.com/example/service/pull/12'
HEAD = 'a' * 40
CLEAN = {'number':12, 'state':'OPEN', 'draft':False, 'mergeable':'MERGEABLE',
         'merge_state':'CLEAN', 'review':'APPROVED', 'checks':2, 'failing':[],
         'pending':[], 'head_oid':HEAD, 'base':'main', 'url':URL}
REMOTE = {'state':'OPEN', 'reviewDecision':'APPROVED',
          'statusCheckRollup':[{'name':'tests', 'conclusion':'SUCCESS'}]}


def task():
    t = store.create_task('Fix service', 'code', 'Fix', None)
    store.update_task(t['id'], status='shipped', pr_urls=URL)
    return t['id']


@pytest.mark.parametrize('words', ['raise the PR and merge after checks pass',
                                    'implement, create PR, review and merge it'])
def test_one_instruction_persists_merge_after_later_plan_approval(words):
    tid = task()
    go.on_spawn(tid, words)
    go.grant(tid, 'approve plan', ship=False)
    assert go.merges(tid) and go.ships(tid)
    assert go.granted(tid)['merge_words'] == words
    go.grant(tid, 'do not merge', ship=False)
    assert not go.merges(tid)


@pytest.mark.parametrize('words', ['raise the PR', 'fix merge conflicts', 'do not merge',
                                    'explain how to merge this', 'should we merge it?'])
def test_no_merge_permission_inferred(words):
    tid = task()
    go.grant(tid, words)
    assert not go.merges(tid)


@pytest.fixture
def remote(monkeypatch):
    async def state(url):
        return dict(REMOTE)
    async def merge_state(*args, **kwargs):
        return dict(CLEAN)
    monkeypatch.setattr(tasks, '_pr_state', state)
    monkeypatch.setattr(review, 'merge_state', merge_state)
    return monkeypatch


@pytest.mark.asyncio
async def test_saved_task_authority_merges_once_after_green_even_if_state_unchanged(remote):
    tid = task()
    await tasks.check_pr(tid)  # green before the explicit instruction
    go.grant(tid, 'raise PR and merge after checks pass')
    calls = []
    async def merge(*args, **kwargs):
        assert kwargs['expected_head'] == HEAD and kwargs['authorized']()
        calls.append(args)
        await asyncio.sleep(0)
        return 'merged #12 into main'
    remote.setattr(review, 'merge', merge)
    results = await asyncio.gather(tasks.check_pr(tid), tasks.check_pr(tid))
    assert len(calls) == 1 and any('saved merge instruction' in (r or '') for r in results)
    await tasks.check_pr(tid)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'pending':['test']}, {'failing':['test']},
                                     {'mergeable':'UNKNOWN'}, {'review':'REVIEW_REQUIRED'},
                                     {'merge_state':'BLOCKED'}, {'checks':0}])
async def test_incomplete_evidence_never_merges(remote, change):
    tid = task()
    go.grant(tid, 'merge after checks pass')
    async def state(*a, **k): return {**CLEAN, **change}
    async def merge(*a, **k): pytest.fail('must not merge')
    remote.setattr(review, 'merge_state', state)
    remote.setattr(review, 'merge', merge)
    await tasks.check_pr(tid)


@pytest.mark.asyncio
async def test_uncertain_write_is_not_retried_after_another_poll(remote):
    tid = task()
    go.grant(tid, 'merge after checks pass')
    calls = []
    async def merge(*args, **kwargs):
        calls.append(1)
        raise RuntimeError('connection lost after request')
    remote.setattr(review, 'merge', merge)
    assert 'not confirmed' in await tasks.check_pr(tid)
    await tasks.check_pr(tid)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_revocation_during_preflight_is_honored(remote):
    tid = task()
    go.grant(tid, 'merge after checks pass')
    async def state(*args, **kwargs):
        go.grant(tid, 'do not merge', ship=False)
        return dict(CLEAN)
    async def merge(*args, **kwargs): pytest.fail('permission revoked')
    remote.setattr(review, 'merge_state', state)
    remote.setattr(review, 'merge', merge)
    await tasks.check_pr(tid)


@pytest.mark.asyncio
async def test_one_merged_pr_does_not_finish_multi_repo_task(remote):
    tid = task()
    other = URL.replace('service', 'other')
    store.update_task(tid, pr_urls=URL + '\n' + other)
    async def state(url): return {'state':'MERGED'} if url == URL else REMOTE
    remote.setattr(tasks, '_pr_state', state)
    await tasks.check_pr(tid)
    assert store.get_task(tid)['status'] != 'merged'
    async def merged(url): return {'state':'MERGED'}
    remote.setattr(tasks, '_pr_state', merged)
    await tasks.check_pr(tid)
    assert store.get_task(tid)['status'] == 'merged'


@pytest.mark.asyncio
async def test_merge_request_uses_verified_head_and_checks_receipt(monkeypatch):
    calls = []
    async def gh(cwd, *args, **kwargs):
        calls.append(args)
        if args[:2] == ('pr', 'merge'): return 0, ''
        return 0, json.dumps({'number':12, 'state':'MERGED' if len(calls) == 3 else 'OPEN',
                              'mergeable':'MERGEABLE', 'headRefOid':HEAD, 'baseRefName':'main',
                              'statusCheckRollup':[{'conclusion':'SUCCESS'}]})
    monkeypatch.setattr(review, '_gh', gh)
    assert 'merged #12' in await review.merge(URL, '', expected_head=HEAD)
    merge_call = calls[1]
    assert merge_call[merge_call.index('--match-head-commit') + 1] == HEAD


@pytest.mark.asyncio
async def test_head_change_before_write_blocks_without_request(monkeypatch):
    async def state(*a, **k): return {**CLEAN, 'head_oid':'b' * 40}
    async def gh(*a, **k): pytest.fail('must not issue a write')
    monkeypatch.setattr(review, 'merge_state', state)
    monkeypatch.setattr(review, '_gh', gh)
    with pytest.raises(review.MergeBlocked, match='revision changed'):
        await review.merge(URL, '', expected_head=HEAD)


@pytest.mark.asyncio
async def test_merge_command_on_existing_task_is_saved_and_revocable(remote):
    tid = task()
    async def merge(*a, **k): return 'merged #12 into main'
    remote.setattr(review, 'merge', merge)
    parsed = go.command(f'merge task {tid}')
    assert parsed == (True, tid)
    await go.act(f'merge task {tid}', tid)
    assert go.merges(tid)
    assert go.command(f'do not merge task {tid}') == (True, tid)
    assert 'revoked' in await go.act(f'do not merge task {tid}', tid)
    assert not go.merges(tid)
    assert go.command(f'merge PR {tid}') == (False, None)
    assert go.command(f'merge the PR {tid}') == (False, None)


@pytest.mark.asyncio
async def test_queued_merge_is_not_reported_as_completed(monkeypatch):
    async def state(*a, **k): return dict(CLEAN)
    async def gh(*a, **k): return 0, 'queued'
    monkeypatch.setattr(review, 'merge_state', state)
    monkeypatch.setattr(review, '_gh', gh)
    result = await review.merge(URL, '', expected_head=HEAD)
    assert 'not confirmed' in result and not result.startswith('merged')


@pytest.mark.asyncio
async def test_cancelled_request_retains_receipt_and_does_not_resend(remote):
    tid = task()
    go.grant(tid, 'merge after checks pass')
    calls = []
    async def merge(*a, **k):
        calls.append(1)
        raise asyncio.CancelledError()
    remote.setattr(review, 'merge', merge)
    with pytest.raises(asyncio.CancelledError):
        await tasks.check_pr(tid)
    await tasks.check_pr(tid)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_changed_preflight_can_be_rechecked_without_new_permission(remote):
    tid = task()
    go.grant(tid, 'merge after checks pass')
    calls = []
    async def merge(*a, **k):
        calls.append(1)
        if len(calls) == 1: raise review.MergeBlocked('head changed before write')
        return 'merged #12'
    remote.setattr(review, 'merge', merge)
    await tasks.check_pr(tid)
    await tasks.check_pr(tid)
    assert len(calls) == 2 and go.merges(tid)
