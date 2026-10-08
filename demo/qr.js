// Print a plain UTF-8 QR code (no ANSI) and save a PNG. Usage: node qr.js <url> [png]
const QRCode = require("qrcode");
const [url, png] = process.argv.slice(2);
const qr = QRCode.create(url, {errorCorrectionLevel: "M"});
const size = qr.modules.size, data = qr.modules.data, quiet = 2;
const dark = (x, y) => x >= 0 && y >= 0 && x < size && y < size && data[y * size + x] === 1;
let out = "";
for (let y = -quiet; y < size + quiet; y += 2) {
  let line = "";
  for (let x = -quiet; x < size + quiet; x++) {
    const top = dark(x, y), bottom = dark(x, y + 1);
    line += top && bottom ? "█" : top ? "▀" : bottom ? "▄" : " ";
  }
  out += line + "\n";
}
process.stdout.write(out);
if (png) QRCode.toFile(png, url, {width: 480, margin: 2}).then(() => console.error("saved", png));
