const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const read = file => fs.readFileSync(path.resolve(__dirname, file), "utf8");
const page = read("../../src/roosta/static/settings.html");
function settingsPage(userAgent) {
  const elements = Object.fromEntries([...page.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)]
    .map(([tag, id]) => [id, { hidden: /\bhidden\b/.test(tag), addEventListener() {} }]));
  const script = page.slice(page.lastIndexOf("(() => {"), page.indexOf("</script>", page.lastIndexOf("(() => {")));
  vm.runInNewContext(script, {
    document: { getElementById: id => elements[id] }, navigator: { userAgent },
    fetch: async () => ({ ok: true, json: async () => ({ enabled: true, managed_by: "terminal" }) }),
  });
  return elements;
}

test("Settings shows macOS notification guidance only on Macs", () => {
  for (const userAgent of ["Mac", "Linux"]) {
    const elements = settingsPage(userAgent);
    assert.equal(elements["notifications-os"].hidden, userAgent !== "Mac");
    assert.equal(elements["open-notification-settings"], undefined);
  }
});
