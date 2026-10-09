"use strict";

const threadViews = (() => {
  const grouping = typeof module !== "undefined" ? require("./thread-groups.js") : threadGrouping;
  const defaults = {repository: "", project: "", phase: "", show: "active", search: ""};
  function filters(values = {}) {
    return Object.fromEntries(Object.entries(defaults).map(([key, value]) =>
      [key, typeof values[key] === "string" ? values[key].trim() : value]));
  }
  function matches(thread, values) {
    const f = filters(values);
    if ((thread.attention === "archived") !== (f.show === "archived")) return false;
    if (f.show === "later" ? thread.attention !== "later" : f.show !== "archived" && thread.attention !== "now") return false;
    for (const field of ["repository", "project"]) {
      if (f[field] && (f[field] === "__none__" ? thread[field + "_id"] != null : thread[field + "_id"] !== f[field])) return false;
    }
    if (f.phase && grouping.phase(thread) !== (f.phase === "discussion" ? "investigation" : f.phase)) return false;
    if (f.show === "attention" && !needsYou(thread)) return false;
    // Preserve the saved filter key, now presented as Done.
    if (f.show === "completed" && grouping.phase(thread) !== "finished") return false;
    const text = [thread.title, thread.repository_name, thread.repository_root, thread.project_name,
      thread.checkpoint?.summary, thread.checkpoint?.next_action].filter(Boolean).join(" ").toLocaleLowerCase();
    return !f.search || text.includes(f.search.toLocaleLowerCase());
  }
  function needsYou(thread) {
    return grouping.awaitsUser(thread);
  }
  function badges(threads, values) {
    const visible = threads.filter(thread => matches(thread, values));
    return {attention: visible.filter(needsYou).length,
      active: visible.some(t => t.attention !== "archived" && t.response_state === "working"),
      completed: visible.some(t => t.attention === "now" && t.unhandled_response && (t.unread_result || t.unread))};
  }
  function same(a, b) {
    return a.group_by === b.group_by && JSON.stringify(filters(a.filters)) === JSON.stringify(filters(b.filters));
  }
  function shortcut(event, index, count, inTabs = false) {
    if (event.isComposing || event.altKey || event.metaKey) return null;
    if (event.ctrlKey && event.shiftKey && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
      return (index + (event.key === "ArrowLeft" ? -1 : 1) + count) % count;
    }
    if (!inTabs || event.ctrlKey || event.shiftKey) return null;
    if (event.key === "Home") return 0;
    if (event.key === "End") return count - 1;
    if (["ArrowLeft", "ArrowRight"].includes(event.key)) return (index + (event.key === "ArrowLeft" ? -1 : 1) + count) % count;
    return null;
  }
  return {filters, matches, badges, same, shortcut};
})();

