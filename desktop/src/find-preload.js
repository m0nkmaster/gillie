const { ipcRenderer } = require("electron");

window.addEventListener("DOMContentLoaded", () => {
  const query = document.getElementById("query");
  const previous = document.getElementById("previous");
  const next = document.getElementById("next");
  query.addEventListener("input", () => {
    previous.disabled = next.disabled = !query.value;
    ipcRenderer.send("find-query", query.value);
  });
  query.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.isComposing && query.value) {
      event.preventDefault();
      ipcRenderer.send("find-step", !event.shiftKey);
    }
  });
  window.addEventListener("keydown", event => {
    if (event.key === "Escape" && !event.isComposing) {
      event.preventDefault();
      ipcRenderer.send("find-close");
    }
  });
  previous.addEventListener("click", () => ipcRenderer.send("find-step", false));
  next.addEventListener("click", () => ipcRenderer.send("find-step", true));
  document.getElementById("close").addEventListener("click", () => ipcRenderer.send("find-close"));
  ipcRenderer.on("find-count", (_event, count) => {
    document.getElementById("count").textContent = count;
  });
  ipcRenderer.on("find-focus", () => {
    query.focus();
    query.select();
  });
});
