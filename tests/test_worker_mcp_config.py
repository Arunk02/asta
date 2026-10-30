"""Scoped workers only load the MCP servers the server selected for that leg."""
import asyncio
import json

import pytest
from app import claude_cli, task_fence

_REAL_ONE_SHOT = claude_cli.one_shot


@pytest.mark.parametrize('mode', ['', 'none', 'code'])
@pytest.mark.parametrize('configured', [False, True])
def test_worker_mcp_is_explicit_and_strict(monkeypatch, mode, configured):
    seen = []
    config = json.dumps({'mcpServers': {'asta': {'command': 'server-owned'}}}) if configured else ''
    class Process:
        returncode = 0
        async def wait(self): return 0
    async def spawn(*args, **kwargs):
        seen.extend(args)
        process = Process()
        process.stdout = asyncio.StreamReader()
        process.stdout.feed_data(b'done')
        process.stdout.feed_eof()
        return process
    monkeypatch.setattr(claude_cli, 'available', lambda: True)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    token = task_fence.MODE.set(mode)
    try:
        assert asyncio.run(_REAL_ONE_SHOT('prompt', mcp_config=config)) == 'done'
    finally:
        task_fence.MODE.reset(token)
    assert ('--strict-mcp-config' in seen) is bool(mode)
    if configured or mode:
        actual = json.loads(seen[seen.index('--mcp-config') + 1])
        assert actual == (json.loads(config) if config else {'mcpServers': {}})
    else:
        assert '--mcp-config' not in seen
