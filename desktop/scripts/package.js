const { packager } = require("@electron/packager");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const SIGNING_IDENTITY = process.env.ASSISTANT_CODESIGN_IDENTITY
  || "Assistant Local Notifications";
const APP_BUNDLE_ID = "com.m0nkmaster.roosta";

async function build() {
  const root = path.join(__dirname, "..");
  const backend = path.join(root, "dist", "backend", "roosta-backend");
  if (!fs.existsSync(path.join(backend, "roosta-backend"))) {
    throw new Error("Build the backend first: python scripts/build-backend.py");
  }
  const version = fs.readFileSync(path.join(root, "..", "src", "roosta", "__init__.py"), "utf8")
    .match(/__version__ = "([^"]+)"/)[1];
  const backendVersion = execFileSync(path.join(backend, "roosta-backend"), ["--version"], { encoding: "utf8" }).trim();
  if (backendVersion !== `roosta ${version}`) throw new Error("Backend version is stale; rebuild it first.");
  const applications = await packager({
    dir: root,
    name: "Roosta",
    platform: "darwin",
    arch: process.arch,
    appVersion: version,
    buildVersion: version,
    out: path.join(root, "dist"),
    overwrite: true,
    asar: true,
    icon: path.join(root, "assets", "icon.icns"),
    appBundleId: APP_BUNDLE_ID,
    appCategoryType: "public.app-category.productivity",
    extendInfo: {
      NSAppTransportSecurity: {
        NSAllowsArbitraryLoads: false,
        NSAllowsLocalNetworking: true,
      },
    },
    ignore: [
      /^\/dist(?:\/|$)/,
      /^\/test(?:\/|$)/,
      /^\/scripts(?:\/|$)/,
    ],
  });
  // Packager's extraResource copy turns relative framework links into absolute
  // build-machine paths. Preserve them so the Python bundle stays relocatable.
  fs.cpSync(backend, path.join(applications[0], "Roosta.app", "Contents", "Resources", "roosta-backend"), {
    recursive: true,
    verbatimSymlinks: true,
  });
  // SECURITY-REVIEW: applies the fixed local identity created by setup-signing.js
  // to the fixed packaged app path. No certificate material leaves the Keychain.
  execFileSync("/usr/bin/codesign", [
    ...(process.env.ASSISTANT_CODESIGN_KEYCHAIN ? ["--keychain", process.env.ASSISTANT_CODESIGN_KEYCHAIN] : []),
    "--force",
    "--deep",
    "--sign",
    SIGNING_IDENTITY,
    "--identifier",
    APP_BUNDLE_ID,
    path.join(applications[0], "Roosta.app"),
  ], { stdio: "inherit" });
  execFileSync("/usr/bin/codesign", ["--verify", "--deep", "--strict", path.join(applications[0], "Roosta.app")]);
  console.log(`Built ${applications[0]}`);
}

build().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
