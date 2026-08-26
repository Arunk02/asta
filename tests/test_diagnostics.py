"""The debugging stack, and the difference between absent and broken.

Every case here is a shape that already fooled something in this system: a file
that exists and holds nothing, a credential that is present and refused, a check
that reports a gap as a fault until people stop reading it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import textwrap

import pytest

from app import diagnostics


def _write_cert(dirpath, base: str, *, days_valid: int = 365) -> None:
    """A real, parseable certificate — so the test exercises real parsing."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{base}:write")])
    now = dt.datetime.now(dt.timezone.utc)
    after = now + dt.timedelta(days=days_valid)
    # An ALREADY-expired cert still has to have been valid at some point, so the
    # start date follows the end date rather than the clock.
    before = min(now - dt.timedelta(days=1), after - dt.timedelta(days=1))
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(before)
            .not_valid_after(after)
            .sign(key, hashes.SHA256()))
    (dirpath / f"{base}.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (dirpath / f"{base}.key").write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()))


@pytest.fixture
def fake_stack(tmp_path, monkeypatch):
    """A stand-in proxy with a known env map, so no test touches his real certs."""
    certs = tmp_path / "certs"
    certs.mkdir()
    proxy = tmp_path / "temporal-mcp-proxy.py"
    proxy.write_text(textwrap.dedent(f'''
        CONFIG_DIR = {str(certs)!r}
        CLI = "temporal"
        ENV_MAP = {{
            "good":    {{"address": "a:7233", "namespace": "n", "cert": "good"}},
            "hollow":  {{"address": "a:7233", "namespace": "n", "cert": "hollow"}},
            "stale":   {{"address": "a:7233", "namespace": "n", "cert": "stale"}},
            "absent":  {{"address": "a:7233", "namespace": "n", "cert": "absent"}},
            "garbage": {{"address": "a:7233", "namespace": "n", "cert": "garbage"}},
        }}
    '''))
    monkeypatch.setattr(diagnostics, "TEMPORAL_PROXY", proxy)
    _write_cert(certs, "good", days_valid=300)
    _write_cert(certs, "stale", days_valid=-5)          # expired last week
    (certs / "hollow.pem").write_bytes(b"")             # the preprod bug, exactly
    (certs / "hollow.key").write_bytes(b"")
    (certs / "garbage.pem").write_bytes(b"this is not a certificate")
    (certs / "garbage.key").write_bytes(b"nor is this")
    return certs


def _by_env(rows):
    return {r["env"]: r for r in rows}


def test_an_empty_cert_is_broken_not_fine(fake_stack):
    """The bug this module exists for.

    `preprod.pem` and `preprod.key` were both 0 bytes on Arun's machine. The proxy
    checks os.path.exists, which an empty file passes, so the failure surfaced as
    "tls: failed to find any PEM data in certificate input" — a sentence naming
    PEM parsing, with no hint that the file is simply empty.
    """
    rows = _by_env(diagnostics.certs())
    assert rows["hollow"]["state"] == "empty"
    assert rows["hollow"]["ok"] is False
    assert "0 bytes" in rows["hollow"]["why"], "the message must name the actual problem"


def test_a_missing_cert_is_not_called_broken(fake_stack):
    """Absent is a gap; present-and-unusable is a fault. Only faults go to health.

    Four of the seven real envs have no cert because Arun does not use them.
    Reporting those as problems on every health pass is how a health report turns
    into something people scroll past — the same way the first selector check
    reported unchecked selectors as BROKEN and had to be corrected.
    """
    broken = {c["env"] for c in diagnostics.broken_certs()}
    assert "absent" not in broken, "a never-configured env is not a fault"
    assert {"hollow", "stale", "garbage"} <= broken, "real faults must be reported"


def test_an_expired_cert_is_broken(fake_stack):
    rows = _by_env(diagnostics.certs())
    assert rows["stale"]["state"] == "expired"
    assert rows["stale"]["ok"] is False


def test_a_valid_cert_says_how_long_it_has(fake_stack):
    """A date is actionable; "ok" is not — he can plan a rotation from one."""
    rows = _by_env(diagnostics.certs())
    assert rows["good"]["ok"] is True
    assert "days" in rows["good"]["why"]


def test_unparseable_content_is_broken_not_a_crash(fake_stack):
    """A file full of the wrong thing must be a finding, not a traceback."""
    rows = _by_env(diagnostics.certs())
    assert rows["garbage"]["state"] == "unreadable"
    assert rows["garbage"]["ok"] is False


def test_the_env_map_comes_from_the_proxy(fake_stack):
    """Read, never copied — two copies is how a new env goes missing in one.

    The fixture's map has five invented envs. If diagnostics held its own copy of
    the real seven, this returns those instead and the test fails.
    """
    envs = {c["env"] for c in diagnostics.certs()}
    assert envs == {"good", "hollow", "stale", "absent", "garbage"}


def test_no_proxy_is_reported_not_guessed(tmp_path, monkeypatch):
    """With no proxy there is nothing to check — and that must not read as healthy."""
    monkeypatch.setattr(diagnostics, "TEMPORAL_PROXY", tmp_path / "nope.py")
    assert diagnostics.certs() == []
    assert "not found" in diagnostics.summary()


def test_reach_refuses_before_spending_thirty_seconds(fake_stack, monkeypatch):
    """A known-bad cert is reported instantly, not proved slowly over TLS.

    Spending the full network timeout to rediscover something already known is
    thirty seconds of silence, and it reports a timeout — which points at the VPN
    instead of at the file.
    """
    def _never(*a, **k):
        raise AssertionError("should not have opened a connection")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _never)
    out = asyncio.run(diagnostics.temporal_reach("hollow"))
    assert out["ok"] is False
    assert out["seconds"] == 0.0
    assert "0 bytes" in out["why"]


