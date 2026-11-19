const path = require("node:path");

// Search the page's WebContents. The controls live in their own local renderer.
const openBars = new WeakMap();

function showFind(window, WebContentsView) {
  if (!window || window.isDestroyed()) return;
  if (openBars.has(window)) {
    openBars.get(window)();
    return;
  }

  const page = window.webContents;
  const view = new WebContentsView({
    webPreferences: {
      preload: path.join(__dirname, "find-preload.js"),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  const bar = view.webContents;
  let ready = false;
  let query = "";
  let requestId;
  function focus() {
    if (!ready) return;
    bar.focus();
    bar.send("find-focus");
  }
  function resize() {
    const width = Math.min(window.getContentSize()[0], 440);
    view.setBounds({ x: window.getContentSize()[0] - width, y: 0, width, height: 48 });
  }
  function close() {
    if (openBars.get(window) !== focus) return;
    openBars.delete(window);
    window.off("resize", resize);
    window.off("hide", close);
    window.off("closed", close);
    page.off("did-navigate", close);
    page.off("before-input-event", escape);
    page.off("found-in-page", result);
    if (!page.isDestroyed()) page.stopFindInPage("clearSelection");
    if (!window.isDestroyed()) window.contentView.removeChildView(view);
    if (!bar.isDestroyed()) bar.close();
  }
  function dismiss() {
    close();
    page.focus();
  }
  function escape(event, input) {
    if (input.type === "keyDown" && input.key === "Escape" && !input.isComposing) {
      event.preventDefault();
      dismiss();
    }
  }
  function result(_event, hit) {
    if (hit.requestId === requestId && hit.finalUpdate) {
      bar.send("find-count", hit.matches ? `${hit.activeMatchOrdinal} of ${hit.matches}` : "No matches");
    }
  }

  openBars.set(window, focus);
  window.contentView.addChildView(view);
  resize();
  window.on("resize", resize);
  window.on("hide", close);
  window.on("closed", close);
  page.on("did-navigate", close);
  page.on("before-input-event", escape);
  page.on("found-in-page", result);
  bar.on("will-navigate", event => event.preventDefault());
  bar.setWindowOpenHandler(() => ({ action: "deny" }));
  bar.ipc.on("find-query", (_event, value) => {
    if (typeof value !== "string") return;
    query = value;
    requestId = undefined;
    bar.send("find-count", "");
    if (query) requestId = page.findInPage(query, { findNext: true });
    else page.stopFindInPage("clearSelection");
  });
  bar.ipc.on("find-step", (_event, forward) => {
    if (query && typeof forward === "boolean") {
      requestId = page.findInPage(query, { findNext: false, forward });
    }
  });
  bar.ipc.on("find-close", dismiss);
  bar.loadFile(path.join(__dirname, "find.html")).then(() => {
    if (openBars.get(window) !== focus) return;
    ready = true;
    if (window.isFocused()) focus();
  }, close);
}

module.exports = { showFind };
