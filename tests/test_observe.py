"""Production's own eyes — Round 3, P10.

Grafana was never flaky. In three weeks the brains made 194 MCP calls and 89
failed, every one of them a query the model wrote: 56 answers too large to read,
13 rejected for too few label matchers, 4 over the scan limit. Temporal was
worse — it worked perfectly and no brain doing the work had it.

So the query is built in code and the answer is summarised in code, and these
tests hold the parts that made the difference: the two-label rule, the escaping,
the grouping, the fallback, and refusals that say what to do next.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app import grafana, temporal


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("ASTA_GRAFANA", "1")
    monkeypatch.setenv("ASTA_GRAFANA_URL", "https://grafana.example")
    monkeypatch.setenv("ASTA_GRAFANA_DATASOURCE_UID", "loki")
    monkeypatch.setenv("ASTA_GRAFANA_NAMESPACE", "team-prod")
    monkeypatch.setenv("ASTA_GRAFANA_CLUSTERS", "west-1,west-2")
    monkeypatch.setenv("ASTA_GRAFANA_EXCLUDED_APPS", "self-monitor")
    monkeypatch.setenv("ASTA_GRAFANA_TOKEN", "t0ken")
    monkeypatch.setenv("ASTA_GRAFANA_MCP_URL", "")
    grafana._token.update(value="", at=0.0)


def _serve(handler):
    """An httpx client whose every request is answered by `handler`."""
    class Fake(httpx.AsyncClient):
        def __init__(self, *a, **k):
            super().__init__(transport=httpx.MockTransport(handler), timeout=5)
    return Fake


# --- the query Loki will actually accept ---------------------------------------------

def test_every_query_carries_namespace_and_cluster():
    """Loki enforces a two-label minimum; a one-label query is the 13 rejections."""
    query = grafana.build_query("team-prod")
    assert 'namespace="team-prod"' in query
    assert 'k8s_cluster=~"west-1|west-2"' in query


def test_a_service_narrows_and_the_search_terms_become_filters():
    query = grafana.build_query("team-prod", service="billing",
                                terms=["MH4DRHV7BDPY", "trace-9"])
    assert 'app=~".*billing.*"' in query
    assert '|= "MH4DRHV7BDPY"' in query and '|= "trace-9"' in query


def test_a_hyphen_is_never_escaped():
    """`re.escape` emits `\\-`, which RE2 rejects outside a character class — the
    bug that made every hyphenated service name fail."""
    assert grafana.escape_re2("booking-consumer") == "booking-consumer"
    assert grafana.escape_re2("a.b+c") == r"a\.b\+c"


def test_tracing_an_identifier_does_not_force_the_error_filter():
    tracing = grafana.build_query("team-prod", terms=["BK-1"], errors_only=False)
    assert "|~" not in tracing and 'level!~' not in tracing
    hunting = grafana.build_query("team-prod", terms=["BK-1"])
    assert "|~" in hunting and "level!~" in hunting


def test_timestamps_are_nanoseconds():
    import datetime as dt
    at = dt.datetime(2026, 9, 23, 12, 0, tzinfo=dt.timezone.utc)
    params = grafana.query_params("{}", at, at, 10)
    assert params["start"] == str(int(at.timestamp() * 1e9))
    assert params["direction"] == "backward"


# --- turning a wall of logs into an answer -------------------------------------------

_STACK = ("java.lang.IllegalStateException: no rate for lane\n"
          "\tat com.example.billing.RateService.price(RateService.java:88)")


def _payload(lines: list[tuple[str, str]], app: str = "billing") -> dict:
    return {"data": {"result": [{
        "stream": {"namespace": "team-prod", "app": app, "level": lvl},
        "values": [[str(int(1790000000 + i) * 10**9), line]],
    } for i, (lvl, line) in enumerate(lines)]}}


def test_records_are_grouped_into_signatures_not_handed_over_raw():
    payload = _payload([("error", _STACK)] * 3
                       + [("error", "connection reset by peer id=42")]
                       + [("info", "started fine")])
    found = grafana.summarise(grafana.parse(payload))
    assert found["scanned"] == 5
    assert found["dominant"]["count"] == 3
    assert "IllegalStateException" in found["dominant"]["signature"]
    assert "price():88" in found["dominant"]["signature"]     # where it happened
    assert len(found["signatures"]) == 2                      # info is not an error


def test_two_of_the_same_error_are_one_signature_whatever_the_ids():
    found = grafana.summarise(grafana.parse(_payload([
        ("error", "payment 8831ab04c9 rejected after 3 tries"),
        ("error", "payment 77f0c1bb21 rejected after 9 tries"),
    ])))
    assert len(found["signatures"]) == 1 and found["dominant"]["count"] == 2


def test_structured_logs_group_by_what_was_said_not_by_the_blob():
    """A JSON line carries a timestamp, a thread, a trace id and a span id before
    it reaches the message. Normalising the whole blob made 500 real lines into
    362 "distinct" errors — a log dump wearing a summary's clothes."""
    def row(trace: str, uri: str) -> str:
        return json.dumps({"timestamp": "2026-09-23 08:08:42.286", "level": "ERROR",
                           "thread": "http-nio-8080-exec-1",
                           "mdc": {"trace_id": trace, "span_id": trace[:8]},
                           "logger": "com.example.security.SecurityUtils",
                           "message": f"Error Details : Response(uri={uri}, status=401)"})
    found = grafana.summarise(grafana.parse(_payload([
        ("error", row("aaaa1111bbbb2222", "/orders")),
        ("error", row("cccc3333dddd4444", "/invoices")),
        ("error", row("eeee5555ffff6666", "/bookings")),
    ])))
    assert len(found["signatures"]) == 1 and found["dominant"]["count"] == 3
    assert "SecurityUtils" in found["dominant"]["signature"]


