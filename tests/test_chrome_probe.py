import subprocess
from unittest.mock import Mock

import pytest

from chrome_probe import run_probe


DOM = '<html><body><pre id="layout-result">{"label":"café &amp; tea"}</pre>\n</body></html>\n'
EXPECTED = {'label': 'café & tea'}


def test_normal_completion(tmp_path, monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, DOM, ''))
    monkeypatch.setattr(subprocess, 'run', run)
    assert run_probe('chrome', tmp_path / 'page.html', 650, 400, 'layout-result') == EXPECTED
    assert run.call_args.kwargs == dict(capture_output=True, text=True, check=True, timeout=120)


def test_timeout_after_complete_dump(tmp_path, monkeypatch):
    timeout = subprocess.TimeoutExpired('chrome', 120, output=DOM.encode())
    monkeypatch.setattr(subprocess, 'run', Mock(side_effect=timeout))
    with pytest.warns(RuntimeWarning, match='complete layout-result'):
        assert run_probe('chrome', tmp_path / 'page.html', 650, 400, 'layout-result') == EXPECTED


@pytest.mark.parametrize('output', [
    DOM.split('</body>')[0],
    DOM + 'trailing bytes',
    DOM.replace('layout-result', 'layout-result-extra'),
    DOM.replace('{"label":', '{"label"'),
    DOM.replace('"café &amp; tea"', 'NaN'),
])
def test_timeout_without_valid_dump(tmp_path, monkeypatch, output):
    timeout = subprocess.TimeoutExpired('chrome', 120, output=output.encode())
    monkeypatch.setattr(subprocess, 'run', Mock(side_effect=timeout))
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        run_probe('chrome', tmp_path / 'page.html', 650, 400, 'layout-result')
    assert raised.value is timeout


def test_nonzero_exit_with_complete_dump(tmp_path, monkeypatch):
    error = subprocess.CalledProcessError(1, 'chrome', output=DOM)
    monkeypatch.setattr(subprocess, 'run', Mock(side_effect=error))
    with pytest.raises(subprocess.CalledProcessError) as raised:
        run_probe('chrome', tmp_path / 'page.html', 650, 400, 'layout-result')
    assert raised.value is error
