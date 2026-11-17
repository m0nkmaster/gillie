const status = document.getElementById("status");
const retry = document.getElementById("retry");
const repair = document.getElementById("repair");
const logs = document.getElementById("logs");

function setBusy(busy) {
  retry.disabled = busy;
  repair.disabled = busy;
  logs.disabled = busy;
}

window.assistantDesktop.onRecoveryStatus((update) => {
  status.textContent = update.message || "The Roosta runner is unavailable.";
  repair.hidden = !update.repairSuggested && repair.hidden;
  setBusy(Boolean(update.busy));
});

retry.addEventListener("click", async () => {
  setBusy(true);
  status.textContent = "Retrying the Roosta runner...";
  const result = await window.assistantDesktop.retryRunner();
  if (!result.ok) setBusy(false);
});

repair.addEventListener("click", async () => {
  repair.hidden = false;
  setBusy(true);
  await window.assistantDesktop.repairRunner();
});

logs.addEventListener("click", () => window.assistantDesktop.openLogs());
