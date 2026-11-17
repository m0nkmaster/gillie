(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let state, poll, editing = null, formOpen = false;
  async function request(url, body) {
    const response = await fetch(url, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not save AI settings');
    return data;
  }
  function message(text, error = false) {
    $('ai-message').className = text ? `alert ${error ? 'alert-danger' : 'alert-info'} mb-3` : '';
    $('ai-message').textContent = text;
  }
  function authOptions(provider) {
    const options = [];
    if (provider === 'anthropic') {
      options.push({value:'system', label:'Use Claude Code’s existing login on this computer'});
      if (editing?.auth === 'cli') options.push({value:'cli', label:'Claude account · including enterprise'});
      options.push({value:'api_key', label:'API key · API billing'});
    } else if (provider === 'openai') {
      options.push({value:'cli', label:'Sign in with ChatGPT · including enterprise'});
      options.push({value:'api_key', label:'API key · API billing'});
    }
    return options;
  }
  function providerChanged() {
    const provider = $('ai-provider').value, info = state.providers[provider];
    $('ai-billing').textContent = info.billing;
    $('ai-label').value = provider === 'anthropic' ? 'Claude Code' : provider === 'openai' ? 'ChatGPT' : info.label;
    $('ai-key-link').href = info.key_url;
    const options = authOptions(provider);
    $('ai-auth-wrap').hidden = !options.length;
    $('ai-auth').innerHTML = options.map(o => `<option value="${o.value}">${esc(o.label)}</option>`).join('');
    if (editing && options.some(o => o.value === editing.auth)) $('ai-auth').value = editing.auth;
    authChanged();
  }
  function authChanged() {
    const provider = $('ai-provider').value, local = provider === 'ollama';
    const auth = local ? 'local' : ($('ai-auth').value || 'api_key');
    $('ai-key-wrap').hidden = auth !== 'api_key';
    $('ai-key').required = auth === 'api_key';
    $('ai-key').value = '';
    $('ai-signin').hidden = auth !== 'cli';
    if (provider === 'openai' && !editing && ['ChatGPT', 'OpenAI'].includes($('ai-label').value)) $('ai-label').value = auth === 'cli' ? 'ChatGPT' : 'OpenAI';
    if (auth === 'cli') {
      $('ai-signin').textContent = 'Sign in with ' + (provider === 'openai' ? 'ChatGPT' : 'Claude') + ' in a browser on this computer. Choose your organisation account if applicable; access depends on its policy. Roosta never asks for subscription tokens. After sign-in, run the connection test.';
      $('ai-billing').textContent = (provider === 'openai' ? 'ChatGPT' : 'Claude') + ' account · your organisation or subscription plan; access depends on organisation policy.';
    } else if (auth === 'system') {
      $('ai-billing').textContent = 'Roosta uses Claude Code and its existing login on this computer. If you already use Claude Code, there is no separate account or API key to set up.';
    } else if (!local) {
      $('ai-billing').textContent = state.providers[provider].billing;
    }
  }
  function showForm(show) {
    formOpen = show;
    $('ai-form').hidden = !show;
    $('ai-add-toggle').hidden = show;
  }
  function engineRow(engine) {
    const d = state.engines?.[engine.id] || {}, job = state.jobs[engine.id];
    let status, action = '';
    if (d.installed) { status = d.source === 'yours' ? `Using your ${engine.name} ${esc(d.version)}` : `Installed by Roosta ${esc(d.version)}`; action = `<button class="btn btn-sm btn-link" data-op="install" data-id="${engine.id}" ${job?.status === 'running' ? 'disabled' : ''}>Update to latest</button>`; }
    else { status = 'Not installed'; action = `<button class="btn btn-sm" data-op="install" data-id="${engine.id}" ${job?.status === 'running' ? 'disabled' : ''}>Install</button>`; }
    return `<div class="list-group-item px-0 d-flex align-items-center justify-content-between gap-2">
      <div><strong>${engine.name}</strong> <span class="text-secondary">· for ${engine.for}</span>
        <div class="text-secondary">${status}${job ? ` · ${esc(job.status)}` : ''}</div>
        ${job?.error ? `<p class="text-danger mb-0">${esc(job.error)}</p>` : ''}
      </div><div class="d-flex flex-column align-items-end gap-2">
        ${d.installed && state.terminal_available ? `<button class="btn btn-sm" data-terminal="${engine.id}" ${job?.status === 'running' ? 'disabled' : ''}>Open in Terminal</button>` : ''}
        ${d.installed && action ? `<details class="small"><summary>Updates</summary><p class="text-secondary mt-2 mb-2">Download the latest release from the publisher; your other installations are kept.</p>${action}</details>` : action}
      </div></div>`;
  }
  function software(c) {
    return c.provider === 'anthropic' ? 'Claude Code' : 'Pi';
  }
  function accountExplanation(c) {
    return c.auth === 'system'
      ? 'Uses your existing Claude Code login on this computer. No separate Roosta sign-in or API key.'
      : `Runs through ${software(c)} on this computer. ${c.billing}`;
  }
  function currentAI() {
    const c = state.connections.find(c => c.id === state.default);
    if (c) return `<strong>${esc(c.label)}</strong> <span class="${c.connected && c.tested ? 'text-success' : 'text-secondary'} small">· ${esc(statusLine(c))}</span>
      <p class="mt-2 mb-0">${esc(accountExplanation(c))}</p>
      <p class="text-secondary small mt-2 mb-0">Default for new chats and workers that follow the default. Existing chats keep their account.</p>`;
    const existing = state.connections.find(c => c.auth === 'system' && c.connected);
    return existing
      ? '<strong>Claude Code found on this computer</strong><p class="mt-2 mb-0">Roosta can use your existing Claude Code login. Check the connection below to finish; the first account that passes becomes your default.</p>'
      : '<strong>Connect your AI to get started</strong><p class="mt-2 mb-0">Choose Claude, ChatGPT or another supported service below.</p>';
  }
  function statusLine(c) {
    if (!c.connected) return 'Disconnected';
    if (c.tested) return 'Ready';
    if (c.signed_in === false) return 'Not signed in';
    return c.signed_in ? 'Signed in · test the connection to finish' : 'Needs a connection test';
  }
  function connectionRow(c) {
    const engine = c.provider === 'anthropic' ? 'claude' : 'pi';
    const installed = state.engines?.[engine]?.installed;
    const job = state.jobs[engine]?.status === 'running' ? state.jobs[engine] : state.jobs[c.id];
    const busy = job?.status === 'running';
    const affected = state.affected[c.id];
    const isDefault = state.default === c.id;
    const action = (label, attribute, primary = false, disabled = false) =>
      `<button class="btn btn-sm ${primary ? 'btn-primary' : ''}" ${attribute} ${busy || disabled ? 'disabled' : ''}>${label}</button>`;
    const op = (name, label, primary = false, disabled = false) => action(label, `data-op="${name}" data-id="${esc(c.id)}"`, primary, disabled);
    const chatTotal = affected ? affected.chat_count ?? affected.chats.length : 0;
    const usage = affected ? [
      [affected.workers.length, 'worker'], [chatTotal, 'chat'], [affected.pending_approvals.length, 'pending approval'],
    ].filter(([n]) => n).map(([n, label]) => `${n} ${label}${n === 1 ? '' : 's'}`).join(', ') : '';
    return `<article class="list-group-item px-0">
      <div class="ai-account-heading">
        <div><h3>${esc(c.label)} ${isDefault ? '<span class="badge ms-1">Default</span>' : ''}</h3>
          <div class="text-secondary small mt-1">${esc(accountExplanation(c))}</div></div>
        <span class="small ${job?.error ? 'text-danger' : c.tested && c.connected ? 'text-success' : 'text-secondary'}" role="status">${busy ? `${esc(job.operation)} · running` : esc(statusLine(c))}</span>
      </div>
      ${job?.error || state.jobs[engine]?.error ? `<p class="text-danger mt-2 mb-0">${esc(job?.error || state.jobs[engine].error)}</p>` : ''}
      ${c.auth === 'system' && job?.operation === 'test' && job.status === 'error' ? '<p class="text-secondary mt-2">Open Terminal, run <code>claude</code> and sign in, then test again.</p>' : ''}
      <div class="ai-account-actions">
        ${!c.connected ? action('Reconnect', `data-change="${esc(c.id)}"`, true) :
          !installed ? action(`Install ${software(c)}`, `data-op="install" data-id="${engine}"`, true) :
          c.auth === 'cli' && c.signed_in === false ? op('login', 'Sign in', true) :
          !c.tested ? op('test', 'Check connection', true) :
          !isDefault ? action('Use as default', `data-default="${esc(c.id)}"`) : '<span class="text-secondary small">Used for new chats and workers that follow the default.</span>'}
      </div>
      <details class="ai-account-details" data-account-details="${esc(c.id)}">
        <summary class="small">Manage account${usage ? ` · ${usage}` : ''}</summary>
        <div class="d-flex gap-2 flex-wrap mt-2">
          ${action('Edit account', `data-change="${esc(c.id)}"`)}
          ${c.connected ? `${c.auth === 'cli' ? op('login', 'Sign in again') : ''}${op('test', 'Test connection', false, c.signed_in === false)}
          <button class="btn btn-sm btn-ghost-secondary" data-disconnect="${esc(c.id)}" ${busy ? 'disabled' : ''}>Disconnect</button>` : ''}
        </div>
        ${usage ? `<p class="text-secondary small mt-2">Disconnecting leaves this work waiting for a replacement account.</p><ul class="small">${affected.workers.map(id => `<li><a href="/runner?task=${encodeURIComponent(id)}">Worker: ${esc(id)}</a></li>`).join('')}${affected.chats.map(id => `<li><a href="/assistant?item=${encodeURIComponent(id)}">${esc(affected.chat_titles?.[id] || id)}</a></li>`).join('')}</ul>${chatTotal > affected.chats.length ? `<p class="text-secondary small mb-0">Showing the latest ${affected.chats.length} of ${chatTotal} chats.</p>` : ''}` : '<p class="text-secondary small mt-2 mb-0">No chats, workers or pending approvals use this account.</p>'}
      </details>
    </article>`;
  }
  async function loadEfforts(effort = '') {
    const model = $('settings-model'), select = $('settings-effort');
    select.disabled = true;
    $('settings-effort-field').hidden = true;
    select.innerHTML = '<option value="">Default</option>';
    if (!model.value) return;
    const value = model.value;
    const [connection, name] = JSON.parse(value);
    try {
      const data = await request(`/api/ai/efforts?connection=${encodeURIComponent(connection)}&model=${encodeURIComponent(name)}`);
      if (model.value !== value) return;
      select.innerHTML += data.levels.map(level => `<option value="${esc(level)}">${esc(level)}</option>`).join('');
      select.value = data.levels.includes(effort) ? effort : '';
      $('settings-effort-field').hidden = !data.levels.length;
      select.disabled = false;
    } catch (e) {
      if (model.value === value) $('model-message').textContent = e.message;
    }
  }
  async function loadModels() {
    const select = $('settings-model');
    select.disabled = true;
    $('settings-effort-field').hidden = true;
    const accounts = state.connections.filter(c => c.connected && c.tested);
    if (!accounts.length) {
      select.innerHTML = '<option>Connect an account to choose a model</option>';
      $('account-setup').open = true;
      return;
    }
    const listings = await Promise.all(accounts.map(async c => {
      try { return {c, data:await request(`/api/ai/models?connection=${encodeURIComponent(c.id)}`)}; }
      catch (e) { return {c, data:{models:[], error:e.message}}; }
    }));
    select.innerHTML = '<option value="" disabled>Choose a model</option>' + listings.map(({c, data}) =>
      `<optgroup label="${esc(c.label)}">${data.models.map(m => `<option value="${esc(JSON.stringify([c.id,m.id]))}">${esc(m.label)}</option>`).join('')}</optgroup>`).join('');
    const current = listings.find(({c}) => c.id === state.default);
    const model = state.model_choice?.provider === current?.c.provider ? state.model_choice.model : current?.data.default;
    select.value = JSON.stringify([state.default, model]);
    if (select.selectedIndex < 0) { select.value = ''; $('model-settings').open = true; }
    select.disabled = !listings.some(({data}) => data.models.length);
    $('model-message').textContent = listings.filter(({data}) => data.error).map(({c,data}) => `${c.label}: ${data.error}`).join(' ');
    await loadEfforts(state.model_choice?.effort || '');
  }
  async function saveModel(effort = '') {
    const select = $('settings-model');
    select.disabled = true;
    $('settings-effort').disabled = true;
    try {
      const [connection, model] = JSON.parse(select.value);
      await request('/api/ai/default', {connection, model, effort});
      await load();
      $('model-message').textContent = 'Model settings saved.';
    } catch (e) {
      await loadModels();
      $('model-message').textContent = e.message;
    }
  }
  $('settings-model').addEventListener('change', () => saveModel());
  $('settings-effort').addEventListener('change', () => saveModel($('settings-effort').value));
  async function load() {
    const firstLoad = !state;
    const previous = state?.jobs || {};
    state = await request('/api/ai/connections');
    for (const [id, job] of Object.entries(state.jobs)) {
      if (job.operation === 'login' && job.status === 'success' && previous[id]?.status === 'running') {
        message('Signed in. Test the connection to finish setup.');
      }
    }
    if (!$('ai-provider').options.length) {
      const names = {anthropic:'Claude', openai:'ChatGPT / OpenAI', openrouter:'OpenRouter', ollama:'Local models (Ollama)'};
      $('ai-provider').innerHTML = ['anthropic', 'openai', 'openrouter', 'ollama'].filter(id => state.providers[id]).map(id => `<option value="${id}">${names[id]}</option>`).join('');
      providerChanged();
    }
    $('ai-current').innerHTML = currentAI();
    $('setup-notice').hidden = !state.workspace_setup_ready;
    $('setup-missing').textContent = 'Still missing: ' + (state.workspace_setup_missing || []).join(', ') + '.';
    const engines = [{id:'claude', name:'Claude Code', for:'Anthropic models'}, {id:'pi', name:'Pi', for:'OpenAI, OpenRouter and local models'}];
    $('ai-engines').innerHTML = engines.map(engineRow).join('');
    const openAccounts = new Set([...document.querySelectorAll('[data-account-details][open]')].map(el => el.dataset.accountDetails));
    $('ai-connections').innerHTML = state.connections.map(connectionRow).join('') || '<p class="text-secondary">Add your first account to get started. Existing conversations are retained.</p>';
    document.querySelectorAll('[data-account-details]').forEach(el => { el.open = openAccounts.has(el.dataset.accountDetails); });
    showForm(formOpen || (firstLoad && !state.connections.some(c => c.connected)));
    if (!state.connections.some(c => c.connected && c.tested)) $('account-setup').open = true;
    await loadModels();
    clearTimeout(poll);
    if (Object.values(state.jobs).some(j => j.status === 'running')) poll = setTimeout(() => load().catch(e => message(e.message, true)), 1500);
  }
  $('ai-provider').addEventListener('change', providerChanged);
  $('ai-auth').addEventListener('change', authChanged);
  $('ai-add-toggle').addEventListener('click', () => { showForm(true); $('ai-provider').focus(); });
  function resetForm() {
    editing = null;
    $('ai-provider').disabled = false;
    $('ai-auth').disabled = false;
    $('ai-form-title').textContent = 'Add account';
    $('ai-form').querySelector('button[type="submit"]').textContent = 'Add account';
    providerChanged();
    showForm(false);
  }
  $('ai-cancel').addEventListener('click', () => { resetForm(); message(''); $('ai-add-toggle').focus(); });
  $('ai-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = event.submitter;
    button.disabled = true;
    try {
      const provider = $('ai-provider').value;
      const auth = provider === 'ollama' ? 'local' : ['anthropic', 'openai'].includes(provider) ? ($('ai-auth').value || 'api_key') : 'api_key';
      const c = await request(editing ? `/api/ai/connections/${editing.id}/change` : '/api/ai/connections', {provider, auth, label:$('ai-label').value, api_key:$('ai-key').value});
      $('ai-key').value = '';
      resetForm();
      message(`Saved ${c.label}. ${auth === 'cli' ? 'Follow the next step shown on the account below to install software, sign in and check the connection.' : auth === 'system' ? 'Run Check connection to use your existing Claude Code login.' : 'Follow the next step shown on the account below.'}`);
      await load();
    } catch (e) { message(e.message, true); }
    finally { button.disabled = false; }
  });
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-op], [data-terminal], [data-default], [data-disconnect], [data-change]');
    if (!button) return;
    if (button.dataset.change) {
      const c = state.connections.find(c => c.id === button.dataset.change);
      editing = c; showForm(true); $('ai-form-title').textContent = 'Edit account'; $('ai-provider').value = c.provider; providerChanged();
      $('ai-provider').disabled = true; $('ai-auth').value = c.auth; $('ai-auth').disabled = true; authChanged();
      $('ai-label').value = c.label;
      $('ai-key').required = c.auth === 'api_key' && !c.connected;
      $('ai-form').querySelector('button[type="submit"]').textContent = 'Save connection';
      message('Changes apply to every chat and worker using this connection. Leave the key blank to keep it. Test again after saving.');
      $('ai-label').focus(); return;
    }
    button.disabled = true;
    try {
      if (button.dataset.terminal) {
        await request('/api/ai/terminal', {id:button.dataset.terminal});
        message('Opened in Terminal. This is a standalone session using the tool’s own login and settings.');
      } else if (button.dataset.op) {
        await request('/api/ai/operations', {id:button.dataset.id, operation:button.dataset.op});
        message(button.dataset.op === 'login' ? 'Complete account sign-in in your browser on this computer, then test the connection.' : 'Working… You can leave this page while the operation runs.');
      } else if (button.dataset.default) {
        await request('/api/ai/default', {connection:button.dataset.default});
        message('Default changed. Existing chats and workers retain their account choices.');
      } else {
        await request(`/api/ai/connections/${button.dataset.disconnect}/disconnect`, {});
        message('Disconnected. Choose a replacement explicitly for affected chats and workers.');
      }
      await load();
    } catch (e) { message(e.message, true); button.disabled = false; }
  });
  load().catch(e => message(e.message, true));
})();
