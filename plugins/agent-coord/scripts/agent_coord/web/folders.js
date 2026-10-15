"use strict";

// Only the active project supplies the launch directory. Views may select a
// group, but repository metadata and filters never choose a workspace.
function newThreadContext(organization, filters, folder) {
  const project = organization.projects.find(item => item.id === filters.project);
  if (filters.project && filters.project !== "__none__" && !project)
    throw new Error("This group is unavailable. Choose another group.");
  if (project?.cwd && project.cwd !== folder?.cwd)
    throw new Error("This group belongs to another project. Choose a group in this project.");
  return {cwd: folder?.cwd || "", project_id: project?.id || null};
}

function inProject(thread, cwd) {
  return !!cwd && (thread.project_cwd || thread.cwd) === cwd;
}

class WorkingFolders {
  constructor({storage, preferences, key = "browser", scope = "", api, onChange}) {
    this.storage = storage; this.preferences = preferences; this.api = api;
    this.onChange = onChange;
    this.key = "agent-coord.folder." + key + ":" + scope;
    this.recentKey = "agent-coord.folders.recent:" + scope;
    this.selected = null; this.projects = [];
    try {
      const saved = JSON.parse(storage?.getItem(this.key) || "null");
      const projects = Array.isArray(saved?.projects) ? saved.projects : saved?.cwd ? [saved] : [];
      this.projects = projects.filter(item => typeof item?.cwd === "string" && item.cwd);
      this.selected = this.projects.find(item => item.cwd === saved?.active) || this.projects[0] || null;
    } catch { /* Optional preferences. */ }
  }
  recent() {
    try {
      const items = JSON.parse(this.preferences?.getItem(this.recentKey) || "[]");
      return Array.isArray(items) ? items.filter(item => typeof item === "string").slice(0, 12) : [];
    } catch { return []; }
  }
  async open(path) {
    const folder = await this.api("workspaces/open", {path});
    return this.adopt(folder);
  }
  adopt(folder) {
    this.selected = folder;
    const index = this.projects.findIndex(item => item.cwd === folder.cwd);
    if (index < 0) this.projects.push(folder);
    else this.projects[index] = folder;
    this.save();
    try {
      this.preferences?.setItem(this.recentKey, JSON.stringify([folder.cwd, ...this.recent().filter(p => p !== folder.cwd)].slice(0, 12)));
    } catch { /* The in-memory selection still works when storage is unavailable. */ }
    return folder;
  }
  save() {
    try { this.storage?.setItem(this.key, JSON.stringify({projects: this.projects, active: this.selected?.cwd || ""})); }
    catch { /* Tabs still work without storage. */ }
    try { this.onChange?.(this.snapshot()); } catch { /* Server sync must not block local project tabs. */ }
  }
  snapshot() { return {projects: this.projects.map(item => item.cwd), active: this.selected?.cwd || ""}; }
  activate(cwd) {
    this.selected = this.projects.find(item => item.cwd === cwd) || null;
    this.save();
  }
  reset() { this.projects = []; this.selected = null; this.save(); }
  close(cwd) {
    const index = this.projects.findIndex(item => item.cwd === cwd);
    this.projects = this.projects.filter(item => item.cwd !== cwd);
    if (this.selected?.cwd === cwd) this.selected = this.projects[Math.min(index, this.projects.length - 1)] || null;
    this.save();
  }
  async restore(fallback) {
    const active = this.selected?.cwd || fallback;
    const paths = [...new Set([...this.projects.map(item => item.cwd), active].filter(Boolean))];
    this.projects = []; this.selected = null;
    let failure;
    for (const path of paths) {
      try { await this.open(path); } catch (error) { failure = error; }
    }
    this.activate(this.projects.some(item => item.cwd === active) ? active : this.projects[0]?.cwd);
    if (failure) throw failure;
  }
  async replace(paths, active = "") {
    const previous = this.selected?.cwd || "";
    this.projects = []; this.selected = null;
    let failure;
    for (const path of [...new Set(paths.filter(Boolean))]) {
      try { await this.open(path); } catch (error) { failure = error; }
    }
    const available = new Set(this.projects.map(item => item.cwd));
    this.activate([previous, active, this.projects[0]?.cwd].find(path => available.has(path)) || "");
    if (failure) throw failure;
  }
}

