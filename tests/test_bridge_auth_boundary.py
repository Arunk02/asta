"""Exercise the real bridge HTTP handler without a socket or WhatsApp login."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_bridge_status_and_writes_fail_closed_without_pairing_or_network():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is not installed')
    source = (Path(__file__).resolve().parents[1] / 'whatsapp/bridge.js').read_text()
    handler = source[source.index('http\n  .createServer'):source.rindex('\nconnect();')]
    script = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = JSON.parse(process.argv[1]);
async function request(token, auth, method, url) {
  let handle;
  const context = {TOKEN: token, PORT: 0, paired: true, config: {enabled: true},
    targetJid: () => 'fixture-account', lastQrPng: 'fixture-qr',
    http: {createServer(fn) {handle = fn; return {listen() {}};}}};
  vm.runInNewContext(source, context);
  let status, body;
  await handle({method, url, headers: {authorization: auth},
    on() { throw new Error('Unauthorized write reached its request body'); }},
    {writeHead(code) {status = code;}, end(value) {body = value;}});
  return {status, body};
}
(async () => {
  for (const [method, url] of [['GET','/status'], ['POST','/send'], ['POST','/config'],
     ['POST','/create-group'], ['POST','/send-document'], ['POST','/done']]) {
    assert.equal((await request('', '', method, url)).status, 401);
    assert.equal((await request('secret', 'Bearer wrong', method, url)).status, 401);
  }
  const ok = await request('secret', 'Bearer secret', 'GET', '/status');
  assert.equal(ok.status, 200);
  assert.equal(JSON.parse(ok.body).jid, 'fixture-account');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run([node, '-e', script, json.dumps(handler)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_status_clients_authenticate_and_supervisor_does_not_spawn_on_401(monkeypatch):
    import httpx
    from app import notify, wa_bridge
    original = httpx.AsyncClient
    responses = [200, 401, 401]
    def handle(request):
        assert request.headers['authorization'] == 'Bearer fixture-token'
        return httpx.Response(responses.pop(0), json={'up': True, 'paired': True})
    transport = httpx.MockTransport(handle)
    monkeypatch.setenv('ASTA_TOKEN', 'fixture-token')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))
    assert (await notify.wa_status())['paired']
    assert await wa_bridge._bridge_answering()
    assert (await notify.wa_status()) == {'up': False, 'paired': False}
