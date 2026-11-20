// The shared header for every Roosta page, shipped or custom. Pages carry an empty
// <header class="workspace-header navbar"> and this script fills it: brand, navigation
// (shipped pages, then custom pages from /api/pages), the Assistant count badge and, top
// right, the version link and the Settings cog. Change the header here, never in a page.
(() => {
  const NAV = [
    ['/assistant', 'Assistant'],
    ['/workspace', 'Workspace'],
    ['/runner', 'Workers'],
    ['/pulse', 'Pulse'],
    ['/office', 'The Office'],
  ];
  // Tabler Icons "settings" (MIT), inline so a cached sprite can never leave it blank.
  const COG = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.325 4.317c.426 -1.756 2.924 -1.756 3.35 0a1.724 1.724 0 0 0 2.573 1.066c1.543 -.94 3.31 .826 2.37 2.37a1.724 1.724 0 0 0 1.065 2.572c1.756 .426 1.756 2.924 0 3.35a1.724 1.724 0 0 0 -1.066 2.573c.94 1.543 -.826 3.31 -2.37 2.37a1.724 1.724 0 0 0 -2.572 1.065c-.426 1.756 -2.924 1.756 -3.35 0a1.724 1.724 0 0 0 -2.573 -1.066c-1.543 .94 -3.31 -.826 -2.37 -2.37a1.724 1.724 0 0 0 -1.065 -2.572c-1.756 -.426 -1.756 -2.924 0 -3.35a1.724 1.724 0 0 0 1.066 -2.573c-.94 -1.543 .826 -3.31 2.37 -2.37c1 .608 2.296 .07 2.572 -1.065z"/><path d="M9 12a3 3 0 1 0 6 0a3 3 0 0 0 -6 0"/></svg>';
  const PAGES_KEY = 'roosta-custom-pages';
  const RETURN_KEY = 'assistant-return';
  const COUNT_KEY = 'assistant-nav-count';

  function isCurrent(href) {
    const path = location.pathname;
    return href.endsWith('/') ? path.startsWith(href) : path === href || path.startsWith(href + '/');
  }
  function link(href, title) {
    const a = document.createElement('a');
    a.className = 'nav-link';
    a.href = href;
    a.title = title;
    a.setAttribute('aria-label', title);
    if (isCurrent(href)) a.setAttribute('aria-current', 'page');
    const span = document.createElement('span');
    span.textContent = title;
    a.appendChild(span);
    return a;
  }
  function cachedPages() {
    try { return JSON.parse(localStorage.getItem(PAGES_KEY)) || []; } catch (e) { return []; }
  }

  // Brand, navigation and the top-right tools. Custom pages paint from the last known list, then refresh.
  function renderHeader(pages) {
    const header = document.querySelector('header.workspace-header');
    if (!header) return;
    if (!header.hasAttribute('aria-label')) header.setAttribute('aria-label', 'Workspace');
    let nav = header.querySelector('nav.workspace-nav');
    if (!nav) {
      const brand = document.createElement('a');
      brand.className = 'workspace-brand navbar-brand';
      brand.href = '/assistant';
      brand.setAttribute('aria-label', 'Roosta home');
      brand.innerHTML = '<img src="/static/favicon.png" alt=""><span>Roosta</span>';
      nav = document.createElement('nav');
      nav.className = 'workspace-nav nav nav-pills';
      nav.setAttribute('aria-label', 'Dashboard');
      header.prepend(brand, nav);
    }
    if (!header.querySelector('.workspace-tools')) {
      const tools = document.createElement('div');
      tools.className = 'workspace-tools';
      const cog = document.createElement('a');
      cog.className = 'workspace-settings';
      cog.href = '/settings';
      cog.title = 'Settings';
      cog.setAttribute('aria-label', 'Settings');
      if (isCurrent('/settings')) cog.setAttribute('aria-current', 'page');
      cog.innerHTML = COG;
      tools.appendChild(cog);
      header.appendChild(tools);
    }
    const custom = pages.map(p => [p.url, p.title]).filter(([url]) => !NAV.some(([h]) => h === url));
    nav.replaceChildren(...NAV.concat(custom).map(([href, title]) => link(href, title)));
  }
  async function refreshPages() {
    try {
      const pages = await (await fetch('/api/pages')).json();
      const fresh = JSON.stringify(pages);
      if (fresh === JSON.stringify(cachedPages())) return;
      try { localStorage.setItem(PAGES_KEY, fresh); } catch (e) {}
      renderHeader(pages);
      setNavCount(cachedCount());
      setAssistantReturn();
    } catch (e) {}
  }

  // Header links to the Assistant (brand and nav) return to the last open item or chat.
  function setAssistantReturn(search) {
    if (search !== undefined) {
      try { search ? localStorage.setItem(RETURN_KEY, search) : localStorage.removeItem(RETURN_KEY); } catch (e) {}
    }
    let saved = '';
    try { saved = localStorage.getItem(RETURN_KEY) || ''; } catch (e) {}
    document.querySelectorAll('.workspace-header a[href^="/assistant"], .workspace-header a[data-assistant-link]')
      .forEach(a => { a.dataset.assistantLink = ''; a.href = '/assistant' + saved; });
  }

  // Assistant badge: how many board items need the user. The Assistant page calls setNavCount on each load.
  function setNavCount(n) {
    try { localStorage.setItem(COUNT_KEY, String(n || 0)); } catch (e) {}
    document.querySelectorAll('nav.workspace-nav a[data-assistant-link]').forEach(a => {
      let b = a.querySelector('.nav-count');
      if (!b) { b = document.createElement('span'); b.className = 'nav-count'; a.appendChild(b); }
      b.hidden = !n;
      b.textContent = n > 99 ? '99+' : String(n);
      a.title = n ? `Assistant · ${n} need you` : 'Assistant';
    });
  }
  function cachedCount() {
    try { return Number(localStorage.getItem(COUNT_KEY)) || 0; } catch (e) { return 0; }
  }
  async function poll() {
    try { setNavCount((await (await fetch('/api/board/count')).json()).items); } catch (e) {}
  }

  // Version link, top right of the header, beside the Settings cog.
  async function showVersion() {
    const tools = document.querySelector('header.workspace-header .workspace-tools');
    if (!tools) return;
    try {
      const { version } = await (await fetch('/api/changelog')).json();
      const a = document.createElement('a');
      a.className = 'workspace-version';
      a.href = '/changelog';
      a.title = 'Changelog';
      a.textContent = 'v' + version;
      if (location.pathname === '/changelog') a.setAttribute('aria-current', 'page');
      tools.prepend(a);
    } catch (e) {}
  }

  async function showUpdateNotice() {
    if (document.hidden || isCurrent('/settings')) return;
    try {
      const response = await fetch('/api/updates');
      if (!response.ok) return;
      const {latest, notes} = await response.json();
      if (!latest) return;
      const key = 'roosta-update-notice-' + latest;
      if (sessionStorage.getItem(key)) return;
      const header = document.querySelector('header.workspace-header');
      if (!header) return;
      const notice = document.createElement('div');
      notice.className = 'alert alert-info m-0 py-1 d-flex align-items-center gap-2 flex-wrap';
      notice.setAttribute('role', 'status');
      const release = document.createElement('a');
      release.href = notes;
      release.textContent = 'Release notes';
      const settings = document.createElement('a');
      settings.href = '/settings#updates';
      settings.textContent = 'Update instructions';
      const close = document.createElement('button');
      close.type = 'button';
      close.className = 'btn btn-ghost-secondary btn-sm';
      close.textContent = 'Dismiss';
      close.onclick = () => notice.remove();
      notice.append('Roosta ' + latest + ' is available.', release, settings, close);
      sessionStorage.setItem(key, '1');
      header.after(notice);
    } catch {} // Background checks stay silent when offline or storage is unavailable.
  }

  function ready() {
    renderHeader(cachedPages());
    setAssistantReturn();
    setNavCount(cachedCount()); // the last known count at once; the fetch corrects it
    showVersion();
    refreshPages();
    showUpdateNotice();
    setInterval(showUpdateNotice, 60000);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready);
  else ready();
  // Keep other open tabs in step.
  window.addEventListener('storage', e => { if (e.key === RETURN_KEY) setAssistantReturn(); });
  window.setNavCount = setNavCount;
  window.setAssistantReturn = setAssistantReturn;
  if (location.pathname !== '/assistant') { poll(); setInterval(poll, 60000); }
})();
