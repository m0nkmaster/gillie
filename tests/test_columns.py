"""Resizable page columns: shared divider wiring and real-CSS behaviour in Chrome."""
import json
from pathlib import Path
import re
import shutil

import pytest

from chrome_probe import run_probe


STATIC = Path(__file__).parents[1] / 'src/roosta/static'
PAGES = {
    'assistant': ['assistant.inbox', 'assistant.document'],
    'workspace': ['workspace.explore', 'workspace.results'],
    'index': ['workers.activity'],
    'pulse': ['pulse.detail', 'constellation.detail'],
}


def chrome_path():
    chrome = shutil.which('google-chrome') or shutil.which('chromium') or shutil.which('chromium-browser')
    mac = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    return chrome or (str(mac) if mac.is_file() else None)


@pytest.mark.parametrize('name', PAGES)
def test_page_loads_the_shared_divider_and_names_each_column(name):
    source = (STATIC / f'{name}.html').read_text()
    assert source.index('/static/shell.css') < source.index('/static/column-resize.js')
    for column in PAGES[name]:
        assert f"'{column}'" in source
    assert source.count('roostaColumns.attach(') == (1 if name == 'pulse' else len(PAGES[name]))


def render(tmp_path, name, seed, probe, width=1600, height=900):
    chrome = chrome_path()
    if not chrome:
        pytest.skip('Chrome or Chromium is required for layout checks')
    source = (STATIC / f'{name}.html').read_text()
    wiring = [s for s in re.findall(r'<script>([\s\S]*?)</script>', source) if 'roostaColumns.attach(' in s]
    assert len(wiring) == 1, 'column wiring should be its own script block'
    source = re.sub(r'<script\b[^>]*>[\s\S]*?</script>', '', source, flags=re.I)
    source = source.replace('/static/', STATIC.as_uri() + '/')
    script = (
        f'<script>localStorage.setItem("roosta-columns", {json.dumps(json.dumps(seed))});</script>'
        f'<script src="{STATIC.as_uri()}/column-resize.js"></script>'
        f'<script>{wiring[0]}</script>'
        f'<script>{probe}\nconst out=document.createElement("pre");out.id="column-result";'
        'out.textContent=JSON.stringify(result);document.body.appendChild(out);</script>'
    )
    page = tmp_path / 'page.html'
    page.write_text(source.replace('</body>', script + '</body>'))
    return run_probe(chrome, page, width, height, "column-result")


HELPERS = """
const w = el => Math.round(el.getBoundingClientRect().width);
const key = (el, k, shift=false) => el.dispatchEvent(new KeyboardEvent('keydown', {key:k, shiftKey:shift, bubbles:true}));
const stored = () => JSON.parse(localStorage.getItem('roosta-columns') || '{}');
const drag = (el, dx) => {
  const x = el.getBoundingClientRect().left;
  el.dispatchEvent(new PointerEvent('pointerdown', {button:0, clientX:x, pointerId:1, bubbles:true}));
  el.dispatchEvent(new PointerEvent('pointermove', {clientX:x + dx, pointerId:1, bubbles:true}));
  el.dispatchEvent(new PointerEvent('pointerup', {clientX:x + dx, pointerId:1, bubbles:true}));
};
"""


def test_workspace_columns_restore_resize_by_keyboard_and_pointer_and_reset(tmp_path):
    probe = HELPERS + """
const explore = document.getElementById('collections-card'), divider = document.getElementById('collections-divider');
const preview = document.querySelector('.preview-card');
const result = {restored: w(explore), role: divider.getAttribute('role'), focusable: divider.tabIndex,
  now: Number(divider.getAttribute('aria-valuenow'))};
key(divider, 'ArrowRight'); result.right = w(explore); result.saved = stored()['workspace.explore'];
key(divider, 'ArrowLeft', true); result.shiftLeft = w(explore);
drag(divider, 40); result.dragged = w(explore); result.savedDrag = stored()['workspace.explore'];
key(divider, 'End'); result.previewAtMax = w(preview);
key(divider, 'Home'); result.home = w(explore);
key(divider, 'Enter'); result.reset = w(explore); result.afterReset = 'workspace.explore' in stored();
"""
    result = render(tmp_path, 'workspace', {'workspace.explore': 300}, probe)
    assert result['restored'] == 300 and result['now'] == 300, result
    assert result['role'] == 'separator' and result['focusable'] == 0, result
    assert result['right'] == 316 and result['saved'] == 316, result
    assert result['shiftLeft'] == 252, result
    assert result['dragged'] == 292 and result['savedDrag'] == 292, result
    assert result['previewAtMax'] >= 319, result
    assert result['home'] == 180, result
    assert result['reset'] == 270 and result['afterReset'] is False, result


def test_assistant_document_divider_grows_leftwards_and_keeps_the_conversation(tmp_path):
    probe = HELPERS + """
const docpane = document.getElementById('docpane'), divider = document.getElementById('docpane-divider');
const inbox = document.getElementById('attention-queue'), conversation = document.getElementById('pane');
const result = {inbox: w(inbox), hiddenDivider: getComputedStyle(divider).display};
docpane.hidden = false; docpane.textContent = 'Document';
result.shownDivider = getComputedStyle(divider).display; result.doc = w(docpane);
key(divider, 'ArrowLeft'); result.left = w(docpane);
key(divider, 'End'); result.conversationAtMax = w(conversation);
"""
    result = render(tmp_path, 'assistant', {'assistant.inbox': 400, 'assistant.document': 500}, probe)
    assert result['inbox'] == 400, result
    assert result['hiddenDivider'] == 'none' and result['shownDivider'] != 'none', result
    assert result['doc'] == 500 and result['left'] == 516, result
    assert result['conversationAtMax'] >= 359, result


def test_assistant_document_gives_way_when_a_saved_inbox_is_wide(tmp_path):
    probe = HELPERS + """
const docpane = document.getElementById('docpane');
docpane.hidden = false; docpane.textContent = 'Document';
const result = {conversation: w(document.getElementById('pane')), inbox: w(document.getElementById('attention-queue'))};
"""
    result = render(tmp_path, 'assistant', {'assistant.inbox': 640, 'assistant.document': 900}, probe, width=1400)
    assert result['inbox'] == 640 and result['conversation'] >= 359, result
