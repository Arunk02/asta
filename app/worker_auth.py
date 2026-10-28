"""Server-issued API credentials for one background execution leg.

This is an HTTP capability boundary, not an OS sandbox. Workers still need a
separate filesystem/process boundary to prevent reading local owner secrets.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import secrets
import time

from . import store

PREFIX = 'asta_worker_'
TTL_SECONDS = 2 * 3600
CURRENT: contextvars.ContextVar[tuple[str, dict] | None] = contextvars.ContextVar('asta_worker_scope', default=None)
# Explicit allowlist: a newly registered tool never automatically joins it.
READ_TOOLS = frozenset({
    'search_memory', 'load_skill', 'search_knowledge', 'resolve_context',
    'read_workspace_file', 'list_services', 'facts_about',
    'grafana_logs', 'temporal_workflows', 'temporal_workflow', 'log_counts',
    'log_lines', 'find_id', 'log_gaps', 'debug_stack_health',
    'jira_search', 'jira_my_issues', 'jira_issue', 'jira_sprint',
    'teams_read_chat', 'teams_history', 'teams_resolve', 'conversation_status',
    'app_recipes', 'task_result', 'task_pr_status', 'task_ci_report', 'ci_status',
})
# Writes that only STAGE for Arun's yes, labelled with the task. Review tasks are
# told to call propose_pr_review and draft tasks prepare_to_send; without these the
# worker finishes its work with no way to hand it over. Nothing here acts outward.
STAGE_TOOLS = frozenset({'prepare_to_send', 'propose_pr_review'})
_WORKSPACE_TOOLS = {'search_memory', 'resolve_context', 'read_workspace_file', 'list_services'}
_TASK_TOOLS = {'task_result', 'task_pr_status', 'task_ci_report'}
_STOPPED = {'done', 'failed', 'cancelled', 'rejected', 'merged', 'pr_closed', 'sent', 'send_uncertain'}


class Refused(ValueError):
    pass


def _key(token: str) -> str:
    return 'worker_scope:' + hashlib.sha256(token.encode()).hexdigest()


def issue(task_id: int, conv_id: str = '', now: float | None = None) -> tuple[str, dict]:
    task = store.get_task(task_id)
    if not task or task.get('status') in _STOPPED:
        raise Refused('Cannot issue a credential for a missing or finished task')
    now = time.time() if now is None else now
    token = PREFIX + secrets.token_urlsafe(32)
    scope = {'version': 1, 'task_id': task_id, 'conv_id': conv_id,
             'workspace': task.get('workspace') or '', 'tools': sorted(READ_TOOLS | STAGE_TOOLS),
             'created_at': now, 'expires_at': now + TTL_SECONDS}
    # Store only the digest of the secret. This survives an API restart without
    # making the reusable token available through the database's record value.
    store.kv_set(_key(token), json.dumps(scope))
    return token, scope


def revoke(token: str) -> None:
    store.kv_del(_key(token))


def prune(now: float | None = None) -> int:
    """Delete scope records that can no longer authenticate.

    A leg revokes its own record on exit; a process that died mid-leg leaves one
    behind. Expired, unreadable and finished-task records are removed — never a
    live one, since its credential is still in a running worker's environment.
    """
    now = time.time() if now is None else now
    gone = 0
    for key, raw in store.kv_like('worker_scope:%').items():
        try:
            scope = json.loads(raw or '{}')
            task = store.get_task(scope['task_id'])
            dead = (scope['expires_at'] <= now or not task
                    or task.get('status') in _STOPPED)
        except (KeyError, TypeError, ValueError):
            dead = True
        if dead:
            store.kv_del(key)
            gone += 1
    return gone


def authenticate(token: str, now: float | None = None) -> dict:
    if not token.startswith(PREFIX):
        raise Refused('Invalid worker credential')
    try:
        scope = json.loads(store.kv_get(_key(token)) or '{}')
        if scope.get('version') != 1 or scope['expires_at'] <= (time.time() if now is None else now):
            raise Refused('Worker credential expired or revoked')
        task = store.get_task(scope['task_id'])
        if not task or task.get('status') in _STOPPED or (task.get('workspace') or '') != scope['workspace']:
            raise Refused('Task ended or its workspace changed')
        return scope
    except (KeyError, TypeError, json.JSONDecodeError):
        raise Refused('Invalid worker scope') from None


def _registered(name) -> bool:
    """One of his own registered workspaces.

    A task with a workspace is pinned to it. A task with none — a colleague's
    question the responder investigates — may read any workspace he registered:
    28 Oct, two such investigations asked for the booking context and were
    refused, and answered without it. Never an unregistered name."""
    if not isinstance(name, str) or not name.strip():
        return False
    try:
        from . import workspace
        return name in workspace.available_workspaces()
    except Exception:                                          # noqa: BLE001
        return False


def authorize(scope: dict, body: dict) -> dict:
    """Bind identity from the server record; reject caller-supplied substitutions."""
    if str(body.get('task_id') or scope['task_id']) != str(scope['task_id']):
        raise Refused('Worker credential belongs to another task')
    if body.get('conv_id', scope['conv_id']) != scope['conv_id']:
        raise Refused('Worker credential belongs to another conversation')
    if body.get('message_id') is not None:
        raise Refused('A worker cannot impersonate a user message')
    name = body.get('tool')
    from . import capabilities
    staging = name in STAGE_TOOLS
    if not isinstance(name, str) or name not in scope['tools'] or (
            not staging and (name not in READ_TOOLS or capabilities.writes(name))):
        raise Refused('Capability is outside this worker credential')
    args = body.get('args') or {}
    if not isinstance(args, dict):
        raise Refused('args must be an object')
    args = dict(args)
    if name == 'prepare_to_send' and (not str(args.get('to') or '').strip() or args.get('to_group')
                                      or args.get('channel', 'chat') not in ('chat', 'teams')):
        # Only the task-draft path, shown labelled with the task when it ends. The
        # chat's own slot would let his next unrelated "yes" approve it unseen.
        raise Refused('A worker can stage only a 1:1 chat draft to a named person')
    if name in _WORKSPACE_TOOLS:
        asked = args.get('workspace', scope['workspace'])
        if asked != scope['workspace'] and not (not scope['workspace'] and _registered(asked)):
            raise Refused('Workspace is outside this worker credential')
        args['workspace'] = asked
    if name in _TASK_TOOLS:
        if str(args.get('task_id', scope['task_id'])) != str(scope['task_id']):
            raise Refused('Cannot access another task with this credential')
        args['task_id'] = scope['task_id']
    return {**body, 'args': args, 'task_id': scope['task_id'], 'conv_id': scope['conv_id'],
            'message_id': None, 'read_only': not staging}


@contextlib.contextmanager
def session(task_id: int):
    from . import tasks
    credential, scope = issue(task_id, tasks.conversation_of(task_id))
    binding = CURRENT.set((credential, scope))
    try:
        yield credential, scope
    finally:
        CURRENT.reset(binding)
        revoke(credential)
