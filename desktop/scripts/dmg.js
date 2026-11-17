const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const { execFileSync } = require("node:child_process");

const root = path.join(__dirname, "..");
const version = fs.readFileSync(path.join(root, "..", "src/roosta/__init__.py"), "utf8")
  .match(/__version__ = "([^"]+)"/)[1];
const app = path.join(root, "dist", `Roosta-darwin-${process.arch}`, "Roosta.app");
const output = path.join(root, "dist", `Roosta-${version}-${process.arch}.dmg`);
const staging = fs.mkdtempSync(path.join(os.tmpdir(), "roosta-dmg-"));
try {
  execFileSync("/usr/bin/ditto", [app, path.join(staging, "Roosta.app")]);
  fs.symlinkSync("/Applications", path.join(staging, "Applications"));
  fs.copyFileSync(path.join(root, "INSTALL.txt"), path.join(staging, "READ ME.txt"));
  // macOS runners occasionally hold a disk-image resource briefly after signing/copying.
  for (let attempt = 0; ; attempt++) {
    try {
      execFileSync("/usr/bin/hdiutil", ["create", "-volname", "Roosta", "-srcfolder", staging,
        "-format", "UDZO", "-ov", output], { stdio: ["ignore", "inherit", "pipe"] });
      break;
    } catch (error) {
      if (attempt >= 2 || !String(error.stderr).includes("Resource busy")) throw error;
      console.warn("Disk-image resource busy; retrying in 10 seconds…");
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10000);
    }
  }
  execFileSync("/usr/bin/hdiutil", ["verify", output], { stdio: "inherit" });
  const hash = crypto.createHash("sha256").update(fs.readFileSync(output)).digest("hex");
  fs.writeFileSync(`${output}.sha256`, `${hash}  ${path.basename(output)}\n`);
  console.log(output);
} finally {
  fs.rmSync(staging, { recursive: true, force: true });
}
