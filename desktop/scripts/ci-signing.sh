#!/bin/bash
# Prepare an isolated build keychain. Source with --local for a local release build.
set -euo pipefail
[[ "${GITHUB_ACTIONS:-}" == true || "${1:-}" == --local ]] || { echo 'Use --local outside CI' >&2; exit 1; }
SIGNING_DIR=$(mktemp -d)
SIGNING_KEYCHAIN="$SIGNING_DIR/roosta-signing.keychain-db"
SIGNING_PASSWORD=$(/usr/bin/openssl rand -hex 32)
trap 'security delete-keychain "$SIGNING_KEYCHAIN" >/dev/null 2>&1 || true; rm -rf "$SIGNING_DIR"' EXIT
security create-keychain -p "$SIGNING_PASSWORD" "$SIGNING_KEYCHAIN"
security set-keychain-settings -lut 21600 "$SIGNING_KEYCHAIN"
security unlock-keychain -p "$SIGNING_PASSWORD" "$SIGNING_KEYCHAIN"
if [[ "${GITHUB_ACTIONS:-}" == true ]]; then
  security list-keychains -d user -s "$SIGNING_KEYCHAIN" login.keychain-db
  security default-keychain -d user -s "$SIGNING_KEYCHAIN"
fi
if [[ -n "${ROOSTA_SIGNING_P12:-}" ]]; then
  printf '%s' "$ROOSTA_SIGNING_P12" | base64 --decode > "$SIGNING_DIR/identity.p12"
  security import "$SIGNING_DIR/identity.p12" -k "$SIGNING_KEYCHAIN" -P "$ROOSTA_SIGNING_PASSWORD" -T /usr/bin/codesign
  /usr/bin/openssl pkcs12 -in "$SIGNING_DIR/identity.p12" -passin env:ROOSTA_SIGNING_PASSWORD -clcerts -nokeys -out "$SIGNING_DIR/cert.pem"
else
  /usr/bin/openssl req -new -newkey rsa:2048 -x509 -sha256 -nodes -days 3650 \
    -subj '/CN=Assistant Local Notifications/O=Roosta/' \
    -addext 'basicConstraints=critical,CA:TRUE' \
    -addext 'keyUsage=critical,digitalSignature,keyCertSign' \
    -addext 'extendedKeyUsage=codeSigning' \
    -keyout "$SIGNING_DIR/key.pem" -out "$SIGNING_DIR/cert.pem"
  /usr/bin/openssl pkcs12 -export -name 'Assistant Local Notifications' \
    -inkey "$SIGNING_DIR/key.pem" -in "$SIGNING_DIR/cert.pem" \
    -out "$SIGNING_DIR/identity.p12" -passout "pass:$SIGNING_PASSWORD"
  security import "$SIGNING_DIR/identity.p12" -k "$SIGNING_KEYCHAIN" -P "$SIGNING_PASSWORD" -T /usr/bin/codesign
fi
if [[ "${GITHUB_ACTIONS:-}" == true ]]; then
  # Hosted runners have passwordless sudo; avoid a trust-settings GUI prompt.
  sudo security add-trusted-cert -d -r trustRoot -p codeSign -k "$SIGNING_KEYCHAIN" "$SIGNING_DIR/cert.pem"
else
  security add-trusted-cert -r trustRoot -p codeSign -k "$SIGNING_KEYCHAIN" "$SIGNING_DIR/cert.pem"
fi
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$SIGNING_PASSWORD" "$SIGNING_KEYCHAIN" >/dev/null

# Select the exact certificate rather than relying on common-name lookup.
ASSISTANT_CODESIGN_IDENTITY=$(/usr/bin/openssl x509 -in "$SIGNING_DIR/cert.pem" -noout -fingerprint -sha1 | cut -d= -f2 | tr -d ':')
export ASSISTANT_CODESIGN_IDENTITY
security find-identity -v -p codesigning "$SIGNING_KEYCHAIN"
export ASSISTANT_CODESIGN_KEYCHAIN="$SIGNING_KEYCHAIN"
if [[ "${GITHUB_ACTIONS:-}" == true ]]; then
  # Keep the keychain until the workflow's explicit cleanup step.
  trap - EXIT
  printf 'ASSISTANT_CODESIGN_IDENTITY=%s\n' "$ASSISTANT_CODESIGN_IDENTITY" >> "$GITHUB_ENV"
  printf 'ASSISTANT_CODESIGN_KEYCHAIN=%s\n' "$SIGNING_KEYCHAIN" >> "$GITHUB_ENV"
  rm -f "$SIGNING_DIR/key.pem" "$SIGNING_DIR/cert.pem" "$SIGNING_DIR/identity.p12"
fi
