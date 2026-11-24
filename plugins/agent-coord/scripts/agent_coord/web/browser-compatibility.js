// macOS 12 can retain a WebKit version that predates these standard methods.
(() => {
  "use strict";
  if (!Object.hasOwn) Object.defineProperty(Object, "hasOwn", {
    configurable: true, writable: true, value: (object, key) => Object.prototype.hasOwnProperty.call(object, key),
  });
  if (!Array.prototype.at) Object.defineProperty(Array.prototype, "at", {
    configurable: true, writable: true, value(index) {
      const offset = Math.trunc(Number(index)) || 0;
      const position = offset < 0 ? this.length + offset : offset;
      return position < 0 || position >= this.length ? undefined : this[position];
    },
  });
  if (!crypto.randomUUID) Object.defineProperty(crypto, "randomUUID", {
    configurable: true, value() {
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      bytes[6] = (bytes[6] & 15) | 64;
      bytes[8] = (bytes[8] & 63) | 128;
      const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
      return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
    },
  });
})();

// An unopened dialog has no child browsing context. Move its frame out of the
// inert template on first use, preserving the native iframe for later visits.
function workspaceDialogFrame(doc, id) {
  const existing = doc.getElementById(id);
  if (existing) return existing;
  const template = doc.getElementById(id + "-template");
  const frame = template?.content?.firstElementChild;
  if (frame) template.replaceWith(frame);
  return frame;
}
