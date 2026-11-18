const { execFile } = require("node:child_process");
const { promisify } = require("node:util");

const exec = promisify(execFile);

async function applyVerifiedCommit(checkout, branch, previous) {
  const git = async (...args) => (await exec("git", args, { cwd: checkout, timeout: 15000 })).stdout.trim();
  const target = await git("rev-parse", "--verify", "refs/roosta/verified^{commit}");
  if (target === previous) return null;
  if (await git("branch", "--show-current") !== "main") throw new Error("Live checkout is not on main");
  await git("check-ref-format", "refs/heads/" + branch);
  await git("merge-base", "--is-ancestor", target, "refs/heads/" + branch);
  // Git refuses overlapping edits or diverging history. Never stash a live checkout.
  await git("merge", "--ff-only", "--no-autostash", target);
  return target;
}

module.exports = { applyVerifiedCommit };
