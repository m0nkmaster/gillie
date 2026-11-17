const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const test = require("node:test");
const zlib = require("node:zlib");
const { execFileSync } = require("node:child_process");

const {
  WAYFINDER,
  trayIcon,
  writeAppIcons,
} = require("../scripts/create-icons");

function readPixels(image) {
  const width = image.readUInt32BE(16);
  const height = image.readUInt32BE(20);
  const compressed = [];
  for (let offset = 8; offset < image.length;) {
    const length = image.readUInt32BE(offset);
    if (image.toString("ascii", offset + 4, offset + 8) === "IDAT") {
      compressed.push(image.subarray(offset + 8, offset + 8 + length));
    }
    offset += length + 12;
  }
  const rows = zlib.inflateSync(Buffer.concat(compressed));
  const pixels = [];
  assert.equal(rows.length, height * (width * 4 + 1));
  for (let y = 0; y < height; y += 1) {
    const start = y * (width * 4 + 1);
    assert.equal(rows[start], 0);
    for (let x = 0; x < width; x += 1) {
      pixels.push([...rows.subarray(start + 1 + x * 4, start + 5 + x * 4)]);
    }
  }
  return { width, height, pixels };
}

test("menu-bar drawing has one consistent pixel grid", () => {
  assert.equal(WAYFINDER.length, 24);
  for (const row of WAYFINDER) {
    assert.equal(row.length, 24);
    for (const key of row) assert.ok(".NTME".includes(key));
  }
});

test("high-resolution Roosta artwork produces every macOS size and a compilable icon", {
  skip: process.platform !== "darwin",
}, () => {
  const assets = fs.mkdtempSync(path.join(os.tmpdir(), "roosta-icon-test-"));
  try {
    const iconset = path.join(assets, "icon.iconset");
    writeAppIcons(assets);
    assert.equal(fs.readdirSync(iconset).length, 10);
    for (const name of fs.readdirSync(iconset)) {
      const [, base, retina] = name.match(/^icon_(\d+)x\d+(@2x)?\.png$/);
      const size = Number(base) * (retina ? 2 : 1);
      const png = fs.readFileSync(path.join(iconset, name));
      assert.equal(png.readUInt32BE(16), size);
      assert.equal(png.readUInt32BE(20), size);
      assert.equal(png[25], 6, "RGBA preserves transparent icon corners");
    }
    assert.deepEqual(fs.readFileSync(path.join(assets, "icon.png")),
      fs.readFileSync(path.join(iconset, "icon_512x512@2x.png")));
    const expected = path.join(assets, "expected.png");
    execFileSync("/usr/bin/sips", [
      "-z", "1024", "1024",
      path.join(__dirname, "..", "..", "assets", "roosta-app.png"),
      "--out", expected,
    ], { stdio: "pipe" });
    assert.deepEqual(fs.readFileSync(path.join(assets, "icon.png")), fs.readFileSync(expected));
    const icns = path.join(assets, "icon.icns");
    assert.equal(fs.readFileSync(icns).toString("ascii", 0, 4), "icns");
  } finally {
    fs.rmSync(assets, { recursive: true, force: true });
  }
});

test("checked-in favicon uses the low-resolution Roosta artwork", () => {
  const root = path.join(__dirname, "..", "..");
  assert.deepEqual(fs.readFileSync(path.join(root, "src", "roosta", "static", "favicon.png")),
    fs.readFileSync(path.join(root, "assets", "roosta-pixel.png")));
});

test("menu-bar icons are monochrome templates with transparent eye cutouts", () => {
  for (const size of [18, 36]) {
    const { pixels } = readPixels(trayIcon(size));
    assert.ok(pixels.some(pixel => pixel[3] === 255));
    assert.ok(pixels.some(pixel => pixel[3] === 0));
    for (const pixel of pixels) assert.deepEqual(pixel.slice(0, 3), [0, 0, 0]);
    assert.equal(pixels[Math.ceil(12 * size / 24) * size + Math.ceil(10 * size / 24)][3], 0);
  }
});

test("desktop package uses the generated Roosta icon", () => {
  const packaging = fs.readFileSync(
    path.join(__dirname, "..", "scripts", "package.js"),
    "utf8",
  );
  assert.match(
    packaging,
    /icon: path\.join\(root, "assets", "icon\.icns"\)/,
  );
});
