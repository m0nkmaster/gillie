"""Render real page CSS without the live runner, polling or external scripts."""
import html
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


STATIC = Path(__file__).parents[1] / 'src/roosta/static'


@pytest.mark.parametrize('name,width,height', [('settings', 1600, 600), ('assistant', 650, 400)])
def test_page_fills_width_and_bottom_controls_remain_reachable(tmp_path, name, width, height):
    chrome = shutil.which('google-chrome') or shutil.which('chromium') or shutil.which('chromium-browser')
    if not chrome:
        mac = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
        if mac.is_file():
            chrome = str(mac)
    if not chrome:
        pytest.skip('Chrome or Chromium is required for layout checks')
    source = (STATIC / f'{name}.html').read_text()
    source = re.sub(r'<script\b[^>]*>[\s\S]*?</script>', '', source, flags=re.I)
    source = source.replace('/static/', STATIC.as_uri() + '/')
    probe = """
if (document.body.classList.contains('assistant-page')) {
  document.getElementById('pane').innerHTML = '<div class="conversation-header">Chat</div>' +
    '<div class="logwrap"><div class="log">' + '<div class="msg"><div class="bubble">A message to read.</div></div>'.repeat(40) +
    '</div></div><form class="form"><textarea class="input form-control"></textarea><button class="btn btn-primary">Send</button></form>';
} else {
  document.querySelectorAll('details').forEach(el => el.open = true);
}
const main = document.querySelector('main,.main');
for (const el of [main, document.querySelector('.logwrap')]) {
  if (el) el.scrollTo({top:el.scrollHeight, behavior:'instant'});
}
window.scrollTo({top:document.scrollingElement.scrollHeight, behavior:'instant'});
const composer = document.querySelector('.form');
const log = document.querySelector('.logwrap');
const result = {
  // clientWidth/Height exclude classic scrollbars (Linux and some macOS runners).
  width:document.documentElement.clientWidth, height:document.documentElement.clientHeight, mainWidth:main.getBoundingClientRect().width,
  mainBottom:main.getBoundingClientRect().bottom,
  composerBottom:composer?.getBoundingClientRect().bottom,
  logHeight:log?.clientHeight, logRemaining:log ? log.scrollHeight-log.clientHeight-log.scrollTop : null,
};
const output = document.createElement('pre');
output.id = 'layout-result'; output.textContent = JSON.stringify(result);
document.body.appendChild(output);
"""
    source = source.replace('</body>', '<script>' + probe + '</script></body>')
    page = tmp_path / 'page.html'
    page.write_text(source)
    rendered = subprocess.run([
        chrome, '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-background-networking',
        '--no-first-run', '--no-default-browser-check', '--allow-file-access-from-files',
        f'--user-data-dir={tmp_path / "browser"}', f'--window-size={width},{height}',
        '--dump-dom', page.as_uri(),
    ], capture_output=True, text=True, check=True, timeout=120)
    match = re.search(r'<pre id="layout-result">(.*?)</pre>', rendered.stdout, re.S)
    assert match, rendered.stderr[-2000:]
    result = json.loads(html.unescape(match[1]))
    if name == 'settings':
        assert result['mainWidth'] == pytest.approx(result['width'], abs=1), result
        assert result['mainBottom'] <= result['height'] + 1, result
    else:
        assert result['composerBottom'] <= result['height'] + 1, result
        assert result['logHeight'] > 0 and abs(result['logRemaining']) <= 1, result
