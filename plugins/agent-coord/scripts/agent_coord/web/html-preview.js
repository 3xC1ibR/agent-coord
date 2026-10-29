/* Local HTML links become private previews in browsers and on phones. */
(function (root) {
  "use strict";
  function target(raw, thread) {
    if (!thread || !raw || /^(?:[?#]|\/\/)/.test(raw)) return null;
    const path = raw.split(/[?#]/, 1)[0].replace(/:\d+(?::\d+)?$/, "");
    let decoded;
    try { decoded = decodeURIComponent(path); } catch (_) { return null; }
    if (!/\.html?$/i.test(decoded) || /^[a-z][a-z\d+.-]*:/i.test(decoded) || /[\u0000-\u001f\u007f\\]/.test(decoded) || /^\/(?:api\/|html-preview(?:\/|$))/.test(decoded)) return null;
    const hashAt = raw.indexOf("#"), queryAt = raw.indexOf("?");
    const search = queryAt >= 0 && (hashAt < 0 || queryAt < hashAt) ? raw.slice(queryAt, hashAt < 0 ? undefined : hashAt) : "";
    return "/html-preview?" + new URLSearchParams({thread, path: decoded, ...(search ? {search} : {})}) + (hashAt < 0 ? "" : raw.slice(hashAt));
  }
  function bind(doc, selected) {
    const dialog = doc.getElementById("html-preview-dialog"), frame = doc.getElementById("html-preview-window");
    if (dialog && frame) {
      doc.defaultView.addEventListener("message", event => {
        if (event.origin === doc.defaultView.location.origin && event.source === frame.contentWindow
            && event.data?.type === "ribbon-field-close-html-preview") dialog.close();
      });
      dialog.addEventListener("close", () => frame.removeAttribute("src"));
    }
    doc.addEventListener("click", event => {
      if (event.defaultPrevented || event.button !== 0) return;
      const link = event.target.closest?.(".markdown a[href]");
      const raw = link?.dataset?.htmlPreviewPath || link?.getAttribute("href");
      const url = target(raw, selected());
      if (!url) return;
      // Set the actual link so modifier clicks still open a working preview.
      if (link.dataset) link.dataset.htmlPreviewPath = raw;
      link.href = url;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      if (dialog && frame) {
        const embedded = new URL(url, doc.defaultView.location.origin);
        embedded.searchParams.set("embedded", "1");
        frame.src = embedded.pathname + embedded.search + embedded.hash;
        dialog.showModal();
      } else doc.defaultView.location.assign(url);
    });
  }
  async function show(doc, win) {
    const params = new URLSearchParams(win.location.search), thread = params.get("thread"), path = params.get("path");
    const status = doc.getElementById("preview-status"), frame = doc.getElementById("preview-frame");
    const title = doc.getElementById("preview-title"), retry = doc.getElementById("preview-retry");
    const back = doc.getElementById("preview-back");
    back.onclick = () => {
      if (win.parent && win.parent !== win) win.parent.postMessage({type: "ribbon-field-close-html-preview"}, win.location.origin);
      else win.location.assign("/#" + encodeURIComponent(thread || ""));
    };
    title.textContent = path?.split("/").pop() || "HTML preview";
    doc.title = title.textContent + " — Ribbon Field";
    retry.onclick = () => show(doc, win);
    frame.hidden = true; frame.removeAttribute("src");
    status.hidden = false; status.textContent = "Loading preview…"; status.setAttribute("role", "status"); retry.hidden = true;
    try {
      if (!thread || !path) throw new Error("Open an HTML link from a conversation to preview it.");
      const response = await win.fetch("/api/browser/html-preview?" + new URLSearchParams({thread, path}), {cache: "no-store"});
      if (response.status === 401) throw new Error("Pair this device again to view files from your Mac.");
      if (response.status === 404) throw new Error("HTML previews need an updated Ribbon Field backend on your Mac. The update may still be waiting for running work to finish.");
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Could not open this HTML file.");
      title.textContent = result.name;
      frame.title = result.name;
      // HTTP navigation works in the iOS companion, which intentionally rejects
      // about:srcdoc. The content response also enforces the sandbox via CSP.
      frame.setAttribute("sandbox", "allow-scripts");
      const search = params.get("search") || "";
      frame.src = "/api/browser/html-preview-content/" + encodeURIComponent(thread) + "/" + encodeURIComponent(path)
        + (search.startsWith("?") ? search : "") + (win.location.hash || "");
      frame.hidden = false; status.hidden = true;
    } catch (error) {
      status.textContent = error instanceof TypeError ? "Could not reach your Mac. Check your connection and try again." : error.message;
      status.setAttribute("role", "alert"); retry.hidden = false;
    }
  }
  const api = {target, bind, show};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else {
    root.HTMLPreview = api;
    if (root.document.getElementById("preview-frame")) show(root.document, root);
  }
})(globalThis);