def test_health_names_the_broken_env(fake_stack):
    """Health must say WHICH env and WHY, not that something is wrong somewhere."""
    from app import health
    problems = asyncio.run(health.checks())
    key = "temporal-hollow"
    assert key in problems, f"a broken cert did not reach health: {sorted(problems)}"
    assert "0 bytes" in problems[key]
    assert "temporal-absent" not in problems, "a never-configured env must not be a problem"


# --- an eval that cannot fail measures nothing -------------------------------

def _degenerate_askers(playbook: str):
    """Answers that did no reasoning. Every one of these must score zero."""
    async def parrot(_q):        # hands back the playbook it was given
        return playbook

    async def silent(_q):
        return ""

    async def waffle(_q):
        return ("Check the logs and the metrics, correlate the timeline, and find "
                "the root cause of the issue.")

    async def shotgun(_q):       # names every value it can think of
        return ("telikos-sit telikos-uat telikos-dev telikos-spt telikos-qa "
                "telikos-preprod telikos-sit-cdt telikos-spt-cdt sit.pem uat.pem "
                "verify loki prometheus tempo |=")

    return {"parrot": parrot, "silent": silent, "waffle": waffle, "shotgun": shotgun}


def test_debugging_evals_are_not_vacuous():
    """A case answerable without reasoning measures the prompt, not the assistant.

    Found the hard way. The first version of this suite scored 8/8 against a real
    brain and 6/8 against an asker that returned the playbook VERBATIM — because
    the playbook is in the prompt, so any token it contains is free. The score was
    real and meant almost nothing.

    So the bar is behavioural: hand the suite four answers that reasoned about
    nothing, and every one must score zero. If a future case can be satisfied by
    reciting the playbook or by naming every value at once, this fails and says so
    before the case is trusted.
    """
    from app import evals

    if not evals.load("debugging"):
        pytest.skip("no debugging cases on this machine — they live under data/, "
                    "which is gitignored because they quote internal namespaces")

    playbook = evals._playbook("debugging")
    assert playbook.strip(), "the suite declares skills that did not load"

    for name, ask in _degenerate_askers(playbook).items():
        out = asyncio.run(evals.run("debugging", ask=ask))
        passed = [r["id"] for r in out["results"] if r["ok"]]
        assert not passed, (
            f"the '{name}' answer reasoned about nothing and still passed {passed}. "
            f"Those cases measure the prompt, not the answer — tighten them so the "
            f"expected value appears only in the question, or make the case a choice "
            f"between things the playbook lists.")


