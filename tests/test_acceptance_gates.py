import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import acceptance_gates as gates


MANIFEST = {'version': 1, 'gates': {'G1': ['test_case.py::test_required']}}
NODE = 'test_case.py::test_required'
PHASES = dict.fromkeys(('setup', 'call', 'teardown'), 'passed')


@pytest.mark.parametrize('phase,outcome', [
    ('setup', 'skipped'), ('call', 'skipped'), ('call', 'xfail'),
    ('call', 'failed'), ('teardown', 'failed'), ('call', None),
])
def test_nonpassing_or_missing_phase_fails_gate(phase, outcome):
    result = gates.summarize(MANIFEST, [NODE], {NODE: {**PHASES, phase: outcome}}, 0)
    assert not result['passed']
    assert not result['gates']['G1']['passed']


def test_uncollected_test_cannot_pass_via_another_successful_test():
    result = gates.summarize(MANIFEST, ['test_case.py::test_other'], {}, 0)
    assert not result['passed']
    assert result['gates']['G1']['missing'] == [NODE]


def test_every_parameterized_instance_must_complete():
    nodes = [NODE + '[a]', NODE + '[b]']
    assert not gates.summarize(MANIFEST, nodes, {nodes[0]: PHASES}, 0)['passed']
    assert gates.summarize(MANIFEST, nodes, dict.fromkeys(nodes, PHASES), 0)['passed']


def test_collection_error_outside_gate_still_fails_report():
    assert not gates.summarize(MANIFEST, [NODE], {NODE: PHASES}, 2)['passed']


def test_versioned_manifest_contains_every_documented_gate():
    manifest = gates.load_manifest()
    assert set(manifest['gates']) == {f'G{i}' for i in range(1, 22)}


@pytest.mark.parametrize('body,passed', [
    ('def test_required(): pass', True),
    ('import pytest\n@pytest.mark.skip(reason="missing dependency")\ndef test_required(): pass', False),
    ('import pytest\n@pytest.mark.xfail\ndef test_required(): assert False', False),
    ('import pytest\n@pytest.mark.xfail\ndef test_required(): pass', False),
    ('def test_required(): assert False', False),
    ('def test_other(): pass', False),
])
def test_real_pytest_process_cannot_hide_skipped_or_missing_gate(tmp_path, body, passed):
    (tmp_path / 'test_case.py').write_text(body)
    report = tmp_path / 'report.json'
    script = (
        'import pytest; from pathlib import Path; '
        'from app.acceptance_gates import AcceptanceGates; '
        f'p = AcceptanceGates({MANIFEST!r}, Path({str(report)!r})); '
        'raise SystemExit(pytest.main(["-q", "test_case.py"], plugins=[p]))'
    )
    env = {**os.environ, 'PYTHONPATH': str(Path(gates.__file__).resolve().parent.parent)}
    run = subprocess.run([sys.executable, '-c', script], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=45)
    assert (run.returncode == 0) is passed, run.stdout + run.stderr
    result = json.loads(report.read_text())
    assert result['passed'] is passed
    assert result['manifest_sha256']
