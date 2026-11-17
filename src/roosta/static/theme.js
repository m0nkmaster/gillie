// Appearance from Settings. Loaded without defer in every page's <head>, after shell.css, so the
// chosen theme is on <html> before first paint. Stored per browser: 'dark' (default) or 'light'.
(() => {
  const KEY = 'roosta-theme';
  function choice() {
    try { return localStorage.getItem(KEY) === 'light' ? 'light' : 'dark'; } catch (e) { return 'dark'; }
  }
  function apply() {
    const theme = choice();
    document.documentElement.setAttribute('data-bs-theme', theme);
    window.dispatchEvent(new CustomEvent('roosta-theme', { detail: theme }));
  }
  apply();
  // Keep other open pages in step.
  window.addEventListener('storage', e => { if (e.key === KEY) apply(); });
  window.roostaTheme = {
    get: choice,
    set(theme) {
      try { theme === 'light' ? localStorage.setItem(KEY, 'light') : localStorage.removeItem(KEY); } catch (e) {}
      apply();
    },
  };
})();
