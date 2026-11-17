const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

function build(failures, message) {
  let creates = 0, waits = 0, cleaned = false;
  const fakeFS = {
    readFileSync: name => name.endsWith('__init__.py') ? '__version__ = "0.4.1"' : Buffer.from('dmg'),
    mkdtempSync: () => '/tmp/staged', symlinkSync() {}, copyFileSync() {}, writeFileSync() {},
    rmSync() { cleaned = true; },
  };
  const execute = (file, args) => {
    if (file.endsWith('hdiutil') && args[0] === 'create' && ++creates <= failures) {
      const error = new Error(message); error.stderr = message; throw error;
    }
  };
  let error;
  try {
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../scripts/dmg.js'), 'utf8'), {
      require: name => name === 'node:fs' ? fakeFS : name === 'node:child_process' ? {execFileSync: execute} : require(name),
      __dirname, process: {arch:'arm64'}, console: {log() {}, warn() {}},
      Atomics: {wait() { waits++; }}, Int32Array, SharedArrayBuffer,
    });
  } catch (caught) { error = caught; }
  return {creates, waits, cleaned, error};
}

test('DMG creation retries transient resource contention and still cleans staging', () => {
  assert.deepEqual(build(2, 'hdiutil: create failed - Resource busy'), {creates:3, waits:2, cleaned:true, error:undefined});
});

test('DMG retries are bounded and unrelated failures fail immediately', () => {
  const busy = build(5, 'Resource busy');
  assert.equal(busy.creates, 3); assert.equal(busy.waits, 2); assert.ok(busy.error); assert.ok(busy.cleaned);
  const invalid = build(5, 'Permission denied');
  assert.equal(invalid.creates, 1); assert.equal(invalid.waits, 0); assert.ok(invalid.error); assert.ok(invalid.cleaned);
});
