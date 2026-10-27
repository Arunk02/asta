"""Two reporters, one service failure: sanitized reproduction of 26 October."""
import time

import pytest

from app import answers, issue_links, responder, store, tasks

DIVY = ("Arunkumar / Vinish atlas-activityplan-service is failing in qa env "
        "post latest build deployment. "
        "https://github.com/example/atlas-activityplan-service/actions/runs/40275933855\n"
        "Error :\nApplication run failed: Unable to connect to Temporal service\n"
        "Caused by: DEADLINE_EXCEEDED after 2.899843160s buffered_nanos=2900431894")
SANKALP = ("Hi Arunkumar / Vinish Can you please check this.. pods are not coming up in qa\n"
           "atlas-activityplan-service-68d55579f-88zzk 1/2 Running 6 (4m25s ago) 23m\n"
           "atlas-activityplan-service-68d55579f-r6bdh 1/2 Running 6 (3m49s ago) 23m")


@pytest.fixture
def spawn(monkeypatch):
    monkeypatch.setenv("ASTA_RESPOND", "1")
    monkeypatch.setattr(responder, "familiar", lambda _: (True, "fixture"))
    out = []
    def create(title, prompt, kind, workspace=None, **kwargs):
        task = store.create_task(title, kind, prompt, workspace)
        out.append(task)
        return task
    monkeypatch.setattr(tasks, "spawn", create)
    return out


def ask(who, text, **kwargs):
    return responder.respond("teams-chat", who, text, priority=1,
                             reply_to=who, thread=f"teams:{who}", kind_override="debug", **kwargs)


def test_distinct_pod_names_and_stack_trace_identify_same_service_and_environment():
    a, b = issue_links.scope(DIVY, "debug"), issue_links.scope(SANKALP, "debug")
    assert a and b
    assert (a['service'], a['env'], a['problem']) == (b['service'], b['env'], b['problem'])


def test_live_reports_share_work_but_keep_both_recipients(spawn):
    first = ask("Reporter One", DIVY, context="old ticket ABC-12345 in prod")
    second = ask("Reporter Two", SANKALP, context="unrelated PR https://github.com/example/other/pull/44")
    assert len(spawn) == 1
    assert second['joined'] and second['id'] == first['id']
    assert answers._meta(first['id'])['who'] == 'Reporter One'
    waiter, = answers._review_waiters(first['id'])
    assert waiter['who'] == 'Reporter Two' and waiter['thread'] == 'teams:Reporter Two'
    assert waiter['source_text'] == SANKALP


def test_finished_report_becomes_focused_recheck_not_blind_cached_reply(spawn):
    first = ask('Reporter One', DIVY)
    store.update_task(first['id'], status='done', finished_at=time.time() - 16 * 60,
                      result='ANALYSIS: Temporal endpoint was unreachable.\nREPLY: Checking connectivity.')
    second = ask('Reporter Two', SANKALP)
    assert len(spawn) == 2 and not second.get('reused')
    assert f"Related investigation #{first['id']}" in spawn[-1]['prompt']
    assert 'Temporal endpoint was unreachable' in spawn[-1]['prompt']
    assert 'verify current pod/build/log evidence' in spawn[-1]['prompt']
    assert store.kv_get(f"issue_predecessor:{second['id']}") == str(first['id'])


@pytest.mark.parametrize('text', [
    SANKALP.replace('in qa', 'in prod'),
    SANKALP.replace('activityplan', 'booking'),
    'Can you check invoice dispatch for booking 88271234 in qa?',
    'atlas-activityplan-service is slow in qa, why?',
    SANKALP + '\nFor booking 88271234',
    SANKALP + '\nIncident INC1234567',
    SANKALP.replace('in qa', ''),
    SANKALP.replace('in qa', 'in qa and prod'),
    SANKALP + '\nWhy only this pod, compare with the other one?',
])
def test_different_or_ambiguous_work_does_not_join(spawn, text):
    first = ask('Reporter One', DIVY)
    second = ask('Reporter Two', text)
    assert second['id'] != first['id'] and len(spawn) == 2


def test_different_explicit_build_is_a_different_investigation(spawn):
    first = ask('Reporter One', DIVY)
    second = ask('Reporter Two', DIVY.replace('40275933855', '40275933999'))
    assert second['id'] != first['id'] and not second.get('joined')


def test_quoted_old_release_environment_does_not_replace_current_report():
    quoted = ('Arunkumar K\n26/10/2026 11:26\nPromote booking to pp\n\n' + SANKALP)
    assert issue_links.scope(quoted, 'debug')['env'] == 'qa'


@pytest.mark.parametrize('status,age', [('failed', 100), ('cancelled', 100),
                                        ('superseded', 100), ('running', 7200), ('done', 25000)])
def test_failed_or_stale_work_cannot_absorb_new_report(spawn, status, age):
    first = ask('Reporter One', DIVY)
    store.update_task(first['id'], status=status, created_at=time.time() - age,
                      finished_at=time.time() - age, result='old finding')
    second = ask('Reporter Two', SANKALP)
    assert second['id'] != first['id']
    assert not store.kv_get(f"issue_predecessor:{second['id']}")


def test_corrupt_scope_record_does_not_block_new_work(spawn):
    first = ask('Reporter One', DIVY)
    store.kv_set(f"issue_scope:{first['id']}", '[]')
    assert ask('Reporter Two', SANKALP)['id'] != first['id']


def test_identical_report_from_second_sender_still_registers_recipient(spawn):
    first = ask('Reporter One', DIVY)
    second = ask('Reporter Two', DIVY)
    assert len(spawn) == 1 and second['joined']
    assert answers._review_waiters(first['id'])[0]['who'] == 'Reporter Two'


def test_changed_build_beyond_feed_preview_is_not_silently_dropped(spawn):
    padding = 'Deployment details and supporting logs. ' * 20
    first = ask('Reporter One', padding + DIVY)
    second = ask('Reporter Two', (padding + DIVY).replace('40275933855', '40275933999'))
    assert second and second['id'] != first['id']
