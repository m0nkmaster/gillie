(() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const split = value => { const [name, effort = ''] = String(value || '').split('@'); return {name, effort}; };
  const join = (name, effort) => name ? (effort ? `${name}@${effort}` : name) : (effort ? `@${effort}` : '');
  async function load(connection, {general = false, refresh = false} = {}) {
    const response = await fetch(`/api/ai/models?connection=${encodeURIComponent(connection)}&general=${general ? 1 : 0}${refresh ? '&refresh=1' : ''}`);
    return response.ok ? response.json() : null;
  }
  function efforts(data, name) {
    return name ? (data.models.find(m => m.id === name)?.efforts || []) : (data.default_efforts || []);
  }
  function fillEfforts(data, name, effort, effortSelect) {
    const levels = efforts(data, name);
    const field = effortSelect.closest('[data-effort-field]') || effortSelect;
    field.hidden = !levels.length;
    effortSelect.innerHTML = ['', ...levels].map(l => `<option value="${esc(l)}">${esc(l || 'Default')}</option>`).join('');
    effortSelect.value = levels.includes(effort) ? effort : '';
  }
  function fill(data, current, modelSelect, effortSelect) {
    const whole = data.models.some(m => m.id === current);
    const {name, effort} = whole ? {name: current, effort: ''} : split(current);
    const options = [{id: '', label: 'Default' + (data.default_label ? ` · ${data.default_label}` : ''), description: ''}]
      .concat(data.models.map(m => ({id: m.id, label: m.label, description: m.description || ''})));
    if (name && !options.some(o => o.id === name)) options.push({id: name, label: name, description: ''});
    modelSelect.innerHTML = options.map(o => `<option value="${esc(o.id)}" title="${esc(o.description)}">${esc(o.label)}</option>`).join('');
    modelSelect.value = name;
    fillEfforts(data, name, effort, effortSelect);
  }
  window.roostaModelPicker = {load, fill, fillEfforts, split, join};
})();