class ProjectTabs {
  constructor({document, folders, onSwitch, onError, canSwitch = () => true}) {
    Object.assign(this, {doc: document, folders, onSwitch, onError, canSwitch});
    this.setupHeader();
    this.bindMobilePicker();
    document.addEventListener("keydown", event => {
      if (document.querySelector("dialog[open]") || event.target.closest("input, textarea, select, [contenteditable]")) return;
      const tabs = document.getElementById("view-tabs"), items = folders.projects;
      if (!items.length) return;
      const current = items.indexOf(folders.selected);
      const next = threadViews.shortcut(event, current, items.length, tabs.contains(event.target));
      if (next === null) return;
      event.preventDefault(); this.activate(items[next].cwd).catch(onError);
    });
  }
  setupHeader() {
    const header = this.doc.querySelector(".workspace-bar"), projects = this.doc.getElementById("project-navigation");
    const desktop = this.doc.defaultView?.matchMedia("(min-width: 721px)");
    if (!header || !projects || !desktop) return;
    // Keep one set of controls and restore their original positions on phones.
    const home = this.doc.createComment("project navigation position"); projects.before(home);
    const sync = () => {
      header.classList.toggle("header-tabs", desktop.matches);
      if (desktop.matches) header.insertBefore(projects, header.querySelector(".dispatcher-mobile-nav"));
      else home.after(projects);
    };
    desktop.addEventListener("change", sync); sync();
  }
  // Phones replace the tab strip with one project picker in the header.
  bindMobilePicker() {
    const picker = this.doc.getElementById("mobile-project"), close = this.doc.getElementById("mobile-close-project");
    if (picker) picker.onchange = () => {
      const cwd = picker.value;
      picker.value = this.folders.selected?.cwd || "";
      if (cwd === "__open__") this.doc.getElementById("open-project-tab")?.click();
      else if (cwd) this.activate(cwd).catch(this.onError);
    };
    if (close) close.onclick = () => {
      this.doc.getElementById("view-menu")?.removeAttribute("open");
      if (this.folders.selected) this.close(this.folders.selected.cwd).catch(this.onError);
    };
  }
  async close(cwd) {
    if (!this.canSwitch()) return;
    const active = this.folders.selected?.cwd === cwd;
    this.folders.close(cwd);
    if (active) await this.onSwitch();
    this.render();
  }
  renderMobilePicker(threads) {
    const picker = this.doc.getElementById("mobile-project");
    if (!picker) return;
    picker.replaceChildren();
    let selectedCounts = {};
    for (const project of this.folders.projects) {
      const option = this.doc.createElement("option"), selected = project.cwd === this.folders.selected?.cwd;
      const counts = threadViews.badges(threads.filter(t => inProject(t, project.cwd)), {});
      if (selected) selectedCounts = counts;
      option.value = project.cwd;
      option.textContent = (project.cwd.split("/").filter(Boolean).pop() || "/") + (selected ? "" :
        (counts.attention ? " · " + counts.attention + " need attention" : "") +
        (counts.active ? " · Working" : counts.completed ? " · New responses" : ""));
      picker.append(option);
    }
    const open = this.doc.createElement("option"); open.value = "__open__"; open.textContent = "Open project…";
    picker.append(open);
    picker.value = this.folders.selected?.cwd || "";
    const attention = this.doc.getElementById("mobile-project-attention"), activity = this.doc.getElementById("mobile-project-activity");
    if (attention) {
      attention.textContent = String(selectedCounts.attention || ""); attention.hidden = !selectedCounts.attention;
      attention.setAttribute("aria-label", (selectedCounts.attention || 0) + " need attention");
    }
    if (activity) {
      activity.hidden = !selectedCounts.active && !selectedCounts.completed;
      activity.className = selectedCounts.active ? "view-active" : "view-completed";
      activity.setAttribute("aria-label", selectedCounts.active ? "Active work" : "New responses");
    }
    const close = this.doc.getElementById("mobile-close-project");
    if (close) close.hidden = !this.folders.selected;
  }
  async activate(cwd) {
    if (!this.canSwitch()) return;
    this.folders.activate(cwd);
    const changing = this.onSwitch();
    this.render();
    const tab = this.doc.getElementById("view-tabs").querySelector('[aria-selected="true"]');
    tab?.focus({preventScroll: true});
    tab?.scrollIntoView({block: "nearest", inline: "nearest"});
    await changing;
  }
  render(threads = this.threads || []) {
    this.threads = threads;
    const tabs = this.doc.getElementById("view-tabs");
    const signature = JSON.stringify([this.folders.projects, this.folders.selected?.cwd,
      threads.map(t => [t.thread_id, t.project_cwd, t.attention, t.needs_attention, t.response_state, t.unread, t.unread_result, t.unhandled_response])]);
    if (signature === this.signature) return;
    this.signature = signature;
    this.renderMobilePicker(threads);
    const focused = tabs.contains(this.doc.activeElement) ? this.doc.activeElement?.dataset.projectCwd : null;
    tabs.replaceChildren();
    for (const project of this.folders.projects) {
      const wrapper = this.doc.createElement("div"); wrapper.className = "project-tab"; wrapper.setAttribute("role", "presentation");
      const tab = this.doc.createElement("button"), label = this.doc.createElement("span");
      const selected = project.cwd === this.folders.selected?.cwd;
      tab.type = "button"; tab.className = "view-tab"; tab.dataset.projectCwd = project.cwd;
      tab.setAttribute("role", "tab"); tab.setAttribute("aria-selected", String(selected));
      tab.setAttribute("aria-controls", "welcome"); tab.tabIndex = selected ? 0 : -1;
      tab.title = project.cwd;
      label.className = "view-tab-name"; label.textContent = project.cwd.split("/").filter(Boolean).pop() || "/";
      const icon = this.doc.createElement("span"); icon.className = "project-folder-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.innerHTML = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"><path d="M2.5 5.5a1.5 1.5 0 0 1 1.5-1.5h4l2 2h6a1.5 1.5 0 0 1 1.5 1.5v7A1.5 1.5 0 0 1 16 16H4a1.5 1.5 0 0 1-1.5-1.5z"/></svg>';
      tab.append(icon, label);
      const counts = threadViews.badges(threads.filter(t => inProject(t, project.cwd)), {});
      if (counts.attention) {
        const badge = this.doc.createElement("span"); badge.className = "view-attention"; badge.textContent = String(counts.attention); tab.append(badge);
      }
      if (counts.active || counts.completed) {
        const dot = this.doc.createElement("span"); dot.className = counts.active ? "view-active" : "view-completed";
        dot.setAttribute("aria-label", counts.active ? "Active work" : "New responses"); tab.append(dot);
      }
      tab.onclick = () => this.activate(project.cwd).catch(this.onError);
      const close = this.doc.createElement("button"); close.type = "button"; close.className = "project-tab-close"; close.textContent = "×";
      close.setAttribute("aria-label", "Close project " + project.cwd); close.title = "Close project tab; agents keep running";
      close.onclick = () => this.close(project.cwd).catch(this.onError);
      wrapper.append(tab, close); tabs.append(wrapper);
      if (focused === project.cwd) tab.focus({preventScroll: true});
    }
  }
}

