const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

test("macOS and menu-bar menus expose restart", () => {
  const main = fs.readFileSync(path.join(__dirname, "..", "src", "main.js"), "utf8");

  assert.equal((main.match(/label: "Restart Roosta"/g) || []).length, 2);
});

test("Roosta menu opens Settings with the standard shortcut", () => {
  const main = fs.readFileSync(path.join(__dirname, "..", "src", "main.js"), "utf8");

  assert.ok(main.includes('{ label: "Settings…", accelerator: "CommandOrControl+,", click: () => showPage("/settings") }'));
});

test("notification clicks open the specific board item or task", () => {
  const main = fs.readFileSync(path.join(__dirname, "..", "src", "main.js"), "utf8");

  assert.ok(main.includes("function openNoticeTarget(notice)"));
  assert.ok(main.includes("onClick: () => openNoticeTarget(notice)"));
  assert.ok(main.includes("if (pendingNavigation)"));
  assert.ok(main.includes("if (!mainWindow.isVisible()) showPage(\"/assistant\")"));
});

test("packaged app uses a stable notification signing identity", () => {
  const packaging = fs.readFileSync(
    path.join(__dirname, "..", "scripts", "package.js"),
    "utf8",
  );

  assert.ok(packaging.includes('"Assistant Local Notifications"'));
  assert.ok(!packaging.includes('"--sign",\n    "-",'));
  assert.ok(packaging.includes('const APP_BUNDLE_ID = "com.m0nkmaster.roosta";'));
  assert.ok(packaging.includes("appBundleId: APP_BUNDLE_ID"));
  assert.ok(packaging.includes('"--identifier",\n    APP_BUNDLE_ID,'));
});

test("workspace setup asks for migration or adoption and treats Cancel as refusal", async () => {
  const vm = require("node:vm");
  const main = fs.readFileSync(path.join(__dirname, "..", "src", "main.js"), "utf8");
  const promptSource = main.slice(main.indexOf("async function confirmWorkspaceSetup("), main.indexOf("async function initialise("));
  assert.ok(main.includes("confirmWorkspace: confirmWorkspaceSetup"));
  let response = 0;
  const dialogs = [];
  let visible = false;
  const window = { show: () => { visible = true; } };
  const confirm = vm.runInNewContext(`${promptSource}; confirmWorkspaceSetup`, {
    mainWindow: window,
    dialog: { showMessageBox: async (parent, options) => {
      assert.equal(parent, window);
      assert.equal(visible, true, "show the startup window before its modal dialog");
      dialogs.push(options);
      return { response };
    } },
  });
  assert.equal(await confirm({ workspaceDir: "/workspace", migrate: true }), true);
  assert.equal(dialogs[0].message, "Migrate this workspace to Roosta?");
  assert.equal(dialogs[0].buttons[0], "Migrate workspace");
  response = 1;
  assert.equal(await confirm({ workspaceDir: "/workspace", migrate: false }), false);
  assert.equal(dialogs[1].message, "Adopt this folder as your Roosta workspace?");
  assert.equal(dialogs[1].buttons[0], "Adopt folder");
  assert.equal(dialogs[1].buttons[dialogs[1].cancelId], "Cancel");
});
