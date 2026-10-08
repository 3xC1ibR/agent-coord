/* Loaded only by the native wrapper, before the existing web UI. */
(() => {
  "use strict";
  const native = window.webkit?.messageHandlers?.desktop;
  if (!native || window.top !== window) return;
  const send = body => native.postMessage(body);
  const preferences = __AGENT_COORD_PREFERENCES__;
  const savedKey = key => key.startsWith("agent-coord.") || key === "agent-coord-group-by";
  // The backend uses a fresh private port on every launch. Preserve UI choices
  // in macOS preferences instead of tying them to that temporary HTTP origin.
  try {
    const setItem = Storage.prototype.setItem, removeItem = Storage.prototype.removeItem;
    for (const [key, value] of Object.entries(preferences)) {
      if (savedKey(key) && key !== "agent-coord.notifications.focus") setItem.call(localStorage, key, value);
    }
    Storage.prototype.setItem = function (key, value) {
      setItem.call(this, key, value);
      if (this === localStorage && savedKey(String(key)) && key !== "agent-coord.notifications.focus") {
        send({action: "preference", key: String(key), value: String(value)}).catch(() => {});
      }
    };
    Storage.prototype.removeItem = function (key) {
      removeItem.call(this, key);
      if (this === localStorage && savedKey(String(key))) send({action: "preference", key: String(key), value: null}).catch(() => {});
    };
  } catch { /* Storage restrictions must not prevent the UI from loading. */ }

  const byID = id => document.getElementById(id);
  const chatDocument = () => window.agentCoordPanes?.active ? window.agentCoordPanes.focusedRecord?.frame?.contentDocument : document;
  const chatByID = id => chatDocument()?.getElementById(id);
  const openDialog = () => document.querySelector("dialog[open]") || chatDocument()?.querySelector("dialog[open]");
  const visible = element => !!element && !element.hidden && !element.closest("[hidden]");
  const forwardedDrops = new WeakSet();
  const imageTypes = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);
  const reportError = error => {
    const box = byID("error"), text = box?.querySelector("span");
    if (box && text) { text.textContent = error.message || String(error); box.hidden = false; }
  };
  function destination() {
    const dialog = openDialog();
    if (dialog) return null;
    const field = chatByID("message"), thread = window.agentCoordPanes?.selected || location.hash;
    if (!visible(chatByID("conversation")) || !visible(chatByID("composer")) || !field || field.disabled || !thread) return null;
    return {mode: "message", thread, field, value: field.value,
      start: field.selectionStart, end: field.selectionEnd};
  }
  function insertReferences(references, target) {
    if (!references.length) return false; // A cancelled file panel is silent.
    const now = destination();
    if (!target || !now || target.mode !== now.mode ||
        (target.mode === "message" && (target.thread !== now.thread || target.field !== now.field))) {
      throw new Error("The destination changed. Drop or choose the files again in the intended conversation.");
    }
    if (references.length > 32 || references.some(item => typeof item.path !== "string" ||
        !item.path.startsWith("/") || item.path.includes("\0"))) throw new Error("Choose up to 32 local files or folders.");
    const field = target.field;
    // Do not overwrite text typed while the native reply was in flight.
    const start = field.value === target.value ? target.start : field.selectionStart;
    const end = field.value === target.value ? target.end : field.selectionEnd;
    const paths = [...new Set(references.map(item => item.path))].map(path => JSON.stringify(path)).join("\n");
    const before = field.value.slice(0, start), after = field.value.slice(end);
    const text = (before && !/\s$/.test(before) ? "\n" : "") + paths + (after && !/^\s/.test(after) ? "\n" : "");
    if (field.maxLength > 0 && before.length + text.length + after.length > field.maxLength) {
      throw new Error("These paths would exceed the message length limit.");
    }
    field.focus();
    field.setSelectionRange(start, end);
    // WebKit's editing command preserves native Undo; setRangeText is a fallback.
    if (!(field.ownerDocument || document).execCommand?.("insertText", false, text)) field.setRangeText(text, start, end, "end");
    field.dispatchEvent(new Event("input", {bubbles: true}));
    return true;
  }
  let palette;
  function sessionOrder() {
    const panes = window.agentCoordPanes;
    // Include stacked tabs in their displayed pane order, not just the active tabs.
    if (panes?.active) return panes.layout?.groups.flat() || [];
    return [...new Set(Array.from(byID("sessions")?.querySelectorAll("button.session[data-thread]") || [])
      .map(button => button.dataset.thread))];
  }
  function cycleSession(direction) {
    const ids = sessionOrder();
    if (!ids.length) return false;
    const panes = window.agentCoordPanes;
    const selected = panes?.active ? panes.selected :
      byID("sessions")?.querySelector('button.session[aria-current="true"]')?.dataset.thread;
    const index = ids.indexOf(selected);
    const id = ids[index < 0 ? (direction > 0 ? 0 : ids.length - 1) : (index + direction + ids.length) % ids.length];
    if (panes?.active) panes.focus(id);
    else Promise.resolve(window.select(id)).catch(reportError);
    return true;
  }
  function commands() {
    const result = [];
    const dialog = openDialog();
    if (palette && (!dialog || dialog === palette.dialog)) result.push("commandPalette");
    if (destination()) result.push("insertFiles");
    if (dialog || !byID("home")) return result;
    result.push("workspace", "findThread", "newSession", "toggleSidebar", "toggleFiles");
    if (sessionOrder().length) result.push("nextSession", "previousSession");
    if (byID("roll-up-start")) result.push("rollUp");
    if (byID("tile-threads") && window.agentCoordPanes) result.push("tileThreads");
    if (visible(chatByID("conversation"))) result.push("expandConversation");
    if (destination()?.mode === "message") {
      result.push("focusMessage");
      if (!chatByID("send")?.disabled) result.push("sendMessage");
    }
    return result;
  }
  function command(name) {
    if (!commands().includes(name)) return false;
    switch (name) {
      case "commandPalette": return palette.toggle();
      case "workspace": byID("home").click(); break;
      case "findThread": byID("home").click(); byID("search").focus(); byID("search").select(); break;
      case "newSession": byID("new-session").click(); break;
      case "nextSession": return cycleSession(1);
      case "previousSession": return cycleSession(-1);
      case "rollUp": byID("roll-up-start").click(); break;
      case "focusMessage": chatByID("message").focus(); break;
      case "toggleSidebar": byID("menu-toggle").click(); break;
      case "toggleFiles": send({action: "toggleFiles"}).catch(reportError); break;
      case "tileThreads": return window.agentCoordPanes.toggleCurrent();
      case "expandConversation":
        if (window.agentCoordPanes?.active) window.agentCoordPanes.maximize();
        else byID("expand-chat").click();
        break;
      case "sendMessage": chatByID("composer").requestSubmit(); break;
      case "insertFiles": {
        const target = destination();
        send({action: "chooseFiles", workspace: target.mode === "workspace"})
          .then(files => insertReferences(files, target)).catch(reportError);
        break;
      }
    }
    return true;
  }
  window.agentCoordDesktop = Object.freeze({command, bindPane,
    insertPaths: (paths, expectedThread) => {
      try {
        const target = destination();
        if (expectedThread !== undefined && target?.thread !== expectedThread)
          throw new Error("The conversation changed. Insert the path again in the intended conversation.");
        return insertReferences(paths.map(path => ({path})), target);
      }
      catch (error) { reportError(error); return false; }
    },
    windowId: "__AGENT_COORD_WINDOW_ID__",
    navigationReady: () => send({action: "navigationReady"}).catch(reportError),
  });

  const hasFiles = event => Array.from(event.dataTransfer?.types || []).includes("Files");
  const clearDropHint = () => {
    chatByID("conversation")?.classList.remove("image-drag-over");
    if (chatByID("image-drop-hint")) chatByID("image-drop-hint").hidden = true;
  };
  function handleDrop(event) {
    if (forwardedDrops.has(event) || !hasFiles(event)) return;
    if (event.target?.ownerDocument && event.target.ownerDocument !== document) window.agentCoordPanes?.focused(event.target.ownerDocument.defaultView);
    const files = Array.from(event.dataTransfer.files || []), target = destination();
    const directories = new Set(Array.from(event.dataTransfer.items || [])
      .map(item => item.webkitGetAsEntry?.()).filter(entry => entry?.isDirectory).map(entry => entry.name));
    const images = files.filter(file => !directories.has(file.name) &&
      (imageTypes.has(file.type) || (!file.type && /\.(png|jpe?g|webp|gif)$/i.test(file.name))));
    const references = files.filter(file => !images.includes(file));
    // Leave ordinary image drops on the existing attachment path.
    if (target?.mode === "message" && images.length && !references.length) return;
    event.preventDefault(); event.stopImmediatePropagation(); clearDropHint();
    if (!target) { reportError(new Error("Open an editable conversation to drop files.")); return; }
    const names = references.map(file => file.name);
    if (!names.length || names.length > 32) { reportError(new Error("Drop up to 32 local files or folders from Finder.")); return; }
    send({action: "dropFiles", names}).then(paths => {
      if (!insertReferences(paths, target) || !images.length || target.mode !== "message") return;
      const transfer = new DataTransfer();
      for (const image of images) transfer.items.add(image);
      const drop = new DragEvent("drop", {bubbles: true, cancelable: true, dataTransfer: transfer});
      forwardedDrops.add(drop);
      target.field.ownerDocument?.getElementById("conversation")?.dispatchEvent(drop) ?? byID("conversation").dispatchEvent(drop);
    }).catch(reportError);
  }
  const boundPanes = new WeakSet();
  function bindPane(doc) {
    if (boundPanes.has(doc)) return;
    boundPanes.add(doc); doc.addEventListener("drop", handleDrop, true);
    const observer = new MutationObserver(() => window.dispatchEvent(new Event("agent-coord-pane-state")));
    observer.observe(doc.body, {subtree: true, childList: true, characterData: true,
      attributes: true, attributeFilter: ["open", "disabled", "hidden"]});
    doc.defaultView?.addEventListener("pagehide", () => observer.disconnect(), {once: true});
  }
  document.addEventListener("drop", handleDrop, true);

  function openWindow(url) { send({action: "newWindow", url}).catch(reportError); }
  // Modifier-click works on thread buttons as well as regular internal links.
  document.addEventListener("click", event => {
    if (!event.metaKey || event.altKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    const thread = event.target.closest("button[data-thread]");
    if (thread && !thread.dataset.action) {
      event.preventDefault(); event.stopImmediatePropagation();
      openWindow(location.pathname + location.search + "#" + encodeURIComponent(thread.dataset.thread));
    }
  }, true);

  function ready() {
    palette = new DesktopCommandPalette({document, onError: reportError,
      actions: () => {
        const available = commands();
        const actions = [
          ["newSession", "New session", "⌘N", "create chat"],
          ["nextSession", "Next session in current view", "⌃Tab", "switch cycle thread"],
          ["previousSession", "Previous session in current view", "⌃⇧Tab", "switch cycle thread"],
          ["workspace", "Show workspace", "⌘1", "home overview"],
          ["findThread", "Find thread in workspace", "⌘F", "search"],
          ["rollUp", "Roll up waiting threads", "⌥⌘R", "attention next oldest queue"],
          ["focusMessage", "Focus message", "⌘L", "composer draft"],
          ["insertFiles", "Insert file or folder paths", "⇧⌘O", "attach references"],
          ["toggleSidebar", "Toggle sidebar", "⌥⌘S", "navigation"],
          ["toggleFiles", "Toggle file browser", "⌥⌘B", "files folders preview finder"],
          ["tileThreads", "Toggle tiled threads", "⌃⌥⌘T", "panes split arrange overview"],
          ["expandConversation", "Expand or collapse conversation", "⇧⌘F", "chat layout"],
        ].filter(([id]) => available.includes(id)).map(([id, label, shortcut, keywords]) =>
          ({id, label, shortcut, keywords, run: () => command(id)}));
        actions.push({id: "newWindow", label: "New window", shortcut: "⇧⌘N", run: () => openWindow("/" + location.search)},
          {id: "duplicateWindow", label: "Open current view in new window", shortcut: "⌥⌘N", run: () => openWindow(location.href)},
          {id: "monitor", label: "Open coordination monitor", keywords: "agents workers", run: () => openWindow("/monitor" + location.search)});
        return actions;
      },
      loadThreads: async signal => {
        const response = await fetch("/api/browser/threads?archived=false", {cache: "no-store", signal});
        if (!response.ok) throw new Error("Could not load threads.");
        const result = await response.json();
        if (!Array.isArray(result.data)) throw new Error("Invalid thread list.");
        return result.data;
      },
      selectThread: id => typeof window.select === "function" ? window.select(id) :
        location.assign("/" + location.search + "#" + encodeURIComponent(id)),
    });
    if (!byID("home")) {
      // The monitor can use the palette, too, without workspace-only controls.
      send({action: "windowState", title: "Coordination monitor", commands: ["commandPalette"]}).catch(() => {});
      return;
    }
    const hint = byID("image-drop-hint");
    if (hint) {
      hint.querySelector("strong").textContent = "Drop images, files, or folders";
      hint.querySelector("span").textContent = "Images attach · Files and folders add local paths";
    }
    byID("message").placeholder = "Message Codex or drop files…";
    const button = document.createElement("button");
    button.type = "button"; button.className = "quiet"; button.id = "desktop-new-window";
    button.textContent = "↗"; button.title = "Open thread in new window (⌘⌥N)";
    button.setAttribute("aria-label", "Open thread in new window");
    button.onclick = () => openWindow(location.href);
    byID("expand-chat").before(button);
    let previous = "", scheduled = false;
    const update = () => {
      scheduled = false;
      const title = visible(chatByID("conversation")) ? chatByID("session-name")?.textContent.trim() || "Conversation" : "Workspace";
      const workspace = visible(chatByID("conversation")) ? chatByID("workspace")?.textContent.trim() || "" : "";
      const state = JSON.stringify({action: "windowState", title, commands: commands(), workspace,
        fileTarget: destination()?.thread || ""});
      if (state !== previous) { previous = state; send(JSON.parse(state)).catch(() => {}); }
    };
    const observer = new MutationObserver(() => {
      if (!scheduled) { scheduled = true; queueMicrotask(update); }
    });
    observer.observe(document.body, {subtree: true, childList: true, characterData: true,
      attributes: true, attributeFilter: ["hidden", "disabled", "open"]});
    window.addEventListener("agent-coord-pane-state", update);
    window.addEventListener("pagehide", () => observer.disconnect(), {once: true});
    update();
  }
  document.addEventListener("DOMContentLoaded", ready, {once: true});

  let permission = "default";
  const pending = new Map();
  class DesktopNotification extends EventTarget {
    static get permission() { return permission; }
    static get agentCoordNative() { return true; }
    static async requestPermission(callback) {
      permission = await send({action: "requestPermission"});
      if (callback) callback(permission);
      window.dispatchEvent(new Event("focus"));
      return permission;
    }
    constructor(title, options = {}) {
      super();
      this.title = String(title);
      this.body = String(options.body || "");
      this.onclick = null;
      this.id = crypto.randomUUID();
      if (permission !== "granted") return;
      pending.set(this.id, this);
      // Keep only a bounded set of live click callbacks.
      if (pending.size > 128) pending.delete(pending.keys().next().value);
      send({action: "notify", id: this.id, title: this.title, body: this.body}).catch(() => pending.delete(this.id));
    }
    close() {
      pending.delete(this.id);
      send({action: "closeNotification", id: this.id}).catch(() => {});
    }
  }
  window.Notification = DesktopNotification;
  window.__agentCoordNotificationClick = id => {
    const item = pending.get(id);
    if (!item) return;
    const event = new Event("click");
    item.dispatchEvent(event);
    if (typeof item.onclick === "function") item.onclick(event);
  };
  send({action: "permission"}).then(value => {
    permission = value;
    window.dispatchEvent(new Event("focus"));
  }).catch(() => {});
})();
