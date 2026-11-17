const { contextBridge, ipcRenderer } = require("electron");

if (globalThis.location.protocol === "file:") {
  contextBridge.exposeInMainWorld("assistantDesktop", {
    retryRunner: () => ipcRenderer.invoke("runner-retry"),
    repairRunner: () => ipcRenderer.invoke("runner-repair"),
    openLogs: () => ipcRenderer.send("logs-open"),
    readLogs: () => ipcRenderer.invoke("logs-read"),
    copyLogs: () => ipcRenderer.invoke("logs-copy"),
    openRunnerStatus: () => ipcRenderer.invoke("runner-status-open"),
    onRecoveryStatus: (callback) => {
      ipcRenderer.on("recovery-status", (_event, value) => callback(value));
    },
  });
}
