"use strict";

// Tabs are a window's working set, independent of thread placement and filters.
class ThreadTabs {
  constructor({document, container, storage, scope = "", onSelect, onError, canNavigate = () => true}) {
    Object.assign(this, {document, container, storage, onSelect, onError, canNavigate});
    this.baseKey = "agent-coord.thread-tabs." + scope;
    this.projects = new Map(); this.items = []; this.selected = null;
    container?.setAttribute("role", "tablist");
    container?.setAttribute("aria-label", "Open conversations");
  }
  setProject(cwd = "") {
    if (this.cwd === cwd) return;
    this.cwd = cwd;
    if (!this.projects.has(cwd)) {
      let items;
      try { items = JSON.parse(this.storage?.getItem(this.baseKey + ":" + cwd) || "[]"); } catch { /* Optional window state. */ }
      const ids = new Set();
      this.projects.set(cwd, Array.isArray(items) ? items.filter(item => {
        if (!item || typeof item.thread_id !== "string" || !item.thread_id || item.thread_id === "new-session" || ids.has(item.thread_id)) return false;
        ids.add(item.thread_id); return true;
      }).map(item => ({thread_id: item.thread_id, title: typeof item.title === "string" ? item.title : "Conversation"})) : []);
    }
    this.items = this.projects.get(cwd); this.selected = null; this.render();
  }
  save() {
    try { this.storage?.setItem(this.baseKey + ":" + this.cwd, JSON.stringify(this.items
      .filter(item => item.thread_id !== "new-session").map(({thread_id, title}) => ({thread_id, title})))); } catch { /* Tabs still work without storage. */ }
  }
  open(thread) {
    if (!thread?.thread_id) return;
    const item = this.items.find(item => item.thread_id === thread.thread_id);
    if (item) Object.assign(item, thread);
    else this.items.push({...thread, title: thread.title || "Conversation"});
    this.selected = thread.thread_id; this.save(); this.render(); this.reveal(this.selected);
  }
  overview() { this.selected = null; this.render(); }
  suspend() { this.selected = undefined; this.render(); }
  update(threads) {
    const available = new Map(threads.map(thread => [thread.thread_id, thread]));
    for (const item of this.items) if (available.has(item.thread_id)) Object.assign(item, available.get(item.thread_id));
    this.save(); this.render();
  }
  replace(id, thread) {
    const index = this.items.findIndex(item => item.thread_id === id);
    if (index < 0) return;
    this.items.splice(index, 1);
    if (!this.items.some(item => item.thread_id === thread.thread_id)) this.items.splice(index, 0, {...thread});
    if (this.selected === id) this.selected = thread.thread_id;
    this.save(); this.render();
  }
  async activate(id) {
    if (id === this.selected || !this.canNavigate()) return;
    try { await this.onSelect(id); } catch (error) { this.onError?.(error); }
  }
  async close(id) {
    const index = this.items.findIndex(item => item.thread_id === id);
    if (index < 0 || !this.canNavigate()) return;
    const selected = this.selected === id;
    this.items.splice(index, 1); this.save(); this.render();
    // Only navigate. Closing a tab never changes the thread or its running agent.
    if (selected) await this.activate(this.items[Math.min(index, this.items.length - 1)]?.thread_id || null);
    this.reveal(this.selected, true);
  }
  move(id, index) {
    const from = this.items.findIndex(item => item.thread_id === id);
    if (from < 0) return;
    const [item] = this.items.splice(from, 1);
    this.items.splice(Math.max(0, Math.min(index, this.items.length)), 0, item);
    this.save(); this.render(); this.reveal(id, true);
  }
  reveal(id, focus = false) {
    const button = [...(this.container?.querySelectorAll("[role=tab]") || [])].find(button => button.dataset.threadTab === (id || ""));
    if (focus) button?.focus({preventScroll: true});
    button?.scrollIntoView?.({block: "nearest", inline: "nearest"});
  }
  keydown(event, id) {
    if (event.isComposing || event.metaKey || event.ctrlKey) return;
    if (event.altKey && event.shiftKey && ["ArrowLeft", "ArrowRight"].includes(event.key) && id) {
      event.preventDefault();
      const index = this.items.findIndex(item => item.thread_id === id);
      this.move(id, index + (event.key === "ArrowLeft" ? -1 : 1)); return;
    }
    if (event.altKey || event.shiftKey) return;
    if (event.key === "Delete" && id) { event.preventDefault(); this.close(id); return; }
    const ids = [null, ...this.items.map(item => item.thread_id)], index = ids.indexOf(id);
    const next = event.key === "Home" ? 0 : event.key === "End" ? ids.length - 1
      : event.key === "ArrowRight" ? (index + 1) % ids.length : event.key === "ArrowLeft" ? (index + ids.length - 1) % ids.length : -1;
    if (next >= 0) { event.preventDefault(); this.reveal(ids[next], true); }
  }
  endDrag() {
    this.dragging = null; this.onDragEnd?.(); this.render();
  }
  render() {
    // Status updates must not replace the native drag source mid-gesture.
    if (!this.container || this.dragging) return;
    const signature = JSON.stringify([this.selected ?? null, this.selected === undefined,
      this.items.map(item => [item.thread_id, item.title, item.response_state, item.unread_result])]);
    if (signature === this.signature) return;
    this.signature = signature;
    const focused = this.container.contains(this.document.activeElement) ? this.document.activeElement?.dataset.threadTab : undefined;
    const scroll = this.container.scrollLeft;
    this.container.replaceChildren();
    const add = (item, id) => {
      const wrapper = this.document.createElement("div"); wrapper.className = "conversation-tab";
      const tab = this.document.createElement("button"); tab.type = "button"; tab.className = "conversation-tab-select";
      tab.dataset.threadTab = id || ""; tab.textContent = item.title; tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", String(this.selected === id));
      tab.setAttribute("aria-controls", id ? "conversation" : "welcome");
      tab.tabIndex = this.selected === id || this.selected === undefined && id === null ? 0 : -1;
      tab.title = item.title + (item.unread_result ? " · New response" : "") + (id ? " · Alt+Shift+←/→ to reorder" : "");
      tab.dataset.status = item.response_state || "";
      tab.onclick = () => this.activate(id); tab.onkeydown = event => this.keydown(event, id);
      wrapper.dataset.selected = String(this.selected === id);
      wrapper.append(tab);
      if (id) {
        const close = this.document.createElement("button"); close.type = "button"; close.className = "conversation-tab-close";
        close.textContent = "×"; close.title = "Close tab"; close.setAttribute("aria-label", "Close tab · " + item.title);
        close.onclick = () => this.close(id); wrapper.append(close);
        wrapper.draggable = id !== "new-session";
        wrapper.ondragstart = event => {
          if (id === "new-session" || !this.canNavigate() || this.onDragStart?.(item, event) === false) { event.preventDefault(); return; }
          this.dragging = id; event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setData("text/plain", id);
        };
        wrapper.ondragend = () => this.endDrag();
        wrapper.ondragover = event => { if (this.dragging) { event.preventDefault(); event.dataTransfer.dropEffect = "move"; } };
        wrapper.ondrop = event => {
          if (!this.dragging || this.dragging === id) return;
          event.preventDefault();
          const from = this.items.findIndex(item => item.thread_id === this.dragging);
          const target = this.items.findIndex(item => item.thread_id === id);
          const rect = wrapper.getBoundingClientRect(), after = event.clientX >= rect.left + rect.width / 2;
          this.move(this.dragging, target + (after ? 1 : 0) - (from < target ? 1 : 0)); this.endDrag();
        };
      } else wrapper.classList.add("overview-tab");
      this.container.append(wrapper);
    };
    add({title: "Overview"}, null);
    for (const item of this.items) add(item, item.thread_id);
    this.container.scrollLeft = scroll;
    if (focused !== undefined) this.reveal(focused || null, true);
  }
}

if (typeof module !== "undefined") module.exports = {ThreadTabs};