class FolderPicker {
  constructor({document, folders, api, desktop, remote, onOpened, onError, canSwitch = () => true}) {
    this.doc = document; this.$ = id => document.getElementById(id);
    Object.assign(this, {folders, api, desktop, onOpened, onError, canSwitch});
    this.$("open-folder").onclick = this.$("welcome-folder").onclick = () => this.choose().catch(onError);
    if (this.$("open-project-tab")) this.$("open-project-tab").onclick = () => this.choose().catch(onError);
    this.$("working-folder").onchange = async () => {
      const path = this.$("working-folder").value;
      if (!path) return;
      if (!this.canSwitch()) { this.render(); return; }
      try {
        if (path === "__choose_folder__") await this.choose();
        else { await folders.open(path); await onOpened(); }
      }
      catch (error) { onError(error); }
      finally { this.render(); }
    };
    this.$("folder-form").onsubmit = async event => {
      event.preventDefault();
      if (this.$("folder-submit").disabled) return;
      this.$("folder-submit").disabled = true;
      try {
        this.chosen = await this.api("workspaces/open", {path: this.$("folder-path").value});
        this.accepted = true; this.$("folder-dialog").close();
      } catch (error) {
        this.$("folder-error").textContent = error.message; this.$("folder-error").hidden = false;
      } finally { this.$("folder-submit").disabled = false; }
    };
    if (remote) this.$("folder-description").textContent = "Choose a folder on the computer running Ribbon Field.";
  }
  render(context = this.context, onboarding = this.onboarding) {
    this.context = context; this.onboarding = onboarding;
    const current = context?.cwd || this.folders.selected?.cwd || "";
    const select = this.$("working-folder");
    const choices = [...new Set([current, ...this.folders.recent()].filter(Boolean))];
    const signature = JSON.stringify(choices);
    if (this.signature !== signature) {
      this.signature = signature;
      const placeholder = new Option("No folder selected", "");
      placeholder.disabled = true;
      select.replaceChildren(placeholder, new Option("Choose a folder…", "__choose_folder__"), ...choices.map(path =>
        new Option((path.split("/").filter(Boolean).pop() || "/") + " — " + path, path)));
    }
    select.value = current; select.title = current || "Choose where new sessions will work";
    this.$("folder-welcome").hidden = !onboarding || !!current;
  }
  async choose({navigate = true} = {}) {
    if (this.pending || !this.canSwitch()) return false;
    this.pending = true;
    try {
      let accepted = false;
      if (this.desktop?.chooseFolder) {
        const path = await this.desktop.chooseFolder();
        if (path) { this.chosen = await this.api("workspaces/open", {path}); accepted = true; }
      } else {
        this.accepted = false;
        this.$("folder-error").hidden = true;
        this.$("folder-path").value = this.folders.selected?.cwd || "";
        const dialog = this.$("folder-dialog");
        const closed = new Promise(resolve => dialog.addEventListener("close", resolve, {once: true}));
        dialog.showModal(); this.$("folder-path").focus();
        this.api("workspaces").then(result => {
          if (dialog.open) this.$("folder-choices").replaceChildren(...result.data.map(item => new Option(item.cwd, item.cwd)));
        }).catch(() => {});
        await closed; accepted = this.accepted;
      }
      if (accepted) {
        const target = this.$("project-target-dialog");
        let destination = "tab";
        if (navigate && this.folders.projects.length && target?.showModal) {
          this.$("project-target-path").textContent = this.chosen.cwd;
          target.returnValue = "cancel";
          const closed = new Promise(resolve => target.addEventListener("close", resolve, {once: true}));
          target.showModal(); await closed; destination = target.returnValue;
        }
        if (destination === "window") {
          const url = "/?directory=" + encodeURIComponent(this.chosen.cwd) + "&new_project=1";
          if (this.desktop?.newWindow) await this.desktop.newWindow(url);
          else if (!this.doc.defaultView.open(url, "_blank", "popup")) throw new Error("Allow a new window to open this project.");
          return false;
        }
        if (destination !== "tab") return false;
        this.folders.adopt(this.chosen);
        await this.onOpened();
      }
      return accepted;
    } finally { this.pending = false; this.render(); }
  }
}

if (typeof module !== "undefined") module.exports = {WorkingFolders, ProjectTabs, FolderPicker, newThreadContext, inProject};
