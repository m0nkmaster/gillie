const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const IDENTITY = "Assistant Local Notifications";
const LOGIN_KEYCHAIN = path.join(os.homedir(), "Library", "Keychains", "login.keychain-db");

function run(command, args, options = {}) {
  try {
    return execFileSync(command, args, {
      encoding: "utf8",
      stdio: options.stdio || ["ignore", "pipe", "pipe"],
    });
  } catch {
    throw new Error(`${path.basename(command)} failed while preparing Assistant signing.`);
  }
}

function hasIdentity() {
  try {
    return run("/usr/bin/security", ["find-identity", "-v", "-p", "codesigning"])
      .includes(`"${IDENTITY}"`);
  } catch {
    return false;
  }
}

function createIdentity() {
  if (process.platform !== "darwin") {
    throw new Error("Assistant signing setup is only supported on macOS.");
  }
  if (hasIdentity()) {
    console.log(`Signing identity already exists: ${IDENTITY}`);
    return;
  }

  const temporaryDirectory = fs.mkdtempSync(
    path.join(os.tmpdir(), "assistant-signing-"),
    { mode: 0o700 },
  );
  const key = path.join(temporaryDirectory, "key.pem");
  const certificate = path.join(temporaryDirectory, "certificate.pem");
  const archive = path.join(temporaryDirectory, "identity.p12");
  const password = crypto.randomBytes(32).toString("hex");

  try {
    // SECURITY-REVIEW: creates a local code-signing identity in the user's login
    // Keychain. The random export password exists only in memory during import.
    run("/usr/bin/openssl", [
      "req",
      "-new",
      "-newkey", "rsa:2048",
      "-x509",
      "-sha256",
      "-nodes",
      "-days", "3650",
      "-subj", `/CN=${IDENTITY}/O=Roosta/`,
      "-addext", "basicConstraints=critical,CA:TRUE",
      "-addext", "keyUsage=critical,digitalSignature,keyCertSign",
      "-addext", "extendedKeyUsage=codeSigning",
      "-keyout", key,
      "-out", certificate,
    ]);
    run("/usr/bin/openssl", [
      "pkcs12",
      "-export",
      "-name", IDENTITY,
      "-inkey", key,
      "-in", certificate,
      "-out", archive,
      "-passout", `pass:${password}`,
    ]);
    run("/usr/bin/security", [
      "import", archive,
      "-k", LOGIN_KEYCHAIN,
      "-P", password,
      "-T", "/usr/bin/codesign",
    ], { stdio: "inherit" });
    run("/usr/bin/security", [
      "add-trusted-cert",
      "-r", "trustRoot",
      "-p", "codeSign",
      "-k", LOGIN_KEYCHAIN,
      certificate,
    ], { stdio: "inherit" });
  } finally {
    fs.rmSync(temporaryDirectory, { recursive: true, force: true });
  }

  if (!hasIdentity()) {
    throw new Error("The Assistant signing identity was created but is not trusted for code signing.");
  }
  console.log(`Created signing identity: ${IDENTITY}`);
}

try {
  createIdentity();
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
