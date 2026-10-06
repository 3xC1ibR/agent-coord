"use strict";

const threadViews = (() => {
  const defaults = {repository: "", project: "", phase: "", show: "active", search: ""};
  function filters(values = {}) {
    return Object.fromEntries(Object.entries(defaults).map(([key, value]) =>
      [key, typeof values[key] === "string" ? values[key].trim() : value]));
  }
  function matches(thread, values) {
    const f = filters(values);
    if ((thread.attention === "archived") !== (f.show === "archived")) return false;
    for (const field of ["repository", "project"]) {
      if (f[field] && (f[field] === "__none__" ? thread[field + "_id"] != null : thread[field + "_id"] !== f[field])) return false;
    }
    if (f.phase && thread.checkpoint?.phase !== f.phase) return false;
    if (f.show === "attention" && !needsYou(thread)) return false;
    if (f.show === "completed" && thread.response_state !== "completed") return false;
    const text = [thread.title, thread.repository_name, thread.repository_root, thread.project_name,
      thread.checkpoint?.summary, thread.checkpoint?.next_action].filter(Boolean).join(" ").toLocaleLowerCase();
    return !f.search || text.includes(f.search.toLocaleLowerCase());
  }
  function needsYou(thread) {
    return thread.attention === "now" && ["input", "reply", "failed"].includes(thread.response_state);
  }
  function badges(threads, values) {
    const visible = threads.filter(thread => matches(thread, values));
    return {attention: visible.filter(needsYou).length,
      completed: visible.some(t => t.attention !== "archived" && t.response_state === "completed" && (t.unread_result || t.unread))};
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
    try { this.navigation = JSON.parse(storage?.getItem(this.key) || "{}"); } catch { /* Optional window state. */ }
    if (!this.navigation || typeof this.navigation !== "object" || Array.isArray(this.navigation)) this.navigation = {};
    this.bind();
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
        option.textContent = f[field] === "__none__" ? "No " + field : "Selected " + field;
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
    try { this.storage?.setItem(this.key, JSON.stringify(this.navigation)); } catch { /* Optional window state. */ }
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
  }
  sync(items) {
    const dirty = this.dirty();
    this.items = [this.all, ...items];
    const exists = this.items.some(view => view.id === this.activeId);
    if (!exists || (!dirty && this.definition.version !== this.current().version)) {
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
    const target = this.items.find(view => view.id === id);
    if (!target) return;
    this.remember();
    const version = ++this.switchVersion;
    this.activeId = id; this.definition = target;
    const remembered = this.navigation[id];
    this.apply(!reset && remembered?.version === target.version ? remembered : target);
    if (reset) delete this.navigation[id];
    this.saveActive(); this.closeMenu(); this.render();
    const changing = this.onSwitch();
    this.focusActive();
    await changing;
    if (version === this.switchVersion) this.restoreScroll();
  }
  focusActive() {
    const tab = this.$("view-tabs").querySelector('[aria-selected="true"]');
    tab?.focus({preventScroll: true}); tab?.scrollIntoView({block: "nearest", inline: "nearest"});
  }
  closeMenu() { this.$("view-menu").open = false; this.$("filter-menu").open = false; }
  changed() {
    const dirty = this.dirty(), custom = this.activeId !== "all";
    this.$("reset-view").hidden = !dirty;
    this.$("update-view").hidden = !dirty || !custom;
    this.$("update-view").disabled = this.busy;
    this.$("view-menu").hidden = !custom;
    this.$("view-move-left").disabled = this.items.indexOf(this.current()) <= 1 || this.busy;
    this.$("view-move-right").disabled = this.current() === this.items.at(-1) || this.busy;
    this.$("back-home").textContent = this.current().name;
    this.$("overview-title").textContent = this.current().name + " overview";
  }
  render(threads = this.threads) {
    this.threads = threads;
    this.changed();
    const badges = this.items.map(view => threadViews.badges(threads, view.filters));
    const signature = JSON.stringify([this.items, this.activeId, badges]);
    if (signature === this.signature) return;
    this.signature = signature;
    const tabs = this.$("view-tabs"), focused = tabs.contains(this.doc.activeElement) ? this.doc.activeElement.dataset.viewId : null;
    tabs.replaceChildren();
    this.items.forEach((view, index) => {
      const button = this.doc.createElement("button"), counts = badges[index];
      button.type = "button"; button.className = "view-tab"; button.id = "saved-view-" + view.id; button.dataset.viewId = view.id;
      button.setAttribute("role", "tab"); button.setAttribute("aria-controls", "welcome");
      button.setAttribute("aria-selected", String(view.id === this.activeId)); button.tabIndex = view.id === this.activeId ? 0 : -1;
      const label = this.doc.createElement("span"); label.className = "view-tab-name"; label.textContent = view.name; button.append(label);
      button.title = view.name + " · " + counts.attention + " need you" + (counts.completed ? " · New completed results" : "");
      button.setAttribute("aria-label", button.title);
      if (counts.attention) {
        const badge = this.doc.createElement("span"); badge.className = "view-attention"; badge.textContent = String(counts.attention);
        badge.setAttribute("aria-hidden", "true"); button.append(badge);
      }
      if (counts.completed) {
        const dot = this.doc.createElement("span"); dot.className = "view-completed"; dot.setAttribute("aria-hidden", "true"); button.append(dot);
      }
      button.onclick = () => this.activate(view.id).catch(this.onError);
      tabs.append(button);
      if (focused === view.id) button.focus({preventScroll: true});
    });
    this.$("welcome").setAttribute("aria-labelledby", "saved-view-" + this.activeId);
  }
  async mutation(fn) {
    if (this.busy) return;
    this.busy = true; this.changed(); this.$("save-view").disabled = true;
    try { await fn(); }
    catch (error) { this.onError(error); }
    finally { this.busy = false; this.$("save-view").disabled = false; this.render(); }
  }
  openDialog(mode) {
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
    this.$("add-view").onclick = () => this.openDialog("create");
    this.$("reset-view").onclick = () => this.activate(this.activeId, true).catch(this.onError);
    this.$("update-view").onclick = () => this.mutation(async () => {
      const id = this.activeId, value = this.read();
      const updated = await this.api("views/" + encodeURIComponent(id), {...value, version: this.definition.version});
      this.items = this.items.map(view => view.id === id ? updated : view);
      if (this.activeId === id) this.definition = updated;
      this.remember();
    });
    for (const mode of ["rename", "duplicate"]) this.$("view-" + mode).onclick = () => this.openDialog(mode);
    for (const direction of ["left", "right"]) this.$("view-move-" + direction).onclick = () => this.mutation(async () => {
      const result = await this.api("views/" + encodeURIComponent(this.activeId) + "/move", {direction});
      this.sync(result.data); this.closeMenu(); this.render(); this.focusActive();
    });
    this.$("view-delete").onclick = () => this.mutation(async () => {
      const id = this.activeId;
      const result = await this.api("views/" + encodeURIComponent(id) + "/delete", {version: this.definition.version});
      this.items = [this.all, ...result.data];
      await this.activate("all");
      delete this.navigation[id]; this.remember();
    });
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
