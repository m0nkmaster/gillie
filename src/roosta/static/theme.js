// Appearance from Settings. Loaded without defer in every page's <head>, after shell.css, so the
// chosen scheme and its light/dark mode are on <html> before first paint. Stored per browser.
(() => {
  const KEY = 'roosta-theme';
  const SCHEMES = ['purple', 'slate', 'midnight', 'forest', 'charcoal', 'light'];
  let current;
  function choice() {
    try {
      const saved = localStorage.getItem(KEY);
      if (saved === 'dark') return 'slate';
      return SCHEMES.includes(saved) ? saved : 'purple';
    } catch (e) { return 'purple'; }
  }
  function apply(scheme = choice()) {
    current = scheme;
    const theme = scheme === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-bs-theme', theme);
    document.documentElement.setAttribute('data-roosta-scheme', scheme);
    window.dispatchEvent(new CustomEvent('roosta-theme', { detail: theme }));
  }
  apply();
  // Keep other open pages in step.
  window.addEventListener('storage', e => { if (e.key === KEY) apply(); });
  window.roostaTheme = {
    get: () => current === 'light' ? 'light' : 'dark',
    scheme: () => current,
    set(scheme) {
      if (scheme === 'dark') scheme = 'slate';
      if (!SCHEMES.includes(scheme)) return;
      try { localStorage.setItem(KEY, scheme); } catch (e) {}
      apply(scheme);
    },
  };
})();
