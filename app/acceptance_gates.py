"""Run the fixed release gates; skipped, missing and incomplete tests fail closed.

This is a deterministic regression report, not a real-world completion score.
Run from the repository root: python -m app.acceptance_gates --report gates.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


MANIFEST = Path(__file__).resolve().parent.parent / 'docs/asta-improvement/acceptance-gates-v1.json'


def matches(nodeid: str, selector: str) -> bool:
    return nodeid == selector or nodeid.startswith(selector + '[')


def summarize(manifest: dict, collected: list[str], reports: dict, exit_code: int) -> dict:
    gates = {}
    for name, selectors in manifest['gates'].items():
        missing = [s for s in selectors if not any(matches(n, s) for n in collected)]
        nodes = sorted(n for n in collected if any(matches(n, s) for s in selectors))
        failures = []
        for node in nodes:
            phases = reports.get(node, {})
            if any(phases.get(p) != 'passed' for p in ('setup', 'call', 'teardown')):
                failures.append({'nodeid': node, 'phases': phases})
        gates[name] = {'passed': bool(nodes) and not missing and not failures,
                       'tests': len(nodes), 'missing': missing, 'failures': failures}
    return {'version': manifest['version'], 'kind': 'deterministic_acceptance_gates',
            'passed': exit_code == 0 and bool(gates) and all(g['passed'] for g in gates.values()),
            'pytest_exit_code': int(exit_code), 'gates': gates}


class AcceptanceGates:
    def __init__(self, manifest: dict, report: Path):
        self.manifest, self.report = manifest, report
        self.collected: list[str] = []
        self.reports: dict = {}
        self.result: dict | None = None

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

    def pytest_runtest_logreport(self, report):
        outcome = 'xfail' if hasattr(report, 'wasxfail') else report.outcome
        self.reports.setdefault(report.nodeid, {})[report.when] = outcome

    def pytest_sessionfinish(self, session, exitstatus):
        self.result = summarize(self.manifest, self.collected, self.reports, exitstatus)
        self.result['manifest_sha256'] = hashlib.sha256(
            json.dumps(self.manifest, sort_keys=True).encode()).hexdigest()
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.report.write_text(json.dumps(self.result, indent=2) + '\n')
        if not self.result['passed']:
            session.exitstatus = 1


def load_manifest(path: Path = MANIFEST) -> dict:
    manifest = json.loads(path.read_text())
    gates = manifest.get('gates', {})
    if manifest.get('version') != 1 or set(gates) != {f'G{i}' for i in range(1, 22)}:
        raise ValueError('expected all 21 version-1 gates')
    for selectors in gates.values():
        if (not isinstance(selectors, list) or not selectors
                or any(not isinstance(s, str) or not s.startswith('tests/test_')
                       or '::test_' not in s or '..' in s for s in selectors)
                or len(set(selectors)) != len(selectors)):
            raise ValueError('each gate needs distinct test selectors')
    return manifest


def main(argv=None) -> int:
    import pytest
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    args.report.unlink(missing_ok=True)
    manifest = load_manifest()
    selectors = sorted({s for group in manifest['gates'].values() for s in group})
    plugin = AcceptanceGates(manifest, args.report)
    result = pytest.main(['-q', '-p', 'no:cacheprovider', *selectors], plugins=[plugin])
    # Initialization errors might bypass pytest_sessionfinish. Never keep an old green report.
    if plugin.result is None:
        plugin.result = summarize(manifest, [], {}, int(result) or 1)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(plugin.result, indent=2) + '\n')
    return int(result) if plugin.result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