def test_the_payload_that_differs_between_two_of_the_same_error_is_not_the_error():
    found = grafana.summarise(grafana.parse(_payload([
        ("error", "ERROR BillingPersistence - Shard key not found for [job-7781, tenant-a]"),
        ("error", "ERROR BillingPersistence - Shard key not found for [job-9912, tenant-b]"),
    ])))
    assert len(found["signatures"]) == 1 and found["dominant"]["count"] == 2


def test_two_genuinely_different_errors_stay_apart():
    found = grafana.summarise(grafana.parse(_payload([
        ("error", "ERROR BillingPersistence - Shard key not found for [job-1]"),
        ("error", "ERROR RateService - no rate for lane [lane-2]"),
    ])))
    assert len(found["signatures"]) == 2


def test_trace_ids_come_back_because_that_is_what_gets_traced():
    line = '{"level":"ERROR","trace_id":"3255cd7b40b4b5d03c48c9eb9a4cb916","msg":"boom"}'
    found = grafana.summarise(grafana.parse(_payload([("error", line)])))
    assert found["dominant"]["trace_ids"] == ["3255cd7b40b4b5d03c48c9eb9a4cb916"]


def test_what_a_brain_reads_is_the_summary_not_the_log_dump():
    found = grafana.summarise(grafana.parse(_payload([("error", _STACK)] * 12)))
    found.update(namespace="team-prod", service="billing", minutes=30, via="api")
    text = grafana.render(found)
    assert "×12" in text and "IllegalStateException" in text
    assert len(text) < 1200                              # an answer, not a log file


def test_a_quiet_window_says_so():
    found = grafana.summarise(grafana.parse({"data": {"result": []}}))
    found.update(namespace="team-prod", minutes=30, via="api")
    assert "No errors in that window" in grafana.render(found)


# --- talking to Grafana ----------------------------------------------------------------

def test_an_expired_token_is_minted_again_and_the_query_retried(monkeypatch):
    """Tokens last about half an hour, so a 401 means "mint a new one", not
    "access denied" — a retry he never has to know about."""
    monkeypatch.setenv("ASTA_GRAFANA_TOKEN", "")
    monkeypatch.setenv("ARM_TENANT_ID", "t")
    monkeypatch.setenv("ARM_CLIENT_ID", "c")
    monkeypatch.setenv("ARM_CLIENT_SECRET", "s")
    monkeypatch.setenv("ASTA_GRAFANA_TOKEN_URL", "https://keys.example/grafana")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "login.microsoftonline.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "azure"})
        if "keys.example" in str(request.url):
            return httpx.Response(200, json={"key": f"minted-{len(seen)}"})
        seen.append(request.headers.get("Authorization", ""))
        if len(seen) == 1:
            return httpx.Response(401, text="api-key.expired")
        return httpx.Response(200, json=_payload([("error", _STACK)]))

    monkeypatch.setattr(grafana.httpx, "AsyncClient", _serve(handler))
    found = asyncio.run(grafana.logs(service="billing"))
    assert found["scanned"] == 1 and found["via"] == "api"
    assert seen[0] != seen[1]                          # it did not retry the dead one


