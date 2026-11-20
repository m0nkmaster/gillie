const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const read = file => fs.readFileSync(path.resolve(__dirname, file), "utf8");
const main = read("../src/main.js");
const preload = read("../src/preload.js");
const page = read("../../src/roosta/static/settings.html");
const origin = "http://127.0.0.1:7667";
const target = "x-apple.systempreferences:com.apple.Notifications-Settings.extension";

function settingsPage(window, userAgent) {
  const elements = Object.fromEntries([...page.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)]
    .map(([tag, id]) => [id, { hidden: /\bhidden\b/.test(tag), addEventListener() {} }]));
  const script = page.slice(page.lastIndexOf("(() => {"), page.indexOf("</script>", page.lastIndexOf("(() => {")));
  vm.runInNewContext(script, {
    window, document: { getElementById: id => elements[id] }, navigator: { userAgent },
    fetch: async () => ({ ok: true, json: async () => ({ enabled: true, managed_by: "terminal" }) }),
  });
  return elements;
}

test("Settings button invokes one fixed native action and reports an OS failure", async () => {
  const frame = { url: origin + "/settings#notifications" };
  const sender = { mainFrame: frame };
  const event = { sender, senderFrame: frame };
  const handlers = {};
  const opened = [];
  const process = { platform: "darwin" };
  const shell = { openExternal: async url => { opened.push(url); } };
  vm.runInNewContext(
    main.slice(main.indexOf("function allowedDashboardUrl"), main.indexOf("function allowedLocalFile")) +
    main.slice(main.indexOf("function registerIpc"), main.indexOf("async function confirmWorkspaceSetup")) +
    "\nregisterIpc();", {
      URL, BASE_URL: origin, mainWindow: { webContents: sender }, process, shell,
      ipcMain: { handle: (key, fn) => { handlers[key] = fn; }, on() {} },
    });
  const window = {};
  vm.runInNewContext(preload, {
    location: new URL(frame.url), process,
    require: () => ({
      contextBridge: { exposeInMainWorld: (key, api) => { window[key] = api; } },
      ipcRenderer: { invoke: (key, ...args) => handlers[key](event, ...args) },
    }),
  });
  assert.deepEqual(Object.keys(window.assistantDesktop), ["openNotificationSettings"]);
  const elements = settingsPage(window, "Mac");
  const button = elements["open-notification-settings"];
  assert.equal(button.hidden, false);
  await button.onclick();
  assert.deepEqual(opened, [target]);
  shell.openExternal = async () => { throw new Error("Launch Services failure"); };
  await button.onclick();
  assert.match(elements["notifications-message"].textContent, /Could not open System Settings.*Notifications/);

  const open = handlers["notification-settings-open"];
  for (const badEvent of [{ ...event, sender: {} }, { ...event, senderFrame: { ...frame } }]) {
    assert.throws(() => open(badEvent), /rejected/);
  }
  for (const url of ["https://example.org/settings", origin + "/assistant"]) {
    frame.url = url;
    assert.throws(() => open(event), /rejected/);
  }
  frame.url = origin + "/settings";
  process.platform = "linux";
  assert.throws(() => open(event), /rejected/);
});

test("browser Settings shows manual guidance on Macs and hides the native action", () => {
  for (const userAgent of ["Mac", "Linux"]) {
    const elements = settingsPage({}, userAgent);
    assert.equal(elements["notifications-os"].hidden, userAgent !== "Mac");
    assert.equal(elements["open-notification-settings"].hidden, true);
  }
});