class SavedViews {
  constructor({document, api, onSwitch, onError, storage, preferences, scope = ""}) {
    this.doc = document; this.api = api; this.onSwitch = onSwitch; this.onError = onError;
    this.storage = storage; this.preferences = preferences;
    this.key = "agent-coord.views." + (scope || "all");
    this.all = {id: "all", name: "All work", filters: threadViews.filters(), group_by: this.$("group-by").value || "phase", version: 1};
    this.items = [this.all]; this.activeId = "all"; this.definition = this.all;
    this.navigation = {}; this.threads = []; this.switchVersion = 0; this.busy = false; this.signature = "";
    this.saves = new Map();
    try { this.navigation = JSON.parse(storage?.getItem(this.key) || "{}"); } catch { /* Optional window state. */ }
    if (!this.navigation || typeof this.navigation !== "object" || Array.isArray(this.navigation)) this.navigation = {};
    this.bind();
    this.setupMobile();
  }
  setupMobile() {
    this.mobile = this.doc.defaultView?.matchMedia("(max-width: 720px)");
    if (!this.mobile) return;
    const host = this.$("mobile-overview");
    this.doc.querySelector(".workspace-bar").insertBefore(host, this.$("connection"));
    const homes = ["welcome-new", "view-menu", "add-view", "reset-view", "update-view"].map(id => {
      const element = this.$(id), marker = this.doc.createComment("overview control position");
      element.before(marker);
      return {element, marker};
    });
    const sync = () => {
      this.closeMenu();
      if (this.mobile.matches) {
        host.append(this.$("welcome-new"), this.$("view-menu"));
        this.$("mobile-view-actions").append(this.$("add-view"), this.$("reset-view"), this.$("update-view"));
      } else {
        for (const {element, marker} of homes) marker.after(element);
      }
      this.changed();
    };
    this.mobile.addEventListener("change", sync);
    sync();
  }
  $(id) { return this.doc.getElementById(id); }
  current() { return this.items.find(view => view.id === this.activeId) || this.all; }
  read() {
    return {filters: threadViews.filters({repository: this.$("repository").value, project: this.$("project").value,
      phase: this.$("phase-filter").value, show: this.$("view").value, search: this.$("search").value}),
      group_by: this.$("group-by").value};
  }
  apply(value) {
    const f = threadViews.filters(value.filters);
    for (const field of ["repository", "project"]) {
      const control = this.$(field);
      // Preserve a saved ID while choices load, or if its association disappears.
      // Falling back to "All" would silently broaden a saved view.
      if (f[field] && ![...control.options].some(option => option.value === f[field])) {
        const option = this.doc.createElement("option"); option.value = f[field];
        option.textContent = f[field] === "__none__" ? "No " + (field === "project" ? "group" : field) : "Selected " + (field === "project" ? "group" : field);
        control.append(option);
      }
      control.value = f[field];
    }
    this.$("phase-filter").value = f.phase; this.$("view").value = f.show; this.$("search").value = f.search;
    this.$("group-by").value = ["phase", "repository", "project", "none"].includes(value.group_by) ? value.group_by : "phase";
  }
  dirty() { return !threadViews.same(this.read(), this.definition); }
  remember() {
    const old = this.navigation[this.activeId];
    this.navigation[this.activeId] = {...this.read(), version: this.definition.version,
      scroll: this.$("welcome").hidden ? old?.scroll || 0 : this.$("welcome").scrollTop};
    this.storeNavigation();
  }
  storeNavigation() {
    try { this.storage?.setItem(this.key, JSON.stringify(this.navigation)); } catch { /* Optional window state. */ }
  }
  effective(view) {
    if (view.id === this.activeId) return this.read();
    const pending = this.saves.get(view.id), remembered = this.navigation[view.id];
    return pending?.value || (remembered?.version === view.version ? remembered : view);
  }
  persist() {
    this.remember();
    return this.save(this.definition, this.read());
  }
  save(definition, value) {
    const id = definition.id;
    if (id === "all") { this.render(); return Promise.resolve(); }
    let pending = this.saves.get(id);
    if (!pending && threadViews.same(definition, value)) return Promise.resolve();
    if (!pending) {
      pending = {definition, value, promise: null, error: null};
      this.saves.set(id, pending);
    }
    pending.value = value;
    if (!pending.promise) {
      pending.error = null;
      // Serialize updates to each view and coalesce edits made during a request.
      // Keep the original version on failure so another window's edit is safe.
      pending.promise = Promise.resolve().then(async () => {
        while (!threadViews.same(pending.definition, pending.value)) {
          const updated = await this.api("views/" + encodeURIComponent(id),
            {filters: pending.value.filters, group_by: pending.value.group_by, version: pending.definition.version});
          pending.definition = updated;
          this.items = this.items.map(view => view.id === id ? updated : view);
          if (this.activeId === id) this.definition = updated;
          if (this.navigation[id]) this.navigation[id].version = updated.version;
          this.storeNavigation();
        }
      }).catch(async error => {
        pending.error = error;
        // Reset must use the latest definition after a version conflict.
        try { this.sync((await this.api("views")).data); } catch { /* Retry remains available offline. */ }
        this.onError(error);
      }).finally(() => {
        pending.promise = null;
        if (!pending.error) this.saves.delete(id);
        this.render();
      });
    }
    this.render();
    return pending.promise;
  }
  async start() {
    this.items = [this.all, ...(await this.api("views")).data];
    let active = "all";
    try { active = this.preferences?.getItem(this.key + ".active") || active; } catch { /* Optional preference. */ }
    this.activeId = this.items.some(view => view.id === active) ? active : "all";
    this.definition = this.current();
    const remembered = this.navigation[this.activeId];
    this.apply(remembered?.version === this.definition.version ? remembered : this.definition);
    this.render();
    // Retain filters remembered by older clients instead of silently widening
    // their named views on upgrade. Never replace a newer saved definition.
    await Promise.all(this.items.filter(view => view.id !== "all" && this.navigation[view.id]?.version === view.version)
      .map(view => this.save(view, this.navigation[view.id])));
  }
  sync(items) {
    const dirty = this.dirty();
    this.items = [this.all, ...items];
    const exists = this.items.some(view => view.id === this.activeId);
    if (!exists || (!dirty && !this.saves.has(this.activeId) && this.definition.version !== this.current().version)) {
      if (!exists) this.activeId = "all";
      this.definition = this.current(); this.apply(this.definition);
      this.saveActive();
      return true;
    }
    return false;
  }
  saveActive() {
    try { this.preferences?.setItem(this.key + ".active", this.activeId); } catch { /* Optional preference. */ }
  }
  restoreScroll() {
    const scroll = this.navigation[this.activeId]?.scroll;
    this.$("welcome").scrollTop = Number.isFinite(scroll) ? Math.max(0, scroll) : 0;
  }
  async activate(id, reset = false) {
    if (reset) await this.saves.get(id)?.promise;
    const target = this.items.find(view => view.id === id);
    if (!target) return;
    this.remember();
    const version = ++this.switchVersion;
    if (reset) { this.saves.delete(id); delete this.navigation[id]; }
    const pending = this.saves.get(id);
    this.activeId = id; this.definition = pending?.definition || target;
    const remembered = this.navigation[id];
    this.apply(pending?.value || (!reset && remembered?.version === target.version ? remembered : target));
    this.saveActive(); this.closeMenu(); this.render();
    const changing = this.onSwitch();
    this.focusActive();
    await changing;
    if (version === this.switchVersion) this.restoreScroll();
  }
  async overview(filters, group = "phase") {
    // Agent links are temporary All work filters. Never autosave them into the
    // named view the user happened to have open when the request arrived.
    this.remember();
    ++this.switchVersion;
    this.activeId = "all"; this.definition = this.all;
    this.apply({filters, group_by: group});
    this.remember(); this.saveActive(); this.closeMenu(); this.render();
    await this.onSwitch();
    this.$("welcome").scrollTop = 0;
  }
  focusActive() {
    if (this.mobile?.matches) { this.$("mobile-view").focus({preventScroll: true}); return; }
    const tab = this.$("view-tabs").querySelector('[aria-selected="true"]');
    tab?.focus({preventScroll: true}); tab?.scrollIntoView({block: "nearest", inline: "nearest"});
  }
  closeMenu() { this.$("view-menu").open = false; this.$("filter-menu").open = false; }
  changed() {
    const dirty = this.dirty(), custom = this.activeId !== "all";
    const pending = this.saves.get(this.activeId);
    this.$("reset-view").hidden = !dirty || (custom && !pending?.error);
    this.$("update-view").hidden = !dirty || !custom;
    this.$("update-view").textContent = pending?.promise ? "Saving…" : pending?.error ? "Retry saving" : "Save filters";
    this.$("update-view").disabled = this.busy || Boolean(pending?.promise);
    this.$("view-menu").hidden = !custom && !this.mobile?.matches;
    this.$("view-custom-actions").hidden = !custom;
    const f = this.read().filters;
    const count = [f.repository, f.project, f.phase, ["attention", "completed"].includes(f.show)].filter(Boolean).length;
    this.$("filter-count").textContent = String(count);
    this.$("filter-count").hidden = !count;
    this.$("view-move-left").disabled = this.items.indexOf(this.current()) <= 1 || this.busy;
    this.$("view-move-right").disabled = this.current() === this.items.at(-1) || this.busy;
    this.$("back-home").textContent = this.current().name;
    this.$("overview-title").textContent = this.current().name + " overview";
  }
  render(threads = this.threads) {
    this.threads = threads;
    this.changed();
    const badges = this.items.map(view => threadViews.badges(threads, this.effective(view).filters));
    const signature = JSON.stringify([this.items, this.activeId, badges]);
    if (signature === this.signature) return;
    this.signature = signature;
    const picker = this.$("mobile-view");
    picker.replaceChildren();
    this.items.forEach((view, index) => {
      const option = this.doc.createElement("option"), counts = badges[index];
      option.value = view.id;
      option.textContent = view.name + (view.id === this.activeId ? "" :
        (counts.attention ? " · " + counts.attention + " need attention" : "") +
        (counts.active ? " · Working" : counts.completed ? " · New responses" : ""));
      picker.append(option);
    });
    picker.value = this.activeId;
    const counts = badges[this.items.indexOf(this.current())];
    const attention = this.$("mobile-view-attention"), activity = this.$("mobile-view-activity");
    attention.textContent = String(counts.attention); attention.hidden = !counts.attention;
    attention.setAttribute("aria-label", counts.attention + " need attention");
    activity.hidden = !counts.active && !counts.completed;
    activity.className = counts.active ? "view-active" : "view-completed";
    activity.setAttribute("aria-label", counts.active ? "Active work" : "New responses");
    const tabs = this.$("view-tabs"), focused = tabs.contains(this.doc.activeElement) ? this.doc.activeElement.dataset.viewId : null;
    tabs.replaceChildren();
    this.items.forEach((view, index) => {
      const button = this.doc.createElement("button"), counts = badges[index];
      button.type = "button"; button.className = "view-tab"; button.id = "saved-view-" + view.id; button.dataset.viewId = view.id;
      button.setAttribute("role", "tab"); button.setAttribute("aria-controls", "welcome");
      button.setAttribute("aria-selected", String(view.id === this.activeId)); button.tabIndex = view.id === this.activeId ? 0 : -1;
      const label = this.doc.createElement("span"); label.className = "view-tab-name"; label.textContent = view.name; button.append(label);
      button.title = view.name + " · " + counts.attention + " in attention" +
        (counts.active ? " · Active work" : "") + (counts.completed ? " · New responses" : "");
      button.setAttribute("aria-label", button.title);
      if (counts.attention) {
        const badge = this.doc.createElement("span"); badge.className = "view-attention"; badge.textContent = String(counts.attention);
        badge.setAttribute("aria-hidden", "true"); button.append(badge);
      }
      if (counts.active || counts.completed) {
        const dot = this.doc.createElement("span"); dot.className = counts.active ? "view-active" : "view-completed";
        dot.setAttribute("aria-hidden", "true"); button.append(dot);
      }
      button.onclick = () => this.activate(view.id).catch(this.onError);
      tabs.append(button);
      if (focused === view.id) button.focus({preventScroll: true});
    });
    this.$("welcome").setAttribute("aria-labelledby", "overview-title");
  }
  async mutation(fn) {
    if (this.busy) return;
    this.busy = true; this.changed(); this.$("save-view").disabled = true;
    try { await Promise.all([...this.saves.values()].map(pending => pending.promise)); await fn(); }
    catch (error) { this.onError(error); }
    finally { this.busy = false; this.$("save-view").disabled = false; this.render(); }
  }
  async openDialog(mode) {
    const id = this.activeId;
    await this.saves.get(id)?.promise;
    if (this.activeId !== id) return;
    this.closeMenu();
    // Capture the target and definition so polling or navigation cannot retarget a save.
    const view = this.current();
    this.edit = {mode, id: view.id, version: this.definition.version,
      value: mode === "duplicate" ? {filters: view.filters, group_by: view.group_by} : this.read()};
    this.$("view-dialog-title").textContent = mode === "rename" ? "Rename view" : mode === "duplicate" ? "Duplicate view" : "Save view";
    this.$("view-dialog-description").textContent = mode === "rename" ? "Give this view a new name."
      : mode === "duplicate" ? "Create a view with the same saved filters and grouping." : "Save the current filters and grouping as a tab.";
    this.$("view-name").value = mode === "rename" ? view.name : mode === "duplicate" ? view.name + " copy" : "";
    this.$("view-error").hidden = true;
    this.$("view-dialog").showModal(); this.$("view-name").focus();
  }
  bind() {
    this.$("mobile-view").onchange = () => this.activate(this.$("mobile-view").value).catch(this.onError);
    this.$("add-view").onclick = () => this.openDialog("create").catch(this.onError);
    this.$("reset-view").onclick = () => this.activate(this.activeId, true).catch(this.onError);
    this.$("update-view").onclick = () => this.persist();
    for (const mode of ["rename", "duplicate"]) this.$("view-" + mode).onclick = () => this.openDialog(mode).catch(this.onError);
    for (const direction of ["left", "right"]) this.$("view-move-" + direction).onclick = () => {
      const id = this.activeId;
      return this.mutation(async () => {
        const result = await this.api("views/" + encodeURIComponent(id) + "/move", {direction});
        this.sync(result.data); this.closeMenu(); this.render(); this.focusActive();
      });
    };
    this.$("view-delete").onclick = () => {
      const id = this.activeId, definition = this.definition, pending = this.saves.get(id);
      return this.mutation(async () => {
        const result = await this.api("views/" + encodeURIComponent(id) + "/delete",
          {version: (pending?.definition || definition).version});
        this.items = [this.all, ...result.data];
        if (this.activeId === id) await this.activate("all");
        this.saves.delete(id); delete this.navigation[id]; this.remember();
      });
    };
    this.$("view-form").onsubmit = event => {
      event.preventDefault();
      return this.mutation(async () => {
        const edit = this.edit, name = this.$("view-name").value.trim();
        if (!name) throw new Error("Enter a view name.");
        const result = await this.api(edit.mode === "rename" ? "views/" + encodeURIComponent(edit.id) : "views",
          edit.mode === "rename" ? {name, version: edit.version} : {name, ...edit.value});
        if (edit.mode === "rename") {
          this.items = this.items.map(view => view.id === result.id ? result : view);
          if (this.activeId === result.id) this.definition = {...this.definition, name: result.name, version: result.version};
        } else this.items.push(result);
        this.$("view-dialog").close();
        if (edit.mode !== "rename") await this.activate(result.id, true);
        else this.focusActive();
      });
    };
    this.doc.addEventListener("keydown", event => {
      if (this.doc.querySelector("dialog[open]") || this.doc.body.classList.contains("navigation-open")) return;
      const tabs = this.$("view-tabs");
      const index = threadViews.shortcut(event, this.items.indexOf(this.current()), this.items.length, tabs.contains(event.target));
      if (index !== null && !event.target.closest("input, textarea, select, [contenteditable]")) {
        event.preventDefault(); this.activate(this.items[index].id).catch(this.onError);
      }
      if (event.key === "Escape" && this.$("view-menu").open) {
        event.preventDefault(); this.closeMenu(); this.$("view-menu-toggle").focus();
      }
    });
    this.doc.addEventListener("pointerdown", event => {
      if (!this.$("view-menu").contains(event.target)) this.$("view-menu").open = false;
    });
  }
}

if (typeof module !== "undefined") module.exports = {threadViews, SavedViews};
