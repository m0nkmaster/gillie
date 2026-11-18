const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { applyVerifiedCommit } = require("../src/local-delivery");

test("delivers the verified revision once, preserving local edits and rejecting other branches", async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "roosta-delivery-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const git = (...args) => execFileSync("git", ["-c", "user.email=test@example.org", "-c", "user.name=Test",
    "-c", "commit.gpgsign=false", ...args], { cwd: dir, encoding: "utf8" }).trim();
  git("init", "-q", "-b", "main");
  const file = path.join(dir, "page");
  fs.writeFileSync(file, "original");
  git("add", "page");
  git("commit", "-qm", "initial");
  git("branch", "other");
  git("switch", "-qc", "worker");
  fs.writeFileSync(file, "verified");
  git("commit", "-qam", "fix");
  const target = git("rev-parse", "HEAD");
  git("update-ref", "refs/roosta/verified", target);
  git("switch", "-q", "main");
  await assert.rejects(applyVerifiedCommit(dir, "other", ""));
  fs.writeFileSync(file, "in progress");
  await assert.rejects(applyVerifiedCommit(dir, "worker", ""));
  assert.equal(fs.readFileSync(file, "utf8"), "in progress");
  git("restore", "page");
  assert.equal(await applyVerifiedCommit(dir, "worker", ""), target);
  assert.equal(await applyVerifiedCommit(dir, "worker", target), null);
  assert.equal(fs.readFileSync(file, "utf8"), "verified");
});
