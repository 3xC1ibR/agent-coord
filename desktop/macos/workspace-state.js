/* Local window recovery. Loaded in the page realm before bridge.js and app.js. */
(function (root) {
  "use strict";
  // Clone JSON-shaped state without encoding image strings into another giant
  // JSON buffer. Strings are immutable and can be shared until native transport.
  const copy = value => {
    if (Array.isArray(value)) return value.map(item => item === undefined ? null : copy(item));
    if (value && typeof value === "object") return Object.fromEntries(Object.entries(value)
      .filter(([, item]) => item !== undefined && typeof item !== "function").map(([key, item]) => [key, copy(item)]));
    return typeof value === "number" && !Number.isFinite(value) ? null : value;
  };
  const entries = value => Array.isArray(value) ? value.filter(pair => Array.isArray(pair) && pair.length === 2 && typeof pair[0] === "string") : [];
  const pairs = value => value?.entries ? [...value.entries()] : [];
  const pick = (value, keys) => Object.fromEntries(keys.filter(key => value?.[key] !== undefined).map(key => [key, copy(value[key])]));
  const tab = value => pick(value, ["thread_id", "title", "conversation_id", "tree_root", "tree_open", "project_cwd", "cwd", "client", "browser_session", "attention"]);
  const usefulStorageKey = key => typeof key === "string" && ["agent-coord.folder.", "agent-coord.thread-tabs.",
    "agent-coord.views.", "timeline-disclosures:", "agent-coord.scratchpad-draft:",
    "agent-coord.project-scratchpad-draft:", "dispatch-request:", "review-request:"].some(prefix => key.startsWith(prefix));
  function readStorage(storage, allowed = usefulStorageKey) {
    const result = {};
    try {
      for (let i = 0; i < storage?.length; i++) {
        const key = storage.key(i);
        if (allowed(key)) result[key] = storage.getItem(key);
      }
    } catch { /* Optional browser storage. */ }
    return result;
  }
  function writeStorage(storage, values) {
    if (!values || typeof values !== "object" || Array.isArray(values)) return;
    for (const [key, value] of Object.entries(values)) {
      if (!usefulStorageKey(key) || typeof value !== "string") continue;
      try { storage?.setItem(key, value); } catch { /* One full store must not prevent draft recovery. */ }
    }
  }
  function sessionDraft(value) {
    if (!value?.session || typeof value.draftId !== "string") return null;
    return {...pick(value, ["draftId", "createdId", "commandFeedback"]),
      session: pick(value.session, ["cwd", "client", "model", "effort", "yolo", "project_id", "repository_id", "name"]),
      work_thread: {...tab(value.work_thread), thread_id: "new-session", browser_session: true, links: []},
      thread: {turns: []}, requests: [], queuedMessages: [], running: false};
  }
  const workDraft = value => ({message: typeof value?.message === "string" ? value.message : "",
    draft: pick(value?.draft, ["client", "model", "effort", "yolo"]),
    locked: value?.locked ? pick(value.locked, ["id", "kind", "title", "summary"]) : null});
  // Keep the attachment component's full data URLs. Its four-image/5 MiB limits
  // apply per draft, not per window. A native snapshot must not truncate them.
  const images = values => Array.isArray(values) ? values.filter(value => typeof value?.url === "string")
    .map(value => pick(value, ["name", "url"])) : [];
  const draft = value => ({...pick(value, ["text", "mentions", "scroll", "following", "selectionStart", "selectionEnd", "disclosures"]),
    images: images(value?.images)});
  function layout(value) {
    if (!Array.isArray(value?.groups) || !value.groups.length ||
        value.groups.some(group => !Array.isArray(group) || !group.length || group.some(id => typeof id !== "string"))) return null;
    const ids = value.groups.flat();
    if (new Set(ids).size !== ids.length) return null;
    const result = pick(value, ["presentation", "columns", "groups", "tabs", "order", "selected", "maximized", "tree", "widths", "heights", "returnTarget"]);
    result.order = Array.isArray(result.order) ? result.order.filter(id => ids.includes(id)) : ids;
    result.tabs = result.groups.map((group, i) => group.includes(result.tabs?.[i]) ? result.tabs[i] : group[0]);
    result.selected = ids.includes(result.selected) ? result.selected : result.tabs[0];
    result.maximized = Number.isInteger(result.maximized) && result.maximized >= 0 && result.maximized < result.groups.length ? result.maximized : null;
    if (!Array.isArray(result.columns) || result.columns.some(n => !Number.isInteger(n) || n < 1) ||
        result.columns.reduce((a, b) => a + b, 0) !== result.groups.length) result.columns = [result.groups.length];
    const leaves = [];
    const validTree = (node, depth = 0) => {
      if (!node || depth > 30) return false;
      if (!node.children) { leaves.push(node.slot); return Number.isInteger(node.slot); }
      return ["horizontal", "vertical"].includes(node.axis) && Array.isArray(node.children) && node.children.length > 0 &&
        Array.isArray(node.sizes) && node.sizes.length === node.children.length && node.sizes.every(n => Number.isFinite(n) && n > 0) &&
        node.children.every(child => validTree(child, depth + 1));
    };
    if (result.tree && (!validTree(result.tree) || leaves.length !== result.groups.length ||
        new Set(leaves).size !== leaves.length || leaves.some(i => i < 0 || i >= result.groups.length))) delete result.tree;
    return result;
  }

  function create({window: view, document: doc, getState, getViews = () => null, getNavigation = () => null,
    select, newSession, goHome, setChatExpanded, render, onSave, onError = () => {}, delay = 600}) {
    const $ = id => doc.getElementById(id);
    const storage = name => { try { return view[name]; } catch { return null; } };
    let pending = null, ready = false, restoring = false, restored = false, timer = null, last = "", interacted = false, dirtyAt = null, nextImage = 0;
    const imageIds = new Map();
    const touched = new Set(), bound = new WeakSet(), hooked = new WeakSet(), paneWaiters = new Map();
    const currentState = () => { try { return getState(); } catch { return null; } };
    const currentViews = () => { try { return getViews(); } catch { return null; } };
    const currentNavigation = () => { try { return getNavigation(); } catch { return null; } };
    const currentDraftId = s => s?.selected === "new-session" ? s.newSessionDraft?.draftId || "new-session" : s?.selected;
    function changed() {
      if (!ready || restoring || pending && !restored) return;
      view.clearTimeout(timer);
      dirtyAt ??= Date.now();
      timer = view.setTimeout(flush, Math.min(delay, Math.max(0, 3000 - (Date.now() - dirtyAt))));
    }
    function storageChanged(key) { if (usefulStorageKey(key)) changed(); }
    function watch(object, name, after = changed) {
      const original = object?.[name];
      if (typeof original !== "function") return;
      object[name] = function (...args) {
        const result = original.apply(this, args);
        after.apply(this, args);
        if (result?.then) result.then(changed, changed);
        return result;
      };
    }
    function watchMap(map) {
      if (!map || hooked.has(map)) return;
      hooked.add(map);
      const set = map.set, remove = map.delete, clear = map.clear;
      map.set = function (key, value) { const before = this.get(key); set.call(this, key, value); if (before !== value) changed(); return this; };
      map.delete = function (key) { const removed = remove.call(this, key); if (removed) changed(); return removed; };
      map.clear = function () { const size = this.size; clear.call(this); if (size) changed(); };
    }
    function bindDocument(document) {
      if (!document || bound.has(document)) return;
      bound.add(document);
      const edited = event => {
        const s = currentState();
        if (event.type === "input" || event.type === "change") {
          const composer = document !== doc || ["message", "model-picker", "effort-picker"].includes(event.target?.id);
          const id = !composer ? null : document === doc ? currentDraftId(s) : view.agentCoordPanes?.from(document.defaultView)?.thread?.thread_id;
          if (id) { touched.add(id); if (s?.selected === "new-session") touched.add("new-session"); }
          if (event.target?.id?.startsWith("send-work-")) touched.add("send-work");
        }
        // A focus click is not a request to abandon the saved workspace.
        const navigationTarget = event.target?.closest?.('[data-thread-tab], [data-pane-thread], button[data-thread], [data-project-cwd], [data-view], #back-home, #new-session, #welcome-new, #tile-threads, #leave-tiles, #open-project-scratchpad');
        if (event.isTrusted && (["input", "change"].includes(event.type) || event.type === "click" && navigationTarget)) interacted = true;
        changed();
      };
      for (const name of ["input", "change", "click", "keyup", "pointerup", "drop"]) document.addEventListener(name, edited, true);
      document.addEventListener("scroll", event => {
        // Automatic following scrolls happen on streaming tokens. Only capture
        // reading positions when the user has left the live tail.
        const s = currentState();
        if (event.target?.id === "timeline" && (document !== doc || s?.timelineScroll?.following)) return;
        changed();
      }, true);
    }
    function installHooks() {
      const s = currentState();
      if (!s) return;
      for (const map of [s.drafts, s.attachments?.drafts, s.mentions?.drafts]) watchMap(map);
      watchMap(view.agentCoordSendWork?.drafts);
      if (s.attachments && !hooked.has(s.attachments)) { hooked.add(s.attachments); watch(s.attachments, "onChange"); }
      const panes = view.agentCoordPanes;
      if (panes && !hooked.has(panes)) {
        hooked.add(panes);
        for (const name of ["focus", "show", "hide", "tile", "moveThread", "maximize", "close", "draft"]) watch(panes, name);
        watch(panes, "attach", source => {
          const record = panes.from(source), id = record?.thread?.thread_id;
          const input = record?.frame?.contentDocument?.getElementById("message"), saved = record?.workspaceSelection;
          if (input && saved && !touched.has(id)) input.setSelectionRange(saved.start, saved.end);
          if (record) delete record.workspaceSelection;
          paneWaiters.get(id)?.(); paneWaiters.delete(id); changed();
        });
      }
    }
    function preload(snapshot) {
      if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) return;
      pending = copy(snapshot);
      writeStorage(storage("sessionStorage"), snapshot.sessionStorage);
      writeStorage(storage("localStorage"), snapshot.localStorage);
    }
    function capture() {
      // Native quit can arrive during asynchronous app boot or restoration. Keep
      // the last complete snapshot instead of replacing it with a half-built UI.
      if (pending && (!ready || !restored || restoring)) {
        const result = copy(pending), s = currentState();
        if (s && touched.size) {
          const texts = new Map(entries(result.drafts)), imageDrafts = new Map(entries(result.images)), refs = new Map(entries(result.mentions));
          for (const id of touched) {
            const key = id === currentDraftId(s) && s.selected === "new-session" ? "new-session" : id;
            const text = key === s.selected ? $("message")?.value : s.drafts.get(key);
            if (typeof text === "string") texts.set(key, text);
            if (s.attachments?.drafts.has(id)) imageDrafts.set(id, images(s.attachments.drafts.get(id)));
            const mentions = s.mentions?.drafts.get(id);
            if (mentions) refs.set(id, {text: mentions.text, refs: copy(mentions.refs)});
          }
          Object.assign(result, {drafts: [...texts], images: [...imageDrafts], mentions: [...refs]});
          if (touched.has("new-session")) result.newSessionDraft = sessionDraft(s.newSessionDraft);
          const work = view.agentCoordSendWork;
          if (touched.has("send-work") && work?.cwd) {
            const drafts = new Map(entries(result.sendWorkDrafts));
            drafts.set(work.cwd, workDraft({message: work.input.value, locked: work.locked, draft: work.draft}));
            result.sendWorkDrafts = [...drafts];
          }
        }
        return result;
      }
      const s = currentState(), tabs = s?.threadTabs, views = currentViews(), panes = view.agentCoordPanes;
      const result = {version: 1, sessionStorage: readStorage(storage("sessionStorage"))};
      if (!s?.config || !tabs) return result;
      const localKey = key => usefulStorageKey(key) && (key === s.folders?.key ||
        key.startsWith(tabs.baseKey + ":") || views?.baseKey && (key === views.baseKey || key.startsWith(views.baseKey + ":") || key.startsWith(views.baseKey + ".")));
      result.localStorage = readStorage(storage("localStorage"), localKey);
      const texts = new Map(s.drafts), imageDrafts = new Map(pairs(s.attachments?.drafts).map(([id, value]) => [id, images(value)]));
      const mentions = new Map(pairs(s.mentions?.drafts).map(([id, value]) => [id, {text: value.text, refs: copy(value.refs || [])}]));
      const positions = new Map(pairs(s.conversationPositions));
      for (const [id, entry] of pairs(s.conversations)) positions.set(id, pick(entry, ["scroll", "following"]));
      let composer = null;
      if (s.selected && !panes?.active) {
        const field = $("message"), id = currentDraftId(s);
        if (field) {
          texts.set(s.selected, field.value);
          mentions.set(id, {text: field.value, refs: s.mentions?.snapshot(id, field.value) || []});
          composer = {id: s.selected, selectionStart: field.selectionStart, selectionEnd: field.selectionEnd};
        }
        positions.set(s.selected, {scroll: s.timelineScroll?.position() ?? $("timeline")?.scrollTop ?? 0,
          following: s.timelineScroll?.following ?? true});
      }
      const records = pairs(panes?.records).map(([id, record]) => {
        const value = panes.active ? record.api?.exportDraft() || record.initialDraft || panes.getDraft?.(id) || {}
          : panes.getDraft?.(id) || record.initialDraft || record.api?.exportDraft() || {};
        const saved = draft(value);
        if (typeof saved.text === "string") {
          texts.set(id, saved.text); imageDrafts.set(id, saved.images);
          mentions.set(id, {text: saved.text, refs: saved.mentions || []});
        }
        const input = record.frame?.contentDocument?.getElementById("message");
        if (input) Object.assign(saved, {selectionStart: input.selectionStart, selectionEnd: input.selectionEnd});
        return [id, {thread: tab(record.thread), draft: saved}];
      });
      const sendWork = view.agentCoordSendWork;
      const workDrafts = new Map(pairs(sendWork?.drafts).map(([cwd, value]) => [cwd, workDraft(value)]));
      if (sendWork?.cwd) workDrafts.set(sendWork.cwd, workDraft({message: sendWork.input.value, locked: sendWork.locked, draft: sendWork.draft}));
      Object.assign(result, {projectCwd: s.projectCwd, selected: s.selected, composer,
        folders: s.folders ? {projects: copy(s.folders.projects), active: s.folders.selected?.cwd || ""} : null,
        tabs: {projects: pairs(tabs.projects).map(([cwd, items]) => [cwd, items.map(tab)]),
          selections: pairs(tabs.selections), scratchpads: pairs(tabs.scratchpads), selected: tabs.selected ?? null},
        // Empty drafts restore identically when omitted; keeping them would retain
        // an entry for every conversation ever viewed.
        drafts: [...texts].filter(([, text]) => text !== ""), images: [...imageDrafts].filter(([, list]) => list.length),
        mentions: [...mentions].filter(([, value]) => value.text || value.refs?.length),
        newSessionDraft: sessionDraft(s.newSessionDraft), draftSequence: s.draftSequence || 0,
        projectDrafts: pairs(s.projectDrafts).map(([cwd, value]) => [cwd, {text: value.text || "", detail: sessionDraft(value.detail)}]),
        projectDestinations: pairs(s.projectDestinations), positions: [...positions],
        sendWorkDrafts: [...workDrafts],
        groupExpansion: pairs(s.groupExpansion), phaseScroll: pairs(s.phaseScroll),
        panes: panes ? {active: panes.active, viewId: panes.viewId,
          layouts: pairs(panes.layouts).map(([id, value]) => [id, layout(value)]).filter(([, value]) => value), records} : null,
        views: views ? {cwd: views.cwd, activeId: views.activeId, navigation: copy(views.navigation), current: views.read()} : null,
        navigation: copy(currentNavigation()?.capture?.()), chatExpanded: doc.body?.classList.contains("chat-expanded") || false});
      return copy(result);
    }
    function flush() {
      view.clearTimeout(timer); timer = null; dirtyAt = null;
      if (!ready || restoring || pending && !restored) return;
      try {
        const snapshot = capture(), usedImages = new Set();
        const encoded = JSON.stringify(snapshot, (key, value) => {
          if (key !== "url" || typeof value !== "string" || !value.startsWith("data:image/")) return value;
          if (!imageIds.has(value)) imageIds.set(value, ++nextImage);
          usedImages.add(value); return {image: imageIds.get(value)};
        });
        for (const value of imageIds.keys()) if (!usedImages.has(value)) imageIds.delete(value);
        if (encoded === last) return;
        last = encoded;
        Promise.resolve(onSave?.(snapshot)).catch(error => { last = ""; onError(error); });
      } catch (error) { onError(error); }
    }
    function restoreMap(target, values, accept = () => true, preserve = false) {
      for (const [id, value] of entries(values)) if (!touched.has(id) && accept(value) && (!preserve || !target.has(id))) target.set(id, copy(value));
    }
    const attempt = fn => { try { return fn(); } catch (error) { onError(error); return undefined; } };
    async function restore(snapshot = pending) {
      if (restored) return true;
      const s = currentState();
      if (!ready || !s?.threadTabs || !snapshot || typeof snapshot !== "object") return false;
      restoring = true;
      try {
        const tabs = s.threadTabs, views = currentViews(), panes = view.agentCoordPanes;
        const currentInput = $("message");
        // Retain actual edits even if they arrived before native's ready reply.
        if (touched.has(currentDraftId(s)) && s.selected && currentInput) s.drafts.set(s.selected, currentInput.value);
        restoreMap(s.drafts, snapshot.drafts, value => typeof value === "string");
        if (s.selected && currentInput && !touched.has(currentDraftId(s)) && s.drafts.has(s.selected)) currentInput.value = s.drafts.get(s.selected);
        for (const [id, value] of entries(snapshot.images)) if (!touched.has(id)) s.attachments?.drafts.set(id, images(value));
        const newDraft = sessionDraft(snapshot.newSessionDraft);
        if (newDraft && !touched.has("new-session")) s.newSessionDraft = newDraft;
        s.draftSequence = Math.max(s.draftSequence || 0, Number(snapshot.draftSequence) || 0);
        const sendWork = view.agentCoordSendWork;
        if (sendWork && !touched.has("send-work")) {
          for (const [cwd, value] of entries(snapshot.sendWorkDrafts)) sendWork.drafts.set(cwd, workDraft(value));
          const saved = sendWork.drafts.get(sendWork.cwd);
          if (saved) { sendWork.input.value = saved.message; sendWork.draft = saved.draft; sendWork.locked = saved.locked; attempt(() => sendWork.render()); }
        }
        restoreMap(s.projectDrafts ||= new Map(), entries(snapshot.projectDrafts).map(([cwd, value]) => [cwd,
          {text: typeof value?.text === "string" ? value.text : "", detail: sessionDraft(value?.detail)}]), () => true, interacted);
        for (const [name, values] of [["projectDestinations", snapshot.projectDestinations], ["conversationPositions", snapshot.positions],
          ["groupExpansion", snapshot.groupExpansion], ["phaseScroll", snapshot.phaseScroll]]) restoreMap(s[name] ||= new Map(), values, () => true, interacted);
        for (const [id, entry] of pairs(s.conversations)) {
          if (!touched.has(id)) Object.assign(entry, s.conversationPositions.get(id));
          try { entry.disclosures = new Set(JSON.parse(storage("sessionStorage")?.getItem("timeline-disclosures:" + id) || "[]")); } catch { /* Old optional disclosure state. */ }
        }
        if (!interacted) {
          restoreMap(tabs.projects, entries(snapshot.tabs?.projects).map(([cwd, items]) => [cwd,
            Array.isArray(items) ? items.filter(item => typeof item?.thread_id === "string").map(tab) : []]));
          restoreMap(tabs.selections, snapshot.tabs?.selections);
          restoreMap(tabs.scratchpads, snapshot.tabs?.scratchpads);
          tabs.items = tabs.projects.get(tabs.cwd) || tabs.items;
          if (snapshot.tabs && Object.hasOwn(snapshot.tabs, "selected")) tabs.selected = snapshot.tabs.selected;
          // Saved tabs carry identity/layout; current inventory supplies their
          // live status and unread indicators, never an old persisted badge.
          attempt(() => { if (tabs.update) tabs.update(s.sessions || []); else tabs.render(); });
          if (views && snapshot.views?.cwd === views.cwd) {
            const saved = snapshot.views;
            if (saved.navigation && typeof saved.navigation === "object") views.navigation = copy(saved.navigation);
            if (views.items.some(item => item.id === saved.activeId)) { views.activeId = saved.activeId; views.definition = views.current(); }
            attempt(() => { if (saved.current) views.apply(saved.current); views.render(); views.restoreScroll(); });
          }
        }
        if (panes && snapshot.panes) {
          for (const [id, value] of entries(snapshot.panes.layouts)) {
            const valid = layout(value);
            if (valid && (!interacted || !panes.layouts.has(id))) panes.layouts.set(id, valid);
          }
          for (const [id, value] of entries(snapshot.panes.records)) {
            if (!value?.thread || touched.has(id)) continue;
            const record = panes.record({...tab(value.thread), thread_id: id});
            const saved = draft(value.draft || {});
            record.initialDraft = saved;
            if (record.api) attempt(() => { record.api.restoreDraft(saved); record.initialDraft = null; });
            if (Number.isFinite(saved.selectionStart)) record.workspaceSelection = {start: saved.selectionStart, end: saved.selectionEnd};
          }
          for (const value of panes.layouts.values()) for (const id of value.groups.flat()) if (!panes.records.has(id)) panes.record({thread_id: id, title: "Conversation"});
        }
        if (!interacted) {
          if (snapshot.panes?.active && panes?.layouts.has(snapshot.panes.viewId)) {
            attempt(() => panes.show(snapshot.panes.viewId));
            doc.body.classList.remove("chat-expanded"); doc.body.classList.add("panes-visible");
            doc.body.classList.toggle("conversation-split", panes.tabLayout);
            if ($("leave-tiles")) $("leave-tiles").hidden = panes.tabLayout;
            if ($("tile-threads")) $("tile-threads").textContent = panes.tabLayout ? "Tile threads" : "Retile threads";
            if ($("page-location")) $("page-location").textContent = panes.tabLayout ? "Conversation" : "Tiled threads";
          } else if (snapshot.selected === "new-session" && newDraft) {
            // newSession only opens a local composer; no create/send operation.
            if (s.selected !== "new-session") await Promise.resolve().then(() => newSession?.()).catch(error => { onError(error); attempt(() => goHome?.()); });
            else s.detail = s.newSessionDraft;
          } else if (typeof snapshot.selected === "string" && s.selected !== snapshot.selected) {
            await Promise.resolve().then(() => select?.(snapshot.selected, {tabId: snapshot.tabs?.selected, focus: false, refreshInventory: false, stayInProject: true}))
              .catch(error => { onError(error); attempt(() => goHome?.()); });
          } else if (s.selected && !s.detail && s.conversations?.get(s.selected)?.refreshError) {
            attempt(() => goHome?.());
          } else if (snapshot.selected === null && s.selected && !snapshot.navigation?.scratchpad) goHome?.();
        }
        if (s.selected && !panes?.active && !touched.has(currentDraftId(s)) && currentInput) {
          currentInput.value = s.drafts.get(s.selected) || "";
          if (snapshot.composer?.id === s.selected && Number.isFinite(snapshot.composer.selectionStart))
            currentInput.setSelectionRange(snapshot.composer.selectionStart, snapshot.composer.selectionEnd);
          const position = s.conversationPositions.get(s.selected);
          if (position?.following) s.timelineScroll?.latest();
          else if (Number.isFinite(position?.scroll)) s.timelineScroll?.restore(position.scroll);
        }
        // ThreadMentions.restore renders immediately and validates against the
        // visible input. Fill that input first or valid bindings get discarded.
        for (const [id, value] of entries(snapshot.mentions)) if (!touched.has(id) && typeof value?.text === "string")
          s.mentions?.restore(id, value.text, Array.isArray(value.refs) ? value.refs : []);
        if (!interacted && !panes?.active && typeof snapshot.chatExpanded === "boolean") {
          if (setChatExpanded) setChatExpanded(snapshot.chatExpanded);
          else doc.body.classList.toggle("chat-expanded", snapshot.chatExpanded);
        }
        attempt(() => render?.()); attempt(() => s.resizeComposer?.()); attempt(() => currentNavigation()?.remember?.());
        // Visible pane documents boot independently. The shell's attach signal
        // confirms restoreDraft ran; stacked, unopened tabs retain initialDraft.
        const awaiting = [];
        if (!interacted && snapshot.panes?.active && panes?.active) for (const id of panes.layout.tabs) {
          const record = panes.records.get(id);
          if (!record?.frame || record.api) continue;
          awaiting.push(new Promise((resolve, reject) => {
            const timeout = view.setTimeout(() => { paneWaiters.delete(id); reject(new Error("A restored conversation pane is still loading.")); }, 15000);
            paneWaiters.set(id, () => { view.clearTimeout(timeout); resolve(); });
          }));
        }
        await Promise.all(awaiting.map(task => task.catch(onError)));
        restored = true;
        return true;
      } catch (error) {
        // Draft maps were installed before navigation/rendering. A stale view
        // or deleted thread must not disable all later recovery saves.
        onError(error); restored = true; return true;
      } finally { restoring = false; installHooks(); changed(); }
    }
    bindDocument(doc);
    view.addEventListener("pagehide", flush);
    return {preload, capture, restore, changed, storageChanged, bindDocument, flush,
      busy() {
        const s = currentState();
        return Boolean(s?.sending?.size || view.agentCoordSendWork?.sending ||
          pairs(s?.attachments?.drafts).some(([, items]) => items.some(item => !item.url)) ||
          pairs(view.agentCoordPanes?.records).some(([, record]) => record.api?.canTransfer?.() === false &&
            !record.frame?.contentDocument?.querySelector("dialog[open]")));
      },
      ready() { ready = true; installHooks(); if (!pending) { restored = true; changed(); } }};
  }
  const api = {create, usefulStorageKey};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.agentCoordWorkspaceState = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