def test_the_vacuity_guard_would_notice_a_weak_case(tmp_path, monkeypatch):
    """The guard must itself be able to fail, or it is decoration.

    A case whose `must` is a word straight out of the playbook is exactly the shape
    that slipped through the first time, so the guard is checked against one.
    """
    from app import evals

    suite = tmp_path / "weak.json"
    suite.write_text('{"skills": [], "cases": [{"id": "weak", "ask": "anything?", '
                     '"must": ["loki"], "why": "", "source": ""}]}')
    monkeypatch.setattr(evals, "CASES_DIR", tmp_path)

    async def parrot(_q):
        return "loki is the default datasource"

    out = asyncio.run(evals.run("weak", ask=parrot))
    assert [r["id"] for r in out["results"] if r["ok"]] == ["weak"], \
        "a weak case must be detectable — otherwise the guard above proves nothing"


# --- the local model must be a model that can talk ---------------------------

def _lmstudio_listing(monkeypatch, ids):
    """Stand in for LM Studio's /models, in the order it returns them."""
    import httpx
    from app import agent

    class _Resp:
        def json(self):
            return {"data": [{"id": i} for i in ids]}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    monkeypatch.delenv("ASTA_LOCAL_MODEL", raising=False)
    return agent


def test_an_embedding_model_is_never_picked_for_chat(monkeypatch):
    """The bug: it returned data[0] — whatever LM Studio listed first.

    Arun has `text-embedding-nomic-embed-text-v1.5` loaded alongside four chat
    models. The day the list sorted embeddings-first, every local completion would
    have come back empty — and empty here is indistinguishable from "the brain had
    nothing to say", so it would have been reported as Asta not knowing rather than
    as the wrong model being asked.
    """
    agent = _lmstudio_listing(monkeypatch, [
        "text-embedding-nomic-embed-text-v1.5", "qwen/qwen3.5-9b", "google/gemma-4-e4b"])
    assert agent._lmstudio_model_id() == "qwen/qwen3.5-9b"


def test_a_listing_of_only_embedders_yields_nothing(monkeypatch):
    """No usable model must read as "none", not as a model that will fail later."""
    agent = _lmstudio_listing(monkeypatch, ["text-embedding-nomic-embed-text-v1.5"])
    assert agent._lmstudio_model_id() is None


def test_a_pinned_model_wins(monkeypatch):
    """Load order should not decide which brain answers — the pick differs by 2x."""
    agent = _lmstudio_listing(monkeypatch, ["qwen/qwen3.5-9b", "google/gemma-4-e4b"])
    monkeypatch.setenv("ASTA_LOCAL_MODEL", "google/gemma-4-e4b")
    assert agent._lmstudio_model_id() == "google/gemma-4-e4b"


def test_a_pin_naming_something_unloaded_falls_through(monkeypatch):
    """A stale pin must not silently disable every local call.

    He unloads a model and forgets the pin; failing every local completion from
    then on — with no message saying why — is worse than quietly using one that
    is actually there.
    """
    agent = _lmstudio_listing(monkeypatch, ["qwen/qwen3.5-9b"])
    monkeypatch.setenv("ASTA_LOCAL_MODEL", "a-model-that-is-not-loaded")
    assert agent._lmstudio_model_id() == "qwen/qwen3.5-9b"


def test_a_blank_setting_falls_back_instead_of_disabling_everything(monkeypatch, tmp_path):
    """`ASTA_TEMPORAL_PROXY=` in .env must not silently switch the module off.

    This happened while writing the .env documentation for the setting: the line
    was added blank, and `os.environ.get(key, default)` uses its default only when
    the key is ABSENT. A key present and empty gave `Path("")`, which does not
    exist, so every cert check returned "nothing to check" — reporting a healthy
    silence for a module that had been disabled by a blank line.
    """
    import importlib
    from app import diagnostics as d

    monkeypatch.setenv("ASTA_TEMPORAL_PROXY", "")
    reloaded = importlib.reload(d)
    try:
        assert str(reloaded.TEMPORAL_PROXY).endswith("temporal-mcp-proxy.py"), \
            "a blank setting was taken literally instead of falling back"
        assert str(reloaded.TEMPORAL_PROXY) != "", "the path resolved to nothing"
    finally:
        monkeypatch.delenv("ASTA_TEMPORAL_PROXY", raising=False)
        importlib.reload(d)