def test_when_the_api_fails_the_same_query_goes_through_the_mcp_fallback(monkeypatch):
    monkeypatch.setenv("ASTA_GRAFANA_MCP_URL", "https://mcp.example/mcp")
    monkeypatch.setenv("ASTA_GRAFANA_MCP_SCOPE", "api://observe/.default")
    monkeypatch.setenv("ARM_TENANT_ID", "t")
    monkeypatch.setenv("ARM_CLIENT_ID", "c")
    monkeypatch.setenv("ARM_CLIENT_SECRET", "s")
    asked: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "login.microsoftonline.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "azure", "expires_in": 3000})
        if "mcp.example" in str(request.url):
            body = json.loads(request.content)
            if body["method"] == "initialize":
                return httpx.Response(200, json={"result": {}},
                                      headers={"Mcp-Session-Id": "s1"})
            asked.update(body["params"]["arguments"])
            payload = {"data": [{"timestamp": "1790000000000000000",
                                 "labels": {"app": "billing", "level": "error"},
                                 "line": _STACK}]}
            return httpx.Response(200, json={"result": {"content": [
                {"type": "text", "text": json.dumps(payload)}]}})
        return httpx.Response(500, text="datasource unavailable")

    monkeypatch.setattr(grafana.httpx, "AsyncClient", _serve(handler))
    found = asyncio.run(grafana.logs(service="billing"))
    assert found["via"] == "mcp fallback" and found["scanned"] == 1
    assert asked["logql"] == found["query"]            # the same query, not a new one
    assert "datasource unavailable" in grafana.render(found)


def test_when_both_paths_fail_the_first_failure_is_the_one_reported(monkeypatch):
    monkeypatch.setenv("ASTA_GRAFANA_MCP_URL", "https://mcp.example/mcp")
    monkeypatch.setenv("ASTA_GRAFANA_MCP_SCOPE", "api://observe/.default")
    monkeypatch.setenv("ARM_TENANT_ID", "t")
    monkeypatch.setenv("ARM_CLIENT_ID", "c")
    monkeypatch.setenv("ARM_CLIENT_SECRET", "s")

    def handler(request: httpx.Request) -> httpx.Response:
        if "login.microsoftonline.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "azure"})
        return httpx.Response(503, text="everything is down")

    monkeypatch.setattr(grafana.httpx, "AsyncClient", _serve(handler))
    with pytest.raises(grafana.GrafanaError) as caught:
        asyncio.run(grafana.logs())
    assert "503" in str(caught.value) and "everything is down" in str(caught.value)


def test_with_no_namespace_it_asks_rather_than_guessing(monkeypatch):
    monkeypatch.setenv("ASTA_GRAFANA_NAMESPACE", "")
    with pytest.raises(grafana.GrafanaError) as caught:
        asyncio.run(grafana.logs())
    assert "namespace" in str(caught.value)


# --- Temporal ---------------------------------------------------------------------------

@pytest.fixture
def temporal_envs(tmp_path, monkeypatch):
    (tmp_path / "prod.pem").write_text("cert")
    (tmp_path / "prod.key").write_text("key")
    envs = {"prod": {"address": "prod.example:7233", "namespace": "team-prod",
                     "cert": "prod", "vault": "vault kv get -mount=team-kv prod/temporal"},
            "qa": {"address": "np.example:7233", "namespace": "team-qa", "cert": "qa",
                   "vault": "vault kv get -mount=team-kv qa/temporal"}}
    monkeypatch.setenv("ASTA_TEMPORAL_ENVS", json.dumps(envs))
    monkeypatch.setattr(temporal, "CONFIG_DIR", Path(tmp_path))
    monkeypatch.setenv("ASTA_TEMPORAL_CLI", "/usr/bin/true")
    return tmp_path


