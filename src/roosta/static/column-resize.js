// Resizable page columns. A page marks a divider element and calls
// roostaColumns.attach(divider, options); the divider becomes a focusable
// vertical separator that sets one CSS width variable on a layout container.
// Drag it, or focus it and use the arrow keys (Shift for bigger steps),
// Home/End for the limits and Enter or a double-click to restore the default.
// Widths are remembered per page and column in localStorage, so a page comes
// back at the widths last chosen there, across app restarts and open windows.
(function () {
  const KEY = 'roosta-columns';
  const attached = [];

  function readAll() {
    try {
      const value = JSON.parse(localStorage.getItem(KEY) || '{}');
      return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
    } catch { return {}; }
  }
  function saved(id) {
    const width = readAll()[id];
    return typeof width === 'number' && Number.isFinite(width) && width > 0 ? width : null;
  }
  function store(id, width) {
    const all = readAll();
    if (width == null) delete all[id]; else all[id] = Math.round(width);
    try { localStorage.setItem(KEY, JSON.stringify(all)); } catch { /* storage full or denied */ }
  }
  function clamp(width, min, max) {
    return Math.round(Math.min(Math.max(width, min), Math.max(min, max)));
  }

  function attach(divider, options) {
    const { id, label, target, variable } = options;
    const container = options.container || target.parentElement;
    const grow = options.grow === 'left' ? -1 : 1;
    const step = options.step || 16;
    const min = options.min || 160;
    const limit = () => {
      const max = typeof options.max === 'function'
        ? options.max(container.getBoundingClientRect().width) : options.max;
      return Number.isFinite(max) ? max : Infinity;
    };
    let chosen = saved(id);

    divider.classList.add('column-divider');
    divider.setAttribute('role', 'separator');
    divider.setAttribute('aria-orientation', 'vertical');
    divider.setAttribute('aria-label', label);
    if (target.id) divider.setAttribute('aria-controls', target.id);
    divider.tabIndex = 0;
    divider.title = `${label}: drag or use the arrow keys; double-click or Enter restores the default`;

    function describe() {
      const max = limit();
      divider.setAttribute('aria-valuemin', String(min));
      if (Number.isFinite(max)) divider.setAttribute('aria-valuemax', String(Math.round(Math.max(min, max))));
      divider.setAttribute('aria-valuenow', String(Math.round(target.getBoundingClientRect().width)));
    }
    function show(width) {
      if (width == null) container.style.removeProperty(variable);
      else container.style.setProperty(variable, clamp(width, min, limit()) + 'px');
      describe();
      divider.dispatchEvent(new CustomEvent('roosta-columns-change', { bubbles: true, detail: { id } }));
    }
    function choose(width) {
      chosen = width == null ? null : clamp(width, min, limit());
      store(id, chosen);
      show(chosen);
    }
    const current = () => target.getBoundingClientRect().width;

    divider.addEventListener('keydown', event => {
      const delta = (event.shiftKey ? step * 4 : step) * grow;
      let width = null;
      if (event.key === 'ArrowRight') width = current() + delta;
      else if (event.key === 'ArrowLeft') width = current() - delta;
      else if (event.key === 'Home') width = min;
      else if (event.key === 'End') width = limit();
      else if (event.key === 'Enter') { event.preventDefault(); choose(null); return; }
      else return;
      event.preventDefault();
      choose(width);
    });
    divider.addEventListener('dblclick', () => choose(null));
    divider.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      event.preventDefault();
      divider.focus();
      const startX = event.clientX;
      const startWidth = current();
      try { divider.setPointerCapture(event.pointerId); } catch { /* pointer already released */ }
      divider.classList.add('dragging');
      document.body.classList.add('column-resizing');
      const move = e => {
        chosen = clamp(startWidth + (e.clientX - startX) * grow, min, limit());
        show(chosen);
      };
      const end = () => {
        divider.removeEventListener('pointermove', move);
        divider.removeEventListener('pointerup', end);
        divider.removeEventListener('pointercancel', end);
        divider.classList.remove('dragging');
        document.body.classList.remove('column-resizing');
        store(id, chosen);
      };
      divider.addEventListener('pointermove', move);
      divider.addEventListener('pointerup', end);
      divider.addEventListener('pointercancel', end);
    });

    const entry = {
      id,
      // Re-fit the remembered width to the space available without forgetting it.
      refit: () => show(chosen),
      reload: () => { chosen = saved(id); show(chosen); },
    };
    attached.push(entry);
    show(chosen);
    return entry;
  }

  let refitQueued = false;
  addEventListener('resize', () => {
    if (refitQueued) return;
    refitQueued = true;
    requestAnimationFrame(() => { refitQueued = false; attached.forEach(entry => entry.refit()); });
  });
  addEventListener('storage', event => {
    if (event.key === KEY || event.key === null) attached.forEach(entry => entry.reload());
  });

  window.roostaColumns = { attach, KEY };
})();
