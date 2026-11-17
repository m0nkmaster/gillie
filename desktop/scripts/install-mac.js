const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const root = path.join(__dirname, "..");
const candidates = [
  path.join(root, "dist", `Roosta-darwin-${process.arch}`, "Roosta.app"),
  path.join(root, "dist", "mac-arm64", "Roosta.app"),
  path.join(root, "dist", "mac", "Roosta.app"),
];
const source = candidates.find((candidate) => fs.existsSync(candidate));

if (!source) {
  throw new Error("Built Roosta.app was not found");
}

const target = "/Applications/Roosta.app";
fs.rmSync(target, { recursive: true, force: true });
// SECURITY-REVIEW: copies a fixed locally-built app to the fixed Applications path.
execFileSync("/usr/bin/ditto", [source, target]);

// macOS only delivers banner/sound notifications when a single Launch Services
// registration owns the bundle id. The build output under dist/ shares the id with
// the installed copy; leaving it registered gives the notification daemon two apps to
// choose from and silently downgrades delivery to Notification Centre only. Drop the
// dist copy, then re-register the installed app as the sole owner.
const lsregister =
  "/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/" +
  "LaunchServices.framework/Versions/A/Support/lsregister";
const distDir = path.join(root, "dist");
try {
  execFileSync(lsregister, ["-u", source], { stdio: "ignore" });
} catch {
  // Nothing registered at that path is fine.
}
fs.rmSync(distDir, { recursive: true, force: true });
execFileSync(lsregister, ["-f", target], { stdio: "ignore" });

console.log(`Installed ${target}`);
