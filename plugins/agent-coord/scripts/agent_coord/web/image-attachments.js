"use strict";
(() => {
  const MAX_IMAGES = 4, MAX_BYTES = 5 * 1024 * 1024;
  const TYPES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);
  const EXTENSIONS = {png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", webp: "image/webp", gif: "image/gif"};
  const isImageURL = url => typeof url === "string" && /^data:image\/(png|jpeg|webp|gif);base64,[A-Za-z0-9+/]+=*$/.test(url);

  class ChatImageAttachments {
    constructor({document, getThread, canAttach, onChange, onError, readFile}) {
      Object.assign(this, {document, getThread, canAttach, onChange, onError});
      this.drafts = new Map();
      this.readFile = readFile || (file => new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = reader.onabort = () => reject(new Error("Could not read " + file.name + ". Try attaching it again."));
        reader.readAsDataURL(file);
      }));
    }

    items(id = this.getThread()) { return this.drafts.get(id) || []; }
    pending(id = this.getThread()) { return this.items(id).some(item => !item.url); }
    snapshot(id = this.getThread()) { return this.items(id).filter(item => item.url); }
    remove(id, item) {
      const remaining = this.items(id).filter(other => other !== item);
      if (remaining.length) this.drafts.set(id, remaining);
      else this.drafts.delete(id);
      this.onChange();
    }
    sent(id, items) {
      // Preserve images added while a request was in flight, even in another thread.
      const remaining = this.items(id).filter(item => !items.includes(item));
      if (remaining.length) this.drafts.set(id, remaining);
      else this.drafts.delete(id);
      this.onChange();
    }

    async add(files) {
      const id = this.getThread();
      if (!id || !this.canAttach()) return;
      const reads = [], errors = [];
      for (const file of Array.from(files)) {
        const mime = file.type || EXTENSIONS[file.name.split(".").pop().toLowerCase()];
        if (!TYPES.has(mime)) { errors.push(file.name + ": choose a PNG, JPEG, WebP, or GIF image."); continue; }
        if (!file.size || file.size > MAX_BYTES) { errors.push(file.name + ": images must be nonempty and at most 5 MiB."); continue; }
        const items = this.items(id);
        if (items.length >= MAX_IMAGES) { errors.push("Attach at most 4 images per message."); break; }
        // Browsers can name a dragged data-URL image after its entire payload.
        const name = file.name && file.name.length <= 255 ? file.name : "Image." + mime.split("/")[1];
        const item = {name, url: null};
        this.drafts.set(id, [...items, item]);
        reads.push(Promise.resolve().then(() => this.readFile(file)).then(result => {
          const encoded = String(result).split(",")[1] || "";
          const url = "data:" + mime + ";base64," + encoded;
          if (!isImageURL(url)) throw new Error("Could not read " + item.name + ". Try attaching it again.");
          item.url = url;
        }).catch(error => {
          this.remove(id, item);
          this.onError(error);
        }).finally(() => this.onChange()));
      }
      this.onChange();
      if (errors.length) this.onError(new Error([...new Set(errors)].join("\n")));
      await Promise.all(reads);
    }

    render() {
      const area = this.document.getElementById("image-attachments"), items = this.items();
      const id = this.getThread(), enabled = this.canAttach();
      this.document.getElementById("attach-images").disabled = !enabled || items.length >= MAX_IMAGES;
      this.document.getElementById("image-files").disabled = !enabled;
      if (!enabled) this.highlight(false);
      const previous = this.rendered;
      if (previous?.id === id && previous.enabled === enabled && previous.items.length === items.length &&
          previous.items.every(([item, url], index) => item === items[index] && url === item.url)) return;
      this.rendered = {id, enabled, items: items.map(item => [item, item.url])};
      area.hidden = !items.length;
      area.replaceChildren();
      for (const item of items) {
        const card = this.document.createElement("div"); card.className = "image-attachment";
        if (item.url) {
          const image = this.document.createElement("img"); image.src = item.url; image.alt = item.name;
          card.append(image);
        } else {
          const loading = this.document.createElement("span"); loading.textContent = "Reading…"; card.append(loading);
        }
        const caption = this.document.createElement("span"); caption.textContent = item.name; caption.title = item.name;
        const remove = this.document.createElement("button"); remove.type = "button"; remove.textContent = "×";
        remove.setAttribute("aria-label", "Remove " + item.name); remove.disabled = !enabled;
        remove.onclick = () => this.remove(id, item);
        card.append(caption, remove); area.append(card);
      }
    }

    highlight(active) {
      this.document.getElementById("conversation").classList.toggle("image-drag-over", active);
      this.document.getElementById("image-drop-hint").hidden = !active;
    }

    bind() {
      const doc = this.document, zone = doc.getElementById("conversation"), input = doc.getElementById("image-files");
      doc.getElementById("attach-images").onclick = () => input.click();
      input.onchange = () => { const files = Array.from(input.files); input.value = ""; this.add(files); };
      const hasFiles = event => Array.from(event.dataTransfer?.types || []).includes("Files") ||
        Array.from(event.dataTransfer?.items || []).some(item => item.kind === "file");
      const accepts = event => !zone.hidden && zone.contains(event.target) && this.canAttach();
      for (const type of ["dragenter", "dragover"]) doc.addEventListener(type, event => {
        if (!hasFiles(event)) return;
        event.preventDefault(); // A dropped file must never replace the app with a browser file view.
        const accepted = accepts(event);
        event.dataTransfer.dropEffect = accepted ? "copy" : "none";
        this.highlight(accepted);
      });
      doc.addEventListener("dragleave", event => {
        if (!event.relatedTarget || !zone.contains(event.relatedTarget)) this.highlight(false);
      });
      doc.addEventListener("dragend", () => this.highlight(false));
      doc.addEventListener("drop", event => {
        this.highlight(false);
        if (!hasFiles(event) && !event.dataTransfer?.files?.length) return;
        event.preventDefault();
        if (accepts(event)) this.add(event.dataTransfer.files);
      });
    }

    static appendPreviews(document, parent, images) {
      const valid = images.filter(image => isImageURL(image.url));
      if (!valid.length) return;
      const gallery = document.createElement("div"); gallery.className = "message-images";
      for (const [index, item] of valid.entries()) {
        const image = document.createElement("img"); image.src = item.url;
        image.alt = item.name || "Attached image " + (index + 1); image.loading = "lazy";
        gallery.append(image);
      }
      parent.append(gallery);
    }
  }
  ChatImageAttachments.isImageURL = isImageURL;
  globalThis.ChatImageAttachments = ChatImageAttachments;
  if (typeof module !== "undefined") module.exports = ChatImageAttachments;
})();