def test_the_temporal_skill_matches_the_proxy(tmp_path):
    """The skill's env table is documentation of a mapping that lives in code.

    Documentation drifts the moment the code moves and nothing checks it — which
    is how `_pins.yml` ended up contradicting its own `lessons.md`. A brain reading
    a stale namespace queries the wrong one and gets an empty result, which reads
    as "no workflows" rather than as a wrong lookup.
    """
    from pathlib import Path as _P

    from app import diagnostics

    skill = _P("skills/temporal-analyser.md")
    if not skill.exists():
        pytest.skip("temporal-analyser skill not present")
    mod = diagnostics._proxy_module()
    if mod is None:
        pytest.skip("temporal proxy not on this machine")

    body = skill.read_text()
    for env, spec in mod.ENV_MAP.items():
        assert f"`{env}`" in body, f"env {env} is missing from the skill table"
        assert f"`{spec['namespace']}`" in body, \
            f"{env} maps to {spec['namespace']} in the proxy and that is not in the skill"
        assert f"`{spec['cert']}.pem`" in body, \
            f"{env} uses cert {spec['cert']}.pem in the proxy and that is not in the skill"


def test_any_of_accepts_a_synonym_but_still_requires_one():
    """A case must test correctness, not vocabulary — and still test something.

    Requiring the literal word "verify" failed an answer that said "confirm the
    label first", which is the same answer. But a group that accepts anything would
    be a case that cannot fail, which is finding 29 all over again.
    """
    from app import evals

    case = {"id": "syn", "must": ["telikos-spt"],
            "any_of": [["verify", "confirm", "check the label"]], "must_not": []}

    assert evals.grade("use telikos-spt and confirm the label first", case)["ok"]
    assert evals.grade("use telikos-spt and verify the label first", case)["ok"]

    bad = evals.grade("use telikos-spt, the system is definitely healthy", case)
    assert not bad["ok"], "an answer satisfying none of the group must still fail"
    assert any("or" in m for m in bad["missing"]), \
        "the report must say what the group wanted, or the failure is unreadable"

    assert not evals.grade("confirm the label first", case)["ok"], \
        "any_of must not excuse a missing `must`"


def test_startup_regenerates_the_temporal_playbook():
    """The playbook must be rebuilt on boot, not written once and trusted.

    Generated content that is only produced by hand becomes stale content: the
    file says one namespace, the proxy uses another, and the query goes to the
    wrong place and returns nothing — which reads as "no workflows" rather than as
    a wrong lookup.
    """
    from pathlib import Path as _P
    src = _P("app/main.py").read_text()
    assert "diagnostics.write_temporal_skill()" in src, \
        "nothing regenerates the playbook — it will drift the day ENV_MAP changes"
    assert "from . import" in src and "diagnostics" in src.split("\n\n")[0] + src[:4000], \
        "diagnostics must be imported at module level, or the call is a NameError " \
        "that quiet.swallow hides and the playbook silently never regenerates"


def test_the_generated_playbook_carries_every_env(tmp_path, monkeypatch):
    """Generation must cover the whole map, not the envs that happen to have certs."""
    import textwrap
    proxy = tmp_path / "temporal-mcp-proxy.py"
    proxy.write_text(textwrap.dedent('''
        CONFIG_DIR = "/nowhere"
        CLI = "temporal"
        ENV_MAP = {
            "alpha": {"address": "cdt-westeurope-01-tls.x:7233", "namespace": "ns-alpha", "cert": "alpha"},
            "beta":  {"address": "prod-westeurope-01-tls.x:7233", "namespace": "ns-beta",  "cert": "beta"},
            "gamma": {"address": "cdt-westeurope-01-tls.x:7233", "namespace": "ns-beta",  "cert": "beta"},
        }
    '''))
    monkeypatch.setattr(diagnostics, "TEMPORAL_PROXY", proxy)
    text = diagnostics.temporal_skill_text()
    for env in ("alpha", "beta", "gamma"):
        assert f"`{env}`" in text
    assert "`ns-beta`" in text
    # beta and gamma share a namespace, so the shared-certificate note must say so.
    assert "share" in text and "gamma" in text
    assert "prod" in text and "nonprod" in text, "the cluster column must be derived"
