"use strict";

// Repository filters organize threads; this selection retains the exact folder
// (including a subdirectory or linked worktree) used to start new sessions.
function newThreadContext(organization, filters, folder) {
  const repository = organization.repositories.find(item => item.id === filters.repository);
  if (filters.repository && filters.repository !== "__none__" && !repository)
    throw new Error("This repository is unavailable. Choose another view or open a folder.");
  const project = organization.projects.find(item => item.id === filters.project);
  if (filters.project && filters.project !== "__none__" && !project)
    throw new Error("This group is unavailable. Choose another group.");
  const cwd = repository ? (folder?.repository?.root === repository.root ? folder.cwd : repository.root) : folder?.cwd;
  const result = {cwd: cwd || "", project_id: project?.id || null};
  if (repository) result.repository_id = repository.id;
  else if (filters.repository === "__none__") result.repository_id = null;
  return result;
}

class WorkingFolders {
  constructor({storage, preferences, key = "browser", scope = "", api}) {
    this.storage = storage; this.preferences = preferences; this.api = api;
    this.key = "agent-coord.folder." + key + ":" + scope;
    this.recentKey = "agent-coord.folders.recent:" + scope;
    this.selected = null;
    try {
      const saved = JSON.parse(storage?.getItem(this.key) || "null");
      if (typeof saved?.cwd === "string") this.selected = saved;
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
    this.selected = folder;
    try {
      this.storage?.setItem(this.key, JSON.stringify(folder));
      this.preferences?.setItem(this.recentKey, JSON.stringify([folder.cwd, ...this.recent().filter(p => p !== folder.cwd)].slice(0, 12)));
    } catch { /* The in-memory selection still works when storage is unavailable. */ }
    return folder;
  }
  async restore(fallback) {
    const path = this.selected?.cwd || fallback;
    if (!path) return;
    try { await this.open(path); }
    catch (error) {
      this.selected = null;
      try { this.storage?.removeItem(this.key); } catch { /* Optional preferences. */ }
      throw error;
    }
  }
}

class FolderPicker {
  constructor({document, folders, api, desktop, remote, onOpened, onError}) {
    this.$ = id => document.getElementById(id);
    Object.assign(this, {folders, api, desktop, onOpened, onError});
    this.$("open-folder").onclick = this.$("welcome-folder").onclick = () => this.choose().catch(onError);
    this.$("working-folder").onchange = async () => {
      const path = this.$("working-folder").value;
      if (!path) return;
      try { await folders.open(path); await onOpened(); }
      catch (error) { onError(error); }
      finally { this.render(); }
    };
    this.$("folder-form").onsubmit = async event => {
      event.preventDefault();
      if (this.$("folder-submit").disabled) return;
      this.$("folder-submit").disabled = true;
      try {
        await folders.open(this.$("folder-path").value);
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
      select.replaceChildren(new Option("Choose a folder", ""), ...choices.map(path =>
        new Option((path.split("/").filter(Boolean).pop() || "/") + " — " + path, path)));
    }
    select.value = current; select.title = current || "Choose where new sessions will work";
    this.$("folder-welcome").hidden = !onboarding || !!current;
  }
  async choose({navigate = true} = {}) {
    if (this.pending) return false;
    this.pending = true;
    try {
      let accepted = false;
      if (this.desktop?.chooseFolder) {
        const path = await this.desktop.chooseFolder();
        if (path) { await this.folders.open(path); accepted = true; }
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
      if (accepted && navigate) await this.onOpened();
      return accepted;
    } finally { this.pending = false; this.render(); }
  }
}

if (typeof module !== "undefined") module.exports = {WorkingFolders, FolderPicker, newThreadContext};
