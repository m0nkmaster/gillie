const assert = require("node:assert/strict");
const test = require("node:test");
const { buildRunnerEnv, killPortListeners, restartDelay } = require("../src/runner-supervisor");

test("runner environment includes GUI-missing tool paths", () => {
  const env = buildRunnerEnv({ PATH: "/custom/bin", SECRET_VALUE: "preserved" });

  assert.equal(env.PYTHONUNBUFFERED, "1");
  assert.equal(env.ASSISTANT_DESKTOP, "1");
  assert.ok(env.PATH.includes("/.local/bin"));
  assert.ok(env.PATH.includes("/opt/homebrew/bin"));
  assert.ok(env.PATH.includes("/custom/bin"));
  assert.equal(env.SECRET_VALUE, "preserved");
});

test("runner retries twice before stopping the crash loop", () => {
  assert.equal(restartDelay(0), 1000);
  assert.equal(restartDelay(1), 3000);
  assert.equal(restartDelay(2), null);
});

test("killPortListeners stops listeners reported by lsof", async () => {
  const calls = [];
  const exec = async (command, args) => {
    calls.push([command, args]);
    return { stdout: "4242\n" };
  };
  const originalKill = process.kill.bind(process);
  const killed = [];
  process.kill = (pid, signal) => {
    killed.push([pid, signal]);
  };
  try {
    const count = await killPortListeners("7667", exec);
    assert.equal(count, 1);
    assert.deepEqual(calls[0], ["lsof", ["-nP", "-iTCP:7667", "-sTCP:LISTEN", "-t"]]);
    assert.deepEqual(killed, [[4242, "SIGTERM"], [4242, 0], [4242, "SIGKILL"]]);
  } finally {
    process.kill = originalKill;
  }
});

test("the runner launches the installed roosta command, preferring the uv tool path", () => {
  const { findRoosta } = require("../src/runner-supervisor");
  const present = new Set(["/home/x/.local/bin/roosta", "/opt/homebrew/bin/roosta"]);
  assert.equal(findRoosta("/home/x", (p) => present.has(p)), "/home/x/.local/bin/roosta");
  assert.equal(findRoosta("/home/y", (p) => p === "/usr/local/bin/roosta"), "/usr/local/bin/roosta");
  assert.equal(findRoosta("/home/z", () => false), null);
});

test("bundled first launch initialises without installing external tools", async () => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const { RunnerSupervisor } = require("../src/runner-supervisor");
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "roosta-test-"));
  try {
    const workspace = path.join(directory, "new workspace");
    const runner = new RunnerSupervisor({ workspaceDir: workspace, logFile: path.join(directory, "log"),
      roosta: "/Applications/Roosta.app/Contents/Resources/roosta-backend/roosta-backend", bundled: true });
    const calls = [];
    runner._run = async (...args) => calls.push(args.slice(0, 2));
    await runner.prepareWorkspace();
    assert.deepEqual(calls, [[runner.roosta, ["init", "--skip-checks", "--no-git", workspace]]]);
    fs.mkdirSync(path.join(workspace, ".roosta"), { recursive: true });
    await runner.repair();
    assert.equal(calls.length, 1, "repair must not install or overwrite an existing workspace");
    runner.bundled = false;
    fs.rmSync(workspace, { recursive: true });
    await runner.prepareWorkspace();
    assert.equal(calls.length, 1, "development retains explicit CLI setup");
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
