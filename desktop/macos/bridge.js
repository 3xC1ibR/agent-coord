/* Loaded only by the native wrapper, before the existing web UI. */
(() => {
  "use strict";
  const native = window.webkit?.messageHandlers?.desktop;
  if (!native || window.top !== window) return;
  const send = body => native.postMessage(body);
  const conversationWindow = __AGENT_COORD_CONVERSATION_WINDOW__;
  const initialWindowState = __AGENT_COORD_WINDOW_STATE__;
  let workspaceState = null, updateWindowState = null, navigationReported = false;
  const route = () => {
    const query = new URLSearchParams(location.search);
    query.delete("new_project"); query.delete("directory");
    // URLSearchParams.size needs Safari 17; macOS 12 WebKit only has toString().
    const search = query.toString();
    return location.pathname + (search ? "?" + search : "") + location.hash;
  };
  if (conversationWindow) document.documentElement?.classList.add("conversation-window");
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
      const previous = this.getItem(key);
      setItem.call(this, key, value);
      if (previous !== String(value)) workspaceState?.storageChanged(String(key));
      if (this === localStorage && savedKey(String(key)) && key !== "agent-coord.notifications.focus") {
        send({action: "preference", key: String(key), value: String(value)}).catch(() => {});
      }
    };
    Storage.prototype.removeItem = function (key) {
      const previous = this.getItem(key);
      removeItem.call(this, key);
      if (previous !== null) workspaceState?.storageChanged(String(key));
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
  workspaceState = window.agentCoordWorkspaceState?.create({window, document,
    getState: () => typeof state === "object" ? state : null,
    getViews: () => typeof savedViews !== "undefined" ? savedViews : null,
    getNavigation: () => typeof navigation !== "undefined" ? navigation : null,
    select: (...args) => typeof select === "function" ? select(...args) : undefined,
    newSession: () => typeof newSession === "function" ? newSession() : undefined,
    goHome: () => typeof goHome === "function" ? goHome({restoreDispatcher: false}) : undefined,
    setChatExpanded: value => typeof setChatExpanded === "function" ? setChatExpanded(value) : undefined,
    render: () => {
      if (typeof renderStatus === "function") renderStatus();
      if (typeof renderTitle === "function") renderTitle();
      if (typeof renderList === "function") renderList();
    },
    onSave: state => send({action: "saveWindowState", state}), onError: reportError,
  });
  workspaceState?.preload(initialWindowState);
  // The router replaces history without a hashchange. Observe real navigation
  // here, not the transcript MutationObserver (which also runs on every token).
  if (window.history) for (const name of ["pushState", "replaceState"]) {
    const original = window.history[name];
    window.history[name] = function (...args) {
      const previous = route(), before = JSON.stringify(window.history.state?.agentCoord);
      const result = original.apply(this, args);
      if (previous !== route() || before !== JSON.stringify(window.history.state?.agentCoord)) {
        workspaceState?.changed(); queueMicrotask(() => updateWindowState?.());
      }
      return result;
    };
  }
  for (const name of ["hashchange", "popstate"]) window.addEventListener(name, () => {
    workspaceState?.changed(); updateWindowState?.();
  });
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
    if (conversationWindow) return Array.from(byID("conversation-tabs")?.querySelectorAll("[data-thread-tab]") || [])
      .map(button => button.dataset.threadTab).filter(id => id && id !== "project-scratchpad");
    return [...new Set(Array.from(byID("sessions")?.querySelectorAll("button.session[data-thread]") || [])
      .map(button => button.dataset.thread))];
  }
  function cycleSession(direction) {
    const ids = sessionOrder();
    if (!ids.length) return false;
    const panes = window.agentCoordPanes;
    const selected = panes?.active ? panes.selected : conversationWindow ?
      byID("conversation-tabs")?.querySelector('[aria-selected="true"]')?.dataset.threadTab :
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
    if (window.agentCoordSendWork?.available &&
        (!dialog || dialog === palette?.dialog || dialog === window.agentCoordSendWork.dialog)) result.push("sendWork");
    if (window.agentCoordProjectScratchpad?.available &&
        (!dialog || dialog === palette?.dialog || dialog === window.agentCoordProjectScratchpad.dialog)) result.push("projectScratchpad");
    if (window.agentCoordProjectScratchpad?.available && byID("open-project-scratchpad") &&
        !byID("open-project-scratchpad").disabled && (!dialog || dialog === palette?.dialog)) result.push("openProjectScratchpad");
    if (destination()) result.push("insertFiles");
    if (dialog || !byID("back-home")) return result;
    result.push("newSession");
    if (!conversationWindow) result.push("workspace", "findThread", "toggleSidebar", "toggleFiles");
    if (!conversationWindow && byID("open-folder")) result.push("openFolder");
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
      case "sendWork":
        if (palette?.dialog.open) palette.close();
        return window.agentCoordSendWork.toggle();
      case "projectScratchpad":
        if (palette?.dialog.open) palette.close();
        return window.agentCoordProjectScratchpad.toggle();
      case "openProjectScratchpad":
        if (palette?.dialog.open) palette.close();
        byID("open-project-scratchpad").click(); break;
      case "workspace": byID("back-home").click(); break;
      case "findThread": byID("sidebar-search").click(); break;
      case "newSession": byID("new-session").click(); break;
      case "openFolder": byID("open-folder").click(); break;
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
    folderSlot: "native-__AGENT_COORD_FOLDER_SLOT__",
    conversationWindow,
    chooseFolder: async () => (await send({action: "chooseFiles", workspace: true}))[0]?.path || null,
    newWindow: url => send({action: "newWindow", url}),
    openSettings: () => send({action: "openSettings"}),
    closeSettings: () => send({action: "closeSettings"}),
    closeConversationWindow: () => {
      if (transferring) { closeAfterTransfer = true; return Promise.resolve(true); }
      return send({action: "closeConversationWindow"});
    },
    fileEditor: operation => send({action: "fileEditor", operation}),
    exportWindowState: () => workspaceState?.capture() ?? initialWindowState ?? null,
    windowStateBusy: () => workspaceState?.busy() || false,
    restoreWindowState: snapshot => workspaceState?.restore(snapshot) || Promise.resolve(false),
    navigationReady: () => {
      navigationReported = true;
      workspaceState?.ready();
      return send({action: "navigationReady"}).catch(reportError);
    },
  });

  // WebKit's native HTML dragend reports the same result for Escape and an
  // unhandled external drop. Pointer capture gives us an unambiguous release
  // outside the window, and reuses the existing HTML reorder/split handlers.
  let tabDrag = null, transferring = false, suppressTabClick = false, closeAfterTransfer = false;
  const dragEvent = (type, event, drag) => new DragEvent(type, {bubbles: true, cancelable: true,
    dataTransfer: drag.transfer, clientX: event.clientX, clientY: event.clientY});
  function finishTabDrag(event, cancel = false) {
    const drag = tabDrag;
    if (!drag || event.pointerId != null && event.pointerId !== drag.pointerId) return;
    tabDrag = null;
    drag.tab.releasePointerCapture?.(drag.pointerId);
    if (!drag.started) return;
    suppressTabClick = true; setTimeout(() => { suppressTabClick = false; }, 0);
    let accepted = false;
    if (!cancel) {
      const target = document.elementFromPoint(event.clientX, event.clientY);
      if (target) {
        const over = dragEvent("dragover", event, drag); target.dispatchEvent(over);
        if (over.defaultPrevented) {
          const drop = dragEvent("drop", event, drag); target.dispatchEvent(drop);
          accepted = drop.defaultPrevented;
        }
      }
    }
    drag.source.dispatchEvent(dragEvent("dragend", event, drag));
    drag.ghost?.remove();
    if (cancel || accepted || transferring) return;
    const transfer = window.agentCoordConversationTransfer;
    const payload = transfer?.capture(drag.id);
    if (!payload) return;
    transferring = true;
    transfer.lock(drag.id, true);
    send({action: "detachConversation", payload, x: event.clientX, y: event.clientY})
      .then(async moved => {
        if (!moved) return;
        try {
          await transfer.release(drag.id, payload.sourceCwd ?? payload.cwd, payload, {retainDraft: true});
          if (moved.transferId) await send({action: "completeConversationTransfer", transferId: moved.transferId, committed: true});
          transfer.discard?.(drag.id);
        } catch (error) {
          // Keep a recoverable source draft until native confirms the commit.
          const recovery = transfer.capture(drag.id, {validate: false}) || payload;
          await transfer.restore(recovery, {rollback: true});
          if (moved.transferId) await send({action: "completeConversationTransfer", transferId: moved.transferId, committed: false}).catch(() => {});
          throw error;
        }
      })
      .catch(reportError).finally(() => {
        transfer.lock(drag.id, false); transferring = false;
        if (closeAfterTransfer) {
          closeAfterTransfer = false;
          if (transfer.empty()) send({action: "closeConversationWindow"}).catch(reportError);
        }
      });
  }
  document.addEventListener("pointerdown", event => {
    if (event.button !== 0 || event.isPrimary === false || tabDrag || transferring || openDialog()) return;
    const tab = event.target.closest?.("[data-thread-tab], [data-pane-thread]");
    const id = tab?.dataset.threadTab || tab?.dataset.paneThread;
    if (!id || id === "new-session" || id === "project-scratchpad" || !window.agentCoordConversationTransfer) return;
    const source = tab.dataset.paneThread ? tab : tab.closest(".conversation-tab") || tab;
    event.preventDefault(); tab.focus({preventScroll: true});
    tabDrag = {id, tab, source, pointerId: event.pointerId, x: event.clientX, y: event.clientY, started: false};
    tab.setPointerCapture(event.pointerId);
  }, true);
  document.addEventListener("pointermove", event => {
    const drag = tabDrag;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (!drag.started) {
      if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 6) return;
      drag.transfer = new DataTransfer();
      const start = dragEvent("dragstart", event, drag);
      drag.source.dispatchEvent(start);
      if (start.defaultPrevented) { finishTabDrag(event, true); return; }
      drag.started = true;
      drag.ghost = drag.source.cloneNode(true);
      Object.assign(drag.ghost.style, {position: "fixed", pointerEvents: "none", zIndex: "10000", opacity: ".85"});
      document.body.append(drag.ghost);
    }
    event.preventDefault();
    Object.assign(drag.ghost.style, {left: event.clientX + 12 + "px", top: event.clientY + 12 + "px"});
    document.elementFromPoint(event.clientX, event.clientY)?.dispatchEvent(dragEvent("dragover", event, drag));
  }, true);
  document.addEventListener("pointerup", event => finishTabDrag(event), true);
  document.addEventListener("pointercancel", event => finishTabDrag(event, true), true);
  document.addEventListener("lostpointercapture", event => finishTabDrag(event, true), true);
  document.addEventListener("keydown", event => {
    if (tabDrag && event.key === "Escape") {
      event.preventDefault(); event.stopImmediatePropagation(); finishTabDrag(event, true);
    }
  }, true);
  document.addEventListener("click", event => {
    if (suppressTabClick) { event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
  window.addEventListener("pagehide", event => finishTabDrag(event, true));

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
  function openFileLink(event) {
    if (event.defaultPrevented || event.button !== 0) return;
    const link = event.target.closest?.(".markdown a[href]");
    if (!link) return;
    // Read the authored path: link.href has already acquired the backend origin.
    const raw = link.getAttribute("href");
    if (!raw || /^(?:[?#]|\/\/)/.test(raw)) return;
    const path = raw.replace(/(?::\d+(?::\d+)?|#L\d+(?:C\d+)?(?:-L?\d+(?:C\d+)?)?)$/, "");
    if (/^[a-z][a-z\d+.-]*:/i.test(path) || /^\/(?:[?#]|$|monitor(?:[?#]|$)|api\/)/.test(path)) return;
    event.preventDefault(); event.stopImmediatePropagation();
    try {
      const doc = link.ownerDocument;
      const workspace = doc.getElementById("workspace")?.textContent.trim() || "";
      send({action: "openFile", path: decodeURIComponent(path), workspace}).catch(reportError);
    } catch (error) { reportError(error); }
  }
  function bindPane(doc) {
    if (boundPanes.has(doc)) return;
    boundPanes.add(doc); doc.addEventListener("drop", handleDrop, true);
    workspaceState?.bindDocument(doc);
    doc.addEventListener("click", openFileLink, true);
    const observer = new MutationObserver(() => window.dispatchEvent(new Event("agent-coord-pane-state")));
    observer.observe(doc.body, {subtree: true, childList: true, characterData: true,
      attributes: true, attributeFilter: ["open", "disabled", "hidden"]});
    doc.defaultView?.addEventListener("pagehide", () => observer.disconnect(), {once: true});
  }
  document.addEventListener("drop", handleDrop, true);
  document.addEventListener("click", openFileLink, true);

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
    if (location.pathname === "/settings") {
      send({action: "windowState", title: "Settings", commands: [], route: route()}).catch(reportError);
      return;
    }
    palette = new DesktopCommandPalette({document, onError: reportError,
      actions: () => {
        const available = commands();
        const actions = [
          ["newSession", "New session", "⌘N", "create chat"],
          ["sendWork", "Send work", "⇧⌘K", "dispatch route request another conversation"],
          ["openFolder", "Open project", "⌘O", "folder directory recent"],
          ["openProjectScratchpad", "Open project scratchpad", "", "notes tab jot"],
          ["projectScratchpad", "Toggle project scratchpad", "⇧⌘S", "notes capture jot floating"],
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
          {id: "settings", label: "Settings", shortcut: "⌘,", keywords: "preferences appearance notifications speech remote access editor", run: () => send({action: "openSettings"})});
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
      searchConversations: byID("search") ? query => {
        window.agentCoordFindThreads();
        byID("search-scope").value = "all_projects";
        byID("search").value = query;
        byID("search").dispatchEvent(new Event("input", {bubbles: true}));
      } : null,
    });
    if (!byID("back-home")) {
      // The monitor can use the palette, too, without workspace-only controls.
      send({action: "windowState", title: "Coordination monitor", commands: ["commandPalette"], route: route()}).catch(() => {});
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
      // A deleted/unavailable initial conversation can reject boot's final
      // select after the shell and router are ready. Recover that loaded shell
      // instead of leaving native recovery permanently waiting for readiness.
      if (!navigationReported && initialWindowState && typeof state === "object" &&
          typeof navigation !== "undefined" && navigation && state.threadTabs) {
        const failed = state.conversations?.get(state.selected);
        if (failed?.refreshError && !failed.pending) window.agentCoordDesktop.navigationReady();
      }
      // A main window names its active project; tabs and the heading already
      // name the thread. A conversation window has no project tabs.
      const project = byID("view-tabs")?.querySelector('[aria-selected="true"] .view-tab-name')?.textContent.trim();
      const title = !conversationWindow && project ? project : visible(chatByID("conversation"))
        ? chatByID("session-name")?.textContent.trim() || "Conversation" : "Workspace";
      const workspace = visible(chatByID("conversation")) ? chatByID("workspace")?.textContent.trim() || "" : "";
      const encodedState = JSON.stringify({action: "windowState", title, commands: commands(), workspace, route: route(),
        fileTarget: destination()?.thread || ""});
      if (encodedState !== previous) { previous = encodedState; send(JSON.parse(encodedState)).catch(() => {}); }
    };
    updateWindowState = update;
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
      this.data = options.data ?? null;
      this.onclick = null;
      this.id = crypto.randomUUID();
      if (permission !== "granted") return;
      pending.set(this.id, this);
      // Keep only a bounded set of live click callbacks.
      if (pending.size > 128) pending.delete(pending.keys().next().value);
      // A thread lets the app reveal its existing tab in whichever window holds it.
      const thread = this.data?.thread_id;
      send({action: "notify", id: this.id, title: this.title, body: this.body,
        ...(typeof thread === "string" && /^[a-z\d_.~-]{1,200}$/i.test(thread) ? {thread} : {})}).catch(() => pending.delete(this.id));
    }
    close() {
      pending.delete(this.id);
      send({action: "closeNotification", id: this.id}).catch(() => {});
    }
  }
  window.Notification = DesktopNotification;
  // Native notification routing asks every window before choosing a destination.
  window.agentCoordHasConversation = id => typeof id === "string" && Boolean(
    (window.agentCoordPanes?.active && window.agentCoordPanes.layout?.groups?.some(group => group.includes(id))) ||
    (typeof state === "object" && state?.threadTabs?.has?.(id)));
  window.__agentCoordNotificationClick = (id, routed = false) => {
    const item = pending.get(id);
    if (!item) return;
    // The native app already opened the thread in its owning window.
    if (routed) { pending.delete(id); return; }
    const event = new Event("click");
    item.dispatchEvent(event);
    if (typeof item.onclick === "function") item.onclick(event);
  };
  let checkingPermission = false;
  function refreshPermission() {
    if (checkingPermission) return;
    checkingPermission = true;
    send({action: "permission"}).then(value => {
      if (permission !== value) { permission = value; window.dispatchEvent(new Event("focus")); }
    }).catch(() => {}).finally(() => { checkingPermission = false; });
  }
  window.addEventListener("focus", refreshPermission);
  window.addEventListener("storage", event => { if (event.key === "agent-coord.notifications.enabled") refreshPermission(); });
  refreshPermission();
})();
