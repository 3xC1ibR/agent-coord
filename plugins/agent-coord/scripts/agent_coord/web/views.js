"use strict";

const threadViews = (() => {
  const grouping = typeof module !== "undefined" ? require("./thread-groups.js") : threadGrouping;
  const overview = typeof module !== "undefined" ? require("./working-set.js") : WorkingSet;
  const defaults = {repository: "", project: "", phase: "", show: "active", search: ""};
  function filters(values = {}) {
    return Object.fromEntries(Object.entries(defaults).map(([key, value]) =>
      [key, typeof values[key] === "string" ? values[key].trim() : value]));
  }
  function matches(thread, values) {
    const f = filters(values);
    if ((thread.attention === "archived") !== (f.show === "archived")) return false;
    if (f.show === "later" ? thread.attention !== "later" : f.show !== "archived" && thread.attention !== "now" && !needsYou(thread)) return false;
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
  function needsAction(thread) {
    return needsYou(thread) && ["blocked", "review"].includes(grouping.reason(thread).key);
  }
  function badges(threads, values, {layout, group, workspace = "all", now = Date.now() / 1000} = {}) {
    if (layout === "list") {
      const f = filters(values);
      // Group pages retain the other groups so orchestration children inherit
      // their owner's group exactly as they do in the List overview.
      const visible = threads.filter(thread => matches(thread, group ? {...f, project: ""} : f));
      const {sections} = overview.listSections(visible, {workspace, now, view: f.show,
        pageKey: group ? "group:" + group : "overview:all"});
      return {attention: sections.attention.length,
        action: sections.attention.some(t => needsAction(t) || ["approval", "question", "needs-you", "failed"].includes(overview.requestOf(t))),
        active: sections.working.length > 0,
        completed: sections.attention.some(t => t.unread_result || t.unread)};
    }
    const visible = threads.filter(thread => matches(thread, values));
    const visibleIds = new Set(visible.map(thread => thread.thread_id));
    return {attention: visible.filter(thread => needsYou(thread) && !visibleIds.has(thread.attention_grouped_under)).length,
      action: visible.some(needsAction),
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
  return {filters, matches, badges, needsAction, same, shortcut};
})();

class SavedViews {
  constructor({document, api, onSwitch, onRefresh, onError, notify, storage, preferences, scope = "", getLayout = () => "cards"}) {
    this.doc = document; this.api = api; this.onSwitch = onSwitch; this.onError = onError;
    this.getLayout = getLayout;
    this.onRefresh = onRefresh;
    this.notify = notify;
    this.storage = storage; this.preferences = preferences;
    this.key = "agent-coord.views." + (scope || "all");
    this.baseKey = this.key;
    this.all = {id: "all", name: "All", filters: threadViews.filters(), group_by: this.$("group-by").value || "phase", version: 1};
    this.items = [this.all]; this.groups = []; this.saved = []; this.activeId = "all"; this.definition = this.all;
    this.navigation = {}; this.threads = []; this.switchVersion = 0; this.busy = false; this.signature = "";
    this.saves = new Map(); this.groupOrders = new Map(); this.hiddenGroups = new Map();
    try { this.navigation = JSON.parse(storage?.getItem(this.key) || "{}"); } catch { /* Optional window state. */ }
    if (!this.navigation || typeof this.navigation !== "object" || Array.isArray(this.navigation)) this.navigation = {};
    this.bind();
    this.setupLayout();
  }
  setupLayout() {
    this.mobile = this.doc.defaultView?.matchMedia("(max-width: 720px)");
    if (!this.mobile) return;
    const host = this.$("mobile-overview");
    const hostHome = this.doc.createComment("project view selector"); host.before(hostHome);
    const bar = this.doc.querySelector(".views-bar"), tools = this.doc.querySelector(".workspace-tools");
    const navigation = this.doc.createElement("div"); navigation.className = "view-navigation";
    const utilities = this.doc.createElement("div"); utilities.className = "view-utilities";
    bar.prepend(navigation); bar.append(utilities);
    const placement = this.doc.querySelector(".placement-switch"), search = this.doc.querySelector(".search-field");
    const filters = this.doc.querySelector(".view-filters"), actions = this.doc.querySelector(".workspace-actions");
    const elements = ["welcome-new", "view-menu", "groups-menu", "add-group", "reset-view", "update-view", "saved-view-tabs",
      "tile-threads", "leave-tiles", "group-by-domain", "start-dispatcher", "send-work-mobile-open"].map(id => this.$(id));
    const viewPicker = host.querySelector(".mobile-view-picker");
    const homes = [...elements, viewPicker, placement, search, filters, actions].map(element => {
      const marker = this.doc.createComment("overview control position");
      element.before(marker);
      return {element, marker};
    });
    const sync = () => {
      this.closeMenu();
      for (const {element, marker} of homes) marker.after(element);
      bar.classList.toggle("unified-views", !this.mobile.matches);
      tools.classList.toggle("tools-in-header", !this.mobile.matches);
      if (this.mobile.matches) {
        this.doc.querySelector(".workspace-bar").insertBefore(host, this.$("connection"));
        // Phones keep one header row: project, Send work, New, and options. Group choice lives in Filters.
        host.append(this.$("send-work-mobile-open"), this.$("welcome-new"), this.$("view-menu"));
        this.$("mobile-view-actions").append(this.$("add-group"), this.$("reset-view"), this.$("update-view"));
        this.doc.querySelector(".filter-panel").prepend(viewPicker, this.$("groups-menu"), this.$("group-by-domain"));
      } else {
        hostHome.after(host);
        navigation.append(this.$("saved-view-tabs"), this.$("groups-menu"), this.$("add-group"), this.$("view-menu"), placement,
          this.$("reset-view"), this.$("update-view"));
        utilities.append(search, filters, this.$("tile-threads"), this.$("leave-tiles"));
        this.doc.querySelector(".filter-panel").prepend(this.$("group-by-domain"));
        const header = this.doc.querySelector(".workspace-bar");
        header.insertBefore(actions, this.$("connection"));
        const menu = header.querySelector(".workspace-switcher-panel");
        if (menu) menu.append(this.$("start-dispatcher"));
      }
      this.changed();
    };
    this.mobile.addEventListener("change", sync);
    sync();
  }
  $(id) { return this.doc.getElementById(id); }
  scoped(items) { return items.filter(view => this.cwd === undefined || view.cwd === this.cwd); }
  local(view) { return view.id === "all" || view.kind === "group"; }
  groupVisibilityKey(cwd = this.cwd) {
    return (cwd === undefined ? this.baseKey : this.baseKey + ":project:" + (cwd || "")) + ".hidden-groups";
  }
  hiddenGroupIds(cwd = this.cwd) {
    const key = this.groupVisibilityKey(cwd);
    if (!this.hiddenGroups.has(key)) {
      let ids = [];
      try { ids = JSON.parse(this.preferences?.getItem(key) || "[]"); } catch { /* Optional preference. */ }
      this.hiddenGroups.set(key, new Set(Array.isArray(ids) ? ids.filter(id => typeof id === "string") : []));
    }
    return this.hiddenGroups.get(key);
  }
  isGroupHidden(id, cwd = this.cwd) { return this.hiddenGroupIds(cwd).has(id); }
  storeHiddenGroups(cwd = this.cwd) {
    try { this.preferences?.setItem(this.groupVisibilityKey(cwd), JSON.stringify([...this.hiddenGroupIds(cwd)])); }
    catch { /* Keep the choice in this window when storage is unavailable. */ }
  }
  visibleTab(view) { return this.local(view) && !this.isGroupHidden(view.projectId); }
  showGroup(id, cwd = this.cwd) {
    if (!id || !this.hiddenGroupIds(cwd).delete(id)) return;
    this.storeHiddenGroups(cwd);
    if (cwd === this.cwd) this.render();
  }
  renderGroups() {
    const views = this.groupItems(), signature = JSON.stringify(views.map(view =>
      [view.projectId, view.name, this.isGroupHidden(view.projectId)]));
    if (signature === this.groupsSignature) return;
    this.groupsSignature = signature;
    const groups = this.$("groups-list");
    groups.replaceChildren();
    for (const view of views) {
      const hidden = this.isGroupHidden(view.projectId), button = this.doc.createElement("button");
      button.type = "button"; button.dataset.groupId = view.projectId;
      const name = this.doc.createElement("span"); name.textContent = view.name;
      const action = this.doc.createElement("span"); action.className = "group-visibility-action";
      action.textContent = hidden ? "Show in tabs" : "Open";
      button.append(name); button.append(action);
      button.setAttribute("aria-label", view.name + (hidden ? " · Hidden · Show in tabs" : " · Open group"));
      button.onclick = () => {
        if (hidden) { this.closeMenu(); this.showGroup(view.projectId); this.$("groups-menu-toggle").focus(); }
        else this.activate(view.id).catch(this.onError);
      };
      groups.append(button);
    }
    this.$("groups-empty").hidden = views.length > 0;
  }
  async hideGroup(view) {
    if (this.busy || view?.kind !== "group") return;
    const cwd = this.cwd, active = this.activeId === view.id;
    this.remember(); this.closeMenu();
    this.hiddenGroupIds().add(view.projectId); this.storeHiddenGroups();
    if (active) await this.activate("all", true);
    else { this.render(); this.focusActive(); }
    const version = this.switchVersion;
    this.notify?.("hide-group:" + view.projectId, "“" + view.name + "” hidden from tabs.", async () => {
      this.showGroup(view.projectId, cwd);
      if (active && this.cwd === cwd && this.switchVersion === version) await this.activate(view.id);
    });
  }
  groupItems() {
    if (!this.groupOrders.has(this.key)) {
      let order = [];
      try { order = JSON.parse(this.preferences?.getItem(this.key + ".group-order") || "[]"); } catch { /* Optional preference. */ }
      this.groupOrders.set(this.key, Array.isArray(order) ? order.filter(id => typeof id === "string") : []);
    }
    const rank = new Map(this.groupOrders.get(this.key).map((id, index) => [id, index]));
    return this.groups.filter(group => this.cwd === undefined || group.cwd === this.cwd)
      .map(group => ({id: "group-" + group.id, kind: "group", projectId: group.id, name: group.name,
        cwd: this.cwd, version: 1, filters: threadViews.filters({project: group.id}), group_by: "phase"}))
      .sort((a, b) => (rank.get(a.id) ?? Infinity) - (rank.get(b.id) ?? Infinity));
  }
  moveGroup(id, targetId, after = false) {
    const order = this.groupItems().map(view => view.id);
    if (this.busy || id === targetId || !order.includes(id) || !order.includes(targetId)) return;
    order.splice(order.indexOf(id), 1);
    order.splice(order.indexOf(targetId) + Number(after), 0, id);
    this.groupOrders.set(this.key, order);
    try { this.preferences?.setItem(this.key + ".group-order", JSON.stringify(order)); } catch { /* Keep the order in this window. */ }
    this.rebuild(); this.render();
  }
  clearGroupDrag() {
    this.dragGroup = null;
    for (const tab of this.$("saved-view-tabs").children) {
      delete tab.dataset.groupDrop; delete tab.dataset.groupDragging;
    }
  }
  bindGroupDrag(tab, view) {
    tab.draggable = true;
    tab.title += " · Drag to reorder";
    tab.ondragstart = event => {
      if (this.busy) { event.preventDefault(); return; }
      this.closeMenu(); this.dragGroup = {id: view.id, key: this.key};
      tab.dataset.groupDragging = "true";
      event.dataTransfer.setData("application/x-ribbon-field-group", view.id);
      event.dataTransfer.effectAllowed = "move";
    };
    tab.ondragover = event => {
      if (!this.dragGroup || this.dragGroup.key !== this.key || this.dragGroup.id === view.id) return;
      event.preventDefault(); event.dataTransfer.dropEffect = "move";
      const rect = tab.getBoundingClientRect();
      for (const other of this.$("saved-view-tabs").children) delete other.dataset.groupDrop;
      tab.dataset.groupDrop = event.clientX >= rect.left + rect.width / 2 ? "after" : "before";
    };
    tab.ondragleave = () => { delete tab.dataset.groupDrop; };
    tab.ondrop = event => {
      const source = this.dragGroup;
      if (!source || source.key !== this.key) return;
      event.preventDefault(); event.stopPropagation();
      const rect = tab.getBoundingClientRect(), after = event.clientX >= rect.left + rect.width / 2;
      this.clearGroupDrag(); this.moveGroup(source.id, view.id, after);
    };
    tab.ondragend = () => { this.clearGroupDrag(); this.render(); };
  }
  rebuild() { this.items = [this.all, ...this.groupItems(), ...this.scoped(this.saved)]; }
  async openGroup(id) { return this.activate(id ? "group-" + id : "all"); }
  async setProject(cwd) {
    this.clearGroupDrag();
    this.remember();
    const version = ++this.switchVersion;
    await Promise.all([...this.saves.values()].map(pending => pending.promise));
    if (version !== this.switchVersion) return;
    this.cwd = cwd || "";
    this.key = this.baseKey + ":project:" + this.cwd;
    this.navigation = {};
    try { this.navigation = JSON.parse(this.storage?.getItem(this.key) || "{}"); } catch { /* Optional window state. */ }
    if (!this.navigation || typeof this.navigation !== "object" || Array.isArray(this.navigation)) this.navigation = {};
    this.activeId = "all"; this.definition = this.all;
    // Definitions are shared across projects and refreshed by the inventory.
    // Switching only needs this project's local selection and filter state.
    await this.start({refresh: !this.loaded});
  }
  current() { return this.items.find(view => view.id === this.activeId) || this.all; }
  read() {
    return {filters: threadViews.filters({repository: this.$("repository").value,
      project: this.current().kind === "group" ? this.current().projectId : this.$("project").value,
      phase: this.$("phase-filter").value, show: this.$("view").value, search: this.$("search").value}),
      group_by: this.$("group-by").value};
  }
  apply(value) {
    const f = threadViews.filters(value.filters);
    if (this.current().kind === "group") f.project = this.current().projectId;
    if (this.cwd !== undefined) f.repository = "";
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
    this.$("group-by").value = ["phase", "domain", "project", "none"].includes(value.group_by) ? value.group_by : "phase";
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
    // Groups remember their display preferences without a second named object.
    for (const view of this.items.filter(view => this.local(view))) {
      if (!this.navigation[view.id]) continue;
      const {filters, group_by, version} = this.navigation[view.id];
      try { this.preferences?.setItem(this.key + "." + view.id, JSON.stringify({filters, group_by, version})); }
      catch { /* Optional preference. */ }
    }
  }
  effective(view) {
    if (view.id === this.activeId) return this.read();
    const pending = this.saves.get(view.id), remembered = this.navigation[view.id];
    return pending?.value || (remembered?.version === view.version ? remembered : view);
  }
  persist() {
    if (this.current().kind === "group") this.$("project").value = this.current().projectId;
    this.remember();
    return this.save(this.definition, this.read());
  }
  save(definition, value) {
    const id = definition.id;
    if (this.local(definition)) { this.render(); return Promise.resolve(); }
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
          this.saved = this.saved.map(view => view.id === id ? updated : view);
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
  async start({refresh = true} = {}) {
    const version = this.switchVersion;
    if (refresh) {
      const [views, organization] = await Promise.all([this.api("views"), this.api("organization")]);
      if (version !== this.switchVersion) return;
      this.saved = views.data; this.groups = organization.projects;
      this.loaded = true;
    }
    this.rebuild();
    for (const view of this.items.filter(view => this.local(view))) {
      try {
        const remembered = JSON.parse(this.preferences?.getItem(this.key + "." + view.id) || "null");
        if (!this.navigation[view.id] && remembered?.version === view.version) this.navigation[view.id] = remembered;
      } catch { /* Optional preference; existing window state takes precedence. */ }
    }
    let active = "all";
    try { active = this.preferences?.getItem(this.key + ".active") || active; } catch { /* Optional preference. */ }
    this.activeId = this.items.some(view => view.id === active && !this.isGroupHidden(view.projectId)) ? active : "all";
    this.definition = this.current();
    const remembered = this.navigation[this.activeId];
    this.apply(remembered?.version === this.definition.version ? remembered : this.definition);
    this.render();
    // Retain filters remembered by older clients instead of silently widening
    // their named views on upgrade. Never replace a newer saved definition.
    await Promise.all(this.items.filter(view => !this.local(view) && this.navigation[view.id]?.version === view.version)
      .map(view => this.save(view, this.navigation[view.id])));
  }
  sync(items, groups = this.groups) {
    const dirty = this.dirty();
    this.saved = items; this.groups = groups; this.rebuild();
    const exists = this.items.some(view => view.id === this.activeId);
    if (!exists || (!dirty && !this.saves.has(this.activeId) && this.definition.version !== this.current().version)) {
      if (!exists) this.activeId = "all";
      this.definition = this.current(); this.apply(this.definition);
      this.saveActive();
      return true;
    }
    if (this.current().kind === "group") this.definition = this.current();
    return false;
  }
  saveActive() {
    try { this.preferences?.setItem(this.key + ".active", this.activeId); } catch { /* Optional preference. */ }
  }
  restoreScroll() {
    const scroll = this.navigation[this.activeId]?.scroll;
    this.$("welcome").scrollTop = Number.isFinite(scroll) ? Math.max(0, scroll) : 0;
  }
  async activate(id, reset = false, {revealGroup = true} = {}) {
    if (reset) await this.saves.get(id)?.promise;
    const target = this.items.find(view => view.id === id);
    if (!target) return;
    if (!revealGroup && target.kind === "group" && this.isGroupHidden(target.projectId)) return this.activate("all");
    // Explicit group navigation restores a tab; inventory updates and opening
    // individual conversations never call this path.
    if (target.kind === "group") this.showGroup(target.projectId);
    this.remember();
    const version = ++this.switchVersion;
    if (reset) { this.saves.delete(id); delete this.navigation[id]; }
    const pending = this.saves.get(id);
    this.activeId = id; this.definition = pending?.definition || target;
    const remembered = this.navigation[id];
    this.apply(pending?.value || (!reset && remembered?.version === target.version ? remembered : target));
    if (reset) {
      this.navigation[id] = {...this.read(), version: this.definition.version, scroll: 0};
      this.storeNavigation();
    }
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
    if (this.mobile?.matches) this.$("mobile-view").focus({preventScroll: true});
    else if (this.local(this.current()) && !this.$("saved-view-tabs").hidden) {
      const tab = this.$("saved-view-tabs").querySelector('[aria-selected="true"]');
      tab?.focus({preventScroll: true});
      tab?.scrollIntoView({block: "nearest", inline: "nearest"});
    } else this.$("filter-toggle").focus({preventScroll: true});
  }
  closeMenu() {
    this.$("view-menu").open = false; this.$("filter-menu").open = false;
    this.$("groups-menu").open = false; this.closeGroupMenu();
  }
  closeGroupMenu(focus = false) {
    this.$("group-context-menu").hidden = true;
    if (focus) this.groupMenuTrigger?.focus();
    this.contextGroup = null;
  }
  openGroupMenu(view, trigger, event) {
    event.preventDefault(); event.stopPropagation();
    this.closeMenu(); this.contextGroup = view; this.groupMenuTrigger = trigger;
    const menu = this.$("group-context-menu"), rect = trigger.getBoundingClientRect();
    menu.hidden = false;
    const bounds = menu.getBoundingClientRect(), win = this.doc.defaultView;
    const x = event.type === "contextmenu" ? event.clientX : rect.left;
    const y = event.type === "contextmenu" ? event.clientY : rect.bottom;
    menu.style.left = Math.max(8, Math.min(x, (win?.innerWidth || 1024) - bounds.width - 8)) + "px";
    menu.style.top = Math.max(8, Math.min(y, (win?.innerHeight || 768) - bounds.height - 8)) + "px";
    this.$("context-rename-group").focus();
  }
  async requestGroupRemoval(view) {
    if (!view?.projectId || this.busy) return;
    const cwd = this.cwd, request = this.groupRemovalRequest = (this.groupRemovalRequest || 0) + 1;
    this.closeMenu();
    const group = await this.api("projects/" + encodeURIComponent(view.projectId));
    if (cwd !== this.cwd || request !== this.groupRemovalRequest) return;
    if (!group.thread_count) return this.removeGroup(group);
    this.removeTarget = group;
    this.$("remove-group-title").textContent = "Delete “" + group.name + "”?";
    this.$("remove-group-description").textContent = "Its " + group.thread_count +
      (group.thread_count === 1 ? " conversation will" : " conversations will") +
      " become ungrouped. All conversations will be kept, and running work will continue.";
    this.$("remove-group-error").hidden = true;
    this.$("remove-group-dialog").showModal(); this.$("cancel-remove-group").focus();
  }
  async removeGroup(group) {
    return this.mutation(async () => {
      this.$("confirm-remove-group").disabled = true;
      try {
        await this.api("projects/" + encodeURIComponent(group.id) + "/delete", {thread_count: group.thread_count});
        const id = "group-" + group.id, active = this.activeId === id;
        this.groups = this.groups.filter(item => item.id !== group.id);
        this.items = this.items.filter(item => item.id !== id);
        this.hiddenGroupIds().delete(group.id); this.storeHiddenGroups();
        delete this.navigation[id]; this.storeNavigation();
        this.removeTarget = null; this.$("remove-group-dialog").close();
        if (active) await this.activate("all", true);
        else { await this.onRefresh?.(); this.render(); this.focusActive(); }
      } catch (error) {
        if (!this.$("remove-group-dialog").open) throw error;
        this.$("remove-group-error").textContent = error.message;
        this.$("remove-group-error").hidden = false;
      } finally { this.$("confirm-remove-group").disabled = false; }
    });
  }
  changed() {
    const dirty = this.dirty(), custom = !this.local(this.current()), group = this.current().kind === "group";
    const pending = this.saves.get(this.activeId);
    this.$("reset-view").hidden = !dirty || (custom && !pending?.error);
    this.$("update-view").hidden = !dirty || !custom;
    this.$("update-view").textContent = pending?.promise ? "Saving…" : pending?.error ? "Retry saving" : "Save filters";
    this.$("update-view").disabled = this.busy || Boolean(pending?.promise);
    this.$("view-menu").hidden = !custom && !group && !this.mobile?.matches;
    this.$("view-custom-actions").hidden = !custom;
    this.$("group-rename").hidden = !group;
    this.$("group-hide").hidden = !group;
    this.$("group-hide").disabled = this.busy;
    this.$("group-remove").hidden = this.current().kind !== "group";
    this.$("group-remove").disabled = this.busy;
    const f = this.read().filters;
    const count = [f.repository, !group && f.project, f.phase, ["attention", "completed"].includes(f.show)].filter(Boolean).length;
    this.$("filter-count").textContent = String(count);
    this.$("filter-count").hidden = !count;
    this.$("filter-label").textContent = custom ? this.current().name : "Filters";
    const saved = this.items.filter(view => !this.local(view));
    this.$("view-move-left").disabled = saved.indexOf(this.current()) <= 0 || this.busy;
    this.$("view-move-right").disabled = this.current() === saved.at(-1) || this.busy;
    this.$("back-home").textContent = this.cwd ? this.cwd.split("/").filter(Boolean).pop() || "/" : this.cwd === "" ? "Open project" : this.current().name;
    this.$("back-home").title = this.cwd || "";
    this.$("add-view").disabled = this.cwd === "";
    this.$("add-group").disabled = this.cwd === "";
    this.$("mobile-view").disabled = this.cwd === "";
    this.$("saved-view-tabs").hidden = this.cwd === "";
    this.$("overview-title").textContent = this.current().name + " overview";
    if (this.$("show-closed")) this.$("show-closed").title = "Show closed threads";
    for (const [id, show] of [["show-now", "active"], ["show-later", "later"], ["show-closed", "archived"]]) {
      const active = show === "active" ? !["later", "archived"].includes(f.show) : f.show === show;
      this.$(id)?.setAttribute("aria-pressed", String(active));
    }
  }
  render(threads = this.threads) {
    this.threads = threads;
    // Inventory polling must not replace the native drag source mid-gesture.
    if (this.dragGroup?.key === this.key) return;
    this.changed();
    const layout = this.getLayout(), now = Date.now() / 1000;
    const badges = this.items.map(view => threadViews.badges(threads, this.effective(view).filters,
      {layout, now, workspace: this.cwd || "all", group: view.kind === "group" ? view.projectId : null}));
    const signature = JSON.stringify([this.items, this.activeId, badges, [...this.hiddenGroupIds()]]);
    if (signature === this.signature) return;
    this.signature = signature;
    const tabs = this.$("saved-view-tabs"), hadFocus = tabs.contains(this.doc.activeElement);
    tabs.replaceChildren();
    const picker = this.$("mobile-view");
    picker.replaceChildren();
    this.renderGroups();
    const savedPicker = this.$("saved-filter");
    const placeholder = this.doc.createElement("option"); placeholder.value = ""; placeholder.textContent = "Choose saved filters…";
    savedPicker.replaceChildren(placeholder);
    this.items.forEach((view, index) => {
      const option = this.doc.createElement("option"), counts = badges[index];
      option.value = view.id;
      option.textContent = (this.local(view) ? "" : "Saved filters: ") + view.name +
        (this.isGroupHidden(view.projectId) ? " · Hidden" : "") + (view.id === this.activeId ? "" :
        (counts.attention ? " · " + counts.attention + " need attention" : "") +
        (counts.active ? " · Working" : counts.completed ? " · New responses" : ""));
      picker.append(option);
      if (!this.local(view)) {
        const savedOption = this.doc.createElement("option"); savedOption.value = view.id; savedOption.textContent = view.name;
        savedPicker.append(savedOption); return;
      }
      if (!this.visibleTab(view)) return;
      const tab = this.doc.createElement("button"), selected = view.id === this.activeId;
      tab.type = "button"; tab.className = "saved-view-tab"; tab.dataset.viewId = view.id;
      tab.setAttribute("role", "tab"); tab.setAttribute("aria-selected", String(selected));
      tab.setAttribute("aria-controls", "welcome"); tab.tabIndex = selected || (!this.local(this.current()) && index === 0) ? 0 : -1;
      tab.title = view.name;
      tab.setAttribute("aria-label", view.name + (counts.attention ? " · " + counts.attention + " need attention" : "") +
        (counts.active ? " · Working" : counts.completed ? " · New responses" : ""));
      const label = this.doc.createElement("span"); label.className = "view-tab-name"; label.textContent = view.name;
      tab.append(label);
      if (counts.attention) {
        const badge = this.doc.createElement("span"); badge.className = "view-attention";
        badge.textContent = String(counts.attention); badge.setAttribute("aria-hidden", "true"); tab.append(badge);
      }
      if (counts.action || counts.active || counts.completed) {
        const dot = this.doc.createElement("span"); dot.className = counts.action ? "view-action" : counts.active ? "view-active" : "view-completed";
        dot.setAttribute("aria-hidden", "true"); tab.append(dot);
      }
      tab.onclick = () => this.activate(view.id).catch(this.onError);
      if (view.kind === "group") {
        this.bindGroupDrag(tab, view);
        tab.oncontextmenu = event => this.openGroupMenu(view, tab, event);
        tab.onkeydown = event => {
          if (event.key === "ContextMenu" || event.shiftKey && event.key === "F10") this.openGroupMenu(view, tab, event);
        };
      }
      tabs.append(tab);
    });
    picker.value = this.activeId;
    savedPicker.value = this.local(this.current()) ? "" : this.activeId;
    const counts = badges[this.items.indexOf(this.current())];
    const attention = this.$("mobile-view-attention"), activity = this.$("mobile-view-activity");
    attention.textContent = String(counts.attention); attention.hidden = !counts.attention;
    attention.setAttribute("aria-label", counts.attention + " need attention");
    activity.hidden = !counts.action && !counts.active && !counts.completed;
    activity.className = counts.action ? "view-action" : counts.active ? "view-active" : "view-completed";
    activity.setAttribute("aria-label", counts.action ? "Action required" : counts.active ? "Active work" : "New responses");
    this.$("welcome").setAttribute("aria-labelledby", "overview-title");
    if (hadFocus) this.focusActive();
  }
  async mutation(fn) {
    if (this.busy) return;
    this.busy = true; this.changed(); this.$("save-view").disabled = true;
    try { await Promise.all([...this.saves.values()].map(pending => pending.promise)); await fn(); }
    catch (error) { this.onError(error); }
    finally { this.busy = false; this.$("save-view").disabled = false; this.render(); }
  }
  async openDialog(mode, target = this.current()) {
    const id = this.activeId, cwd = this.cwd;
    await this.saves.get(id)?.promise;
    if (this.activeId !== id || this.cwd !== cwd || cwd === "") return;
    this.closeMenu();
    // Capture the target and definition so polling or navigation cannot retarget a save.
    const view = this.items.find(view => view.id === target.id);
    if (!view) return;
    this.edit = {mode, id: view.id, projectId: view.projectId, version: view.version, cwd,
      value: mode === "duplicate" ? {filters: view.filters, group_by: view.group_by} : this.read()};
    this.$("view-dialog-title").textContent = mode === "group-rename" ? "Rename group" : mode === "rename" ? "Rename saved filters" : mode === "duplicate" ? "Duplicate saved filters" : "Save filters";
    this.$("view-dialog-description").textContent = mode === "group-rename" ? "Change the name wherever this group appears."
      : mode === "rename" ? "Give these saved filters a new name." : "Save these filters and display settings in this project.";
    this.$("view-name").value = ["rename", "group-rename"].includes(mode) ? view.name : mode === "duplicate" ? view.name + " copy" : "";
    this.$("save-view").textContent = ["rename", "group-rename"].includes(mode) ? "Rename" : "Save filters";
    this.$("view-error").hidden = true;
    this.$("view-dialog").showModal(); this.$("view-name").focus();
  }
  bind() {
    for (const [id, show] of [["show-now", "active"], ["show-later", "later"], ["show-closed", "archived"]]) {
      const button = this.$(id);
      if (button) button.onclick = async () => {
        try {
          const base = this.read();
          const placement = show === "active" && ["attention", "completed"].includes(base.filters.show) ? base.filters.show : show;
          this.apply({...base, filters: {...base.filters, show: placement}});
          await this.persist(); await this.onSwitch(); button.focus({preventScroll: true});
        } catch (error) { this.onError(error); }
      };
    }
    this.$("saved-view-tabs").onkeydown = event => {
      if (event.ctrlKey || event.altKey || event.metaKey || event.shiftKey) return;
      const custom = this.items.filter(view => this.visibleTab(view));
      if (!custom.length) return;
      const focused = event.target?.dataset?.viewId || this.activeId;
      const next = threadViews.shortcut(event, Math.max(0, custom.findIndex(view => view.id === focused)), custom.length, true);
      if (next === null) return;
      event.preventDefault();
      return this.activate(custom[next].id).catch(this.onError);
    };
    this.$("mobile-view").onchange = () => this.activate(this.$("mobile-view").value).catch(this.onError);
    this.$("saved-filter").onchange = () => {
      if (this.$("saved-filter").value) return this.activate(this.$("saved-filter").value).catch(this.onError);
    };
    this.$("group-rename").onclick = () => this.openDialog("group-rename").catch(this.onError);
    this.$("context-rename-group").onclick = () => this.openDialog("group-rename", this.contextGroup).catch(this.onError);
    this.$("group-hide").onclick = () => this.hideGroup(this.current()).catch(this.onError);
    this.$("context-hide-group").onclick = () => this.hideGroup(this.contextGroup).catch(this.onError);
    this.$("group-context-menu").onkeydown = event => {
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const buttons = [...this.$("group-context-menu").children];
      const current = buttons.indexOf(this.doc.activeElement);
      const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 :
        (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next]?.focus();
    };
    this.$("group-remove").onclick = () => this.requestGroupRemoval(this.current()).catch(this.onError);
    this.$("context-remove-group").onclick = () => this.requestGroupRemoval(this.contextGroup).catch(this.onError);
    this.$("remove-group-form").onsubmit = event => {
      event.preventDefault();
      if (this.removeTarget) return this.removeGroup(this.removeTarget);
    };
    this.$("remove-group-dialog").onclose = () => { this.removeTarget = null; };
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
        this.saved = result.data; this.rebuild();
        if (this.activeId === id) await this.activate("all");
        this.saves.delete(id); delete this.navigation[id]; this.remember();
      });
    };
    this.$("view-form").onsubmit = event => {
      event.preventDefault();
      return this.mutation(async () => {
        const edit = this.edit, name = this.$("view-name").value.trim();
        if (!name) throw new Error("Enter a name.");
        if (edit.mode === "group-rename") {
          const group = await this.api("projects/" + encodeURIComponent(edit.projectId), {name});
          this.groups = this.groups.map(item => item.id === group.id ? group : item); this.rebuild();
          if (this.activeId === edit.id) this.definition = this.current();
          this.$("view-dialog").close(); await this.onSwitch(); this.focusActive(); return;
        }
        const result = await this.api(edit.mode === "rename" ? "views/" + encodeURIComponent(edit.id) : "views",
          edit.mode === "rename" ? {name, version: edit.version} : {name, ...edit.value, ...(edit.cwd ? {cwd: edit.cwd} : {})});
        if (edit.mode === "rename") {
          this.saved = this.saved.map(view => view.id === result.id ? result : view);
          this.items = this.items.map(view => view.id === result.id ? result : view);
          if (this.activeId === result.id) this.definition = {...this.definition, name: result.name, version: result.version};
        } else {
          // Keep the shared definition even if its project is no longer visible.
          this.saved = [...this.saved.filter(view => view.id !== result.id), result];
          if (this.scoped([result]).length) this.items = [...this.items.filter(view => view.id !== result.id), result];
        }
        this.$("view-dialog").close();
        if (edit.mode !== "rename") await this.activate(result.id, true);
        else this.focusActive();
      });
    };
    this.doc.addEventListener("keydown", event => {
      if (!this.$("group-context-menu").hidden && ["Escape", "Tab"].includes(event.key)) {
        if (event.key === "Escape") event.preventDefault();
        this.closeGroupMenu(true); return;
      }
      if (this.doc.querySelector("dialog[open]") || this.doc.body.classList.contains("navigation-open")) return;
      if (event.key === "Escape" && this.$("view-menu").open) {
        event.preventDefault(); this.closeMenu(); this.$("view-menu-toggle").focus();
      }
      if (event.key === "Escape" && this.$("groups-menu").open) {
        event.preventDefault(); this.$("groups-menu").open = false; this.$("groups-menu-toggle").focus();
      }
    });
    this.doc.addEventListener("pointerdown", event => {
      if (!this.$("group-context-menu").contains(event.target)) this.closeGroupMenu();
      if (!this.$("view-menu").contains(event.target)) this.$("view-menu").open = false;
      if (!this.$("groups-menu").contains(event.target)) this.$("groups-menu").open = false;
    });
    this.doc.defaultView?.addEventListener("scroll", () => this.closeGroupMenu(), {capture: true, passive: true});
    this.doc.defaultView?.addEventListener("resize", () => this.closeGroupMenu());
  }
}

if (typeof module !== "undefined") module.exports = {threadViews, SavedViews};