def _cli(monkeypatch, answer: str, code: int = 0):
    calls: list[list[str]] = []

    async def run(args):
        calls.append(args)
        if code:
            raise temporal.TemporalError(answer)
        return answer

    monkeypatch.setattr(temporal, "_run", run)
    return calls


def test_an_environment_chooses_the_cluster_namespace_and_certificate(temporal_envs,
                                                                      monkeypatch):
    calls = _cli(monkeypatch, json.dumps([]))
    asyncio.run(temporal.workflows("prod", 'ExecutionStatus="Failed"'))
    args = calls[0]
    assert "--address" in args and "prod.example:7233" in args
    assert "--namespace" in args and "team-prod" in args
    assert "--tls" in args and str(temporal_envs / "prod.pem") in args
    # The query is an argument, never something a shell parses.
    assert 'ExecutionStatus="Failed"' in args


def test_an_env_with_no_certificate_says_where_to_get_it(temporal_envs, monkeypatch):
    _cli(monkeypatch, "")
    with pytest.raises(temporal.TemporalError) as caught:
        asyncio.run(temporal.workflows("qa"))
    assert "no certificate for qa" in str(caught.value)
    assert "vault kv get" in str(caught.value)


def test_an_unknown_env_lists_the_ones_there_are(temporal_envs, monkeypatch):
    _cli(monkeypatch, "")
    with pytest.raises(temporal.TemporalError) as caught:
        asyncio.run(temporal.workflows("staging"))
    assert "prod" in str(caught.value) and "qa" in str(caught.value)


def test_the_clis_own_words_are_what_he_hears(temporal_envs, monkeypatch):
    _cli(monkeypatch, "connection refused: check your certificate", code=1)
    with pytest.raises(temporal.TemporalError) as caught:
        asyncio.run(temporal.count("prod"))
    assert "connection refused" in str(caught.value)


def test_a_count_is_a_number(temporal_envs, monkeypatch):
    _cli(monkeypatch, "Total: 836")
    assert asyncio.run(temporal.count("prod")) == 836


@pytest.mark.parametrize("answer, rows", [
    ('[{"a": 1}, {"a": 2}]', 2),                          # a list
    ('{"executions": [{"a": 1}]}', 1),                    # an object holding one
    ('{"a": 1}\n{"a": 2}\n{"a": 3}', 3),                  # JSON lines
    ("", 0),
])
def test_however_the_cli_answers_it_becomes_rows(answer, rows):
    assert len(temporal._rows(answer)) == rows


def test_what_he_reads_is_the_workflow_not_the_json(temporal_envs, monkeypatch):
    _cli(monkeypatch, json.dumps([
        {"execution": {"workflowId": "REPRICING-2737185"},
         "type": {"name": "RepricingWorkflow"},
         "status": "WORKFLOW_EXECUTION_STATUS_FAILED",
         "startTime": "2026-09-23T07:46:34Z"}]))
    text = temporal.render(asyncio.run(temporal.workflows("prod")), "prod")
    assert "RepricingWorkflow" in text and "REPRICING-2737185" in text
    assert "FAILED" in text


def test_a_stuck_workflow_says_what_it_is_waiting_on(temporal_envs, monkeypatch):
    _cli(monkeypatch, json.dumps({
        "workflowExecutionInfo": {"execution": {"workflowId": "BK-1"},
                                  "type": {"name": "BookingWorkflow"},
                                  "status": "WORKFLOW_EXECUTION_STATUS_RUNNING",
                                  "startTime": "2026-09-23T07:00:00Z"},
        "pendingActivities": [{"activityType": {"name": "ChargeCustomer"}, "attempt": 7,
                               "lastFailure": {"message": "payment gateway timeout"}}]}))
    text = temporal.render_one(asyncio.run(temporal.describe("prod", "BK-1")), "prod")
    assert "waiting on ChargeCustomer" in text and "attempt 7" in text
    assert "payment gateway timeout" in text


def test_describing_nothing_is_refused(temporal_envs, monkeypatch):
    _cli(monkeypatch, "")
    with pytest.raises(temporal.TemporalError):
        asyncio.run(temporal.describe("prod", ""))
