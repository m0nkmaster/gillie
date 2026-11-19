"""Read the final measurement appended by a static Chrome layout probe."""
import html
import json
import re
import subprocess
import warnings


def _invalid_constant(value):
    raise ValueError(f'Invalid JSON constant {value}')


def run_probe(chrome, page, width, height, probe_id):
    command = [
        chrome, '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-background-networking',
        '--no-first-run', '--no-default-browser-check', '--allow-file-access-from-files',
        f'--user-data-dir={page.parent / "browser"}', f'--window-size={width},{height}',
        '--dump-dom', page.as_uri(),
    ]
    pattern = rf'<pre id="{re.escape(probe_id)}">([^<]*)</pre>'
    try:
        rendered = subprocess.run(command, capture_output=True, text=True, check=True, timeout=120)
    except subprocess.TimeoutExpired as timeout:
        stdout = timeout.stdout or ''
        try:
            if isinstance(stdout, bytes):
                stdout = stdout.decode('utf-8')
            match = re.search(pattern + r'\s*</body>\s*</html>\s*\Z', stdout)
            if not match:
                raise
            result = json.loads(html.unescape(match[1]), parse_constant=_invalid_constant)
        except ValueError as invalid:
            raise timeout from invalid
        warnings.warn(f'Chrome timed out after emitting the complete {probe_id} probe', RuntimeWarning, stacklevel=2)
        return result

    match = re.search(pattern, rendered.stdout)
    assert match, rendered.stderr[-2000:]
    return json.loads(html.unescape(match[1]), parse_constant=_invalid_constant)
