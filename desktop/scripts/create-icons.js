const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");
const { execFileSync } = require("node:child_process");

const assetsDir = path.join(__dirname, "..", "assets");
// The small pixel drawing supplies monochrome menu-bar icons.
const WAYFINDER = [
  "........................",
  ".......NNNNNN...........",
  "....NNNMMMMMMNN.........",
  "..NNMMMMMMMMMMMNN.......",
  "..NMMMMMMMMMMMMMMN......",
  "...NNMTMMMMMMMMMMMNN....",
  "....NMTMMMMMMMMMMMMN....",
  "....NTMMMMMMMMNNMMMN....",
  "....NMMMMMMMMNNNNMMMN...",
  "...NMMMMMMMNNNNNNMMMN...",
  "..NMMMMMMNNNNNNNNNMMN...",
  "..NMMMMMNNNNNNNNNNMMN...",
  "..NMMMMNNNEENNEENNNMN...",
  "..NMMMMNNNEENNEENNNMN...",
  "...NMMMNNNNNNNNNNNMN....",
  "....NMMMNNNNNNNNNMMN....",
  ".....NMMMNNNNNNNMMN.....",
  "......NMMMMNNNNMMN......",
  ".......NMMMMMMMMN.......",
  "........NNNNNNNN.....E..",
  "....................EEE.",
  ".....................E..",
  "........................",
  "........................",
];

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const name = Buffer.from(type);
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length);
  const checksum = Buffer.alloc(4);
  checksum.writeUInt32BE(crc32(Buffer.concat([name, data])));
  return Buffer.concat([length, name, data, checksum]);
}

function png(width, height, pixel) {
  const rows = [];
  for (let y = 0; y < height; y += 1) {
    const row = Buffer.alloc(1 + width * 4);
    row[0] = 0;
    for (let x = 0; x < width; x += 1) {
      const colour = pixel(x, y);
      row.set(colour, 1 + x * 4);
    }
    rows.push(row);
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header.set([8, 6, 0, 0, 0], 8);
  return Buffer.concat([
    Buffer.from("89504e470d0a1a0a", "hex"),
    chunk("IHDR", header),
    chunk("IDAT", zlib.deflateSync(Buffer.concat(rows), { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

function trayIcon(size) {
  return png(size, size, (x, y) => {
    const key = WAYFINDER[Math.floor(y * 24 / size)][Math.floor(x * 24 / size)];
    return [0, 0, 0, key !== "." && key !== "E" ? 255 : 0];
  });
}

function writeIcons() {
  fs.copyFileSync(
    path.join(__dirname, "..", "..", "assets", "roosta-pixel.png"),
    path.join(__dirname, "..", "..", "src", "roosta", "static", "favicon.png"),
  );
  fs.mkdirSync(assetsDir, { recursive: true });
  fs.writeFileSync(path.join(assetsDir, "trayTemplate.png"), trayIcon(18));
  fs.writeFileSync(path.join(assetsDir, "trayTemplate@2x.png"), trayIcon(36));

  writeAppIcons(assetsDir);
}

function writeAppIcons(destination) {
  const iconsetDir = path.join(destination, "icon.iconset");
  fs.rmSync(iconsetDir, { recursive: true, force: true });
  fs.mkdirSync(iconsetDir);
  const source = path.join(__dirname, "..", "..", "assets", "roosta-app.png");
  for (const [name, size] of [
    ["icon_16x16", 16], ["icon_16x16@2x", 32],
    ["icon_32x32", 32], ["icon_32x32@2x", 64],
    ["icon_128x128", 128], ["icon_128x128@2x", 256],
    ["icon_256x256", 256], ["icon_256x256@2x", 512],
    ["icon_512x512", 512], ["icon_512x512@2x", 1024],
  ]) {
    // Resize the approved image directly; never redraw or upscale the artwork.
    execFileSync("/usr/bin/sips", [
      "-z", String(size), String(size), source, "--out", path.join(iconsetDir, `${name}.png`),
    ], { stdio: "pipe" });
  }
  fs.copyFileSync(path.join(iconsetDir, "icon_512x512@2x.png"), path.join(destination, "icon.png"));
  // SECURITY-REVIEW: invokes the fixed macOS icon compiler with fixed generated paths.
  execFileSync("/usr/bin/iconutil", [
    "-c",
    "icns",
    iconsetDir,
    "-o",
    path.join(destination, "icon.icns"),
  ]);
}

if (require.main === module) writeIcons();

module.exports = {
  WAYFINDER,
  trayIcon,
  writeAppIcons,
  writeIcons,
};
