"use strict";

const ViewDrops = (() => {
  const views = typeof module !== "undefined" ? require("./views.js").threadViews : threadViews;
  function plan(thread, view, projects) {
    const reject = message => ({allowed: false, message});
    if (!thread || !view) return reject("This thread or view is no longer available.");
    if (view.pending) return reject("Wait for this view’s filters to finish saving.");
    if (view.cwd && view.cwd !== (thread.project_cwd || thread.cwd)) return reject("This view belongs to another workspace.");
    const filters = views.filters(view.filters);
    if (!filters.project) return reject("This view has no group filter.");
    if (filters.search) return reject("Views with a search cannot accept cards.");
    if (thread.attention === "archived" || filters.show === "archived") return reject("Reopen closed threads before changing their group here.");
    const projectId = filters.project === "__none__" ? null : filters.project;
    const project = projects.find(item => item.id === projectId);
    if (projectId && !project) return reject("This view’s group is no longer available.");
    if (project?.cwd && project.cwd !== (thread.project_cwd || thread.cwd)) return reject("This group belongs to another workspace.");
    const result = {...thread, project_id: projectId, project_name: project?.name || null};
    if (!views.matches(result, {show: filters.show})) {
      return reject({attention: "This view only shows threads that need attention.", completed: "This view only shows finished threads.",
        later: "This view only shows threads in Later.", active: "This thread does not match this view’s Now filter."}[filters.show] || "This thread does not match the view’s placement filter.");
    }
    if (!views.matches(result, {show: filters.show, phase: filters.phase})) return reject("This thread does not match the view’s stage filter.");
    if (!views.matches(result, filters)) return reject("This thread does not match the view’s other filters.");
    if ((thread.project_id || null) === projectId) return reject("This thread already belongs to this view’s group.");
    return {allowed: true, projectId, message: project ? "Assign to " + project.name : "Remove from group"};
  }

  function create({document: doc, getThread, getViews, getProjects, api, onChanged, notify, onError}) {
    let dragging = null;
    const pending = new Set(), tabSelector = "#saved-view-tabs [data-view-id]";
    const hint = doc.createElement("div");
    hint.className = "view-drop-hint"; hint.hidden = true;
    hint.setAttribute("role", "status"); doc.body.append(hint);
    const tabs = () => [...doc.querySelectorAll(tabSelector)];
    const choice = (id, thread = getThread(dragging)) => plan(thread, getViews().find(view => view.id === id), getProjects());
    function message(text) { hint.hidden = !text; if (hint.textContent !== text) hint.textContent = text; }
    function reset() {
      dragging = null; message("");
      for (const tab of tabs()) for (const name of ["view-drop-allowed", "view-drop-blocked", "view-drop-over"]) tab.classList.remove(name);
    }
    function highlight(target) {
      let available = false;
      for (const tab of tabs()) {
        const allowed = choice(tab.dataset.viewId).allowed;
        available ||= allowed;
        tab.classList.toggle("view-drop-allowed", allowed);
        tab.classList.toggle("view-drop-blocked", !allowed);
        tab.classList.toggle("view-drop-over", allowed && tab === target);
      }
      message(target ? choice(target.dataset.viewId).message : available
        ? "Drop onto a highlighted view to change this thread’s group."
        : "No saved views can accept this thread.");
    }
    async function assign(threadId, viewId) {
      if (pending.has(threadId)) return;
      const target = getViews().find(view => view.id === viewId);
      const initial = plan(getThread(threadId), target, getProjects());
      if (!initial.allowed) throw new Error(initial.message);
      pending.add(threadId);
      try {
        // Recheck saved filters and current thread state without opening its conversation.
        const [thread, inventory, organization] = await Promise.all([
          api("threads/" + encodeURIComponent(threadId)), api("views"), api("organization"),
        ]);
        const latest = inventory.data.find(view => view.id === viewId);
        if (!latest || latest.version !== target.version || !views.same(latest, target)) throw new Error("This view changed. Try dropping the card again.");
        const assignment = plan(thread, {...latest, cwd: latest.cwd || target.cwd}, organization.projects);
        if (!assignment.allowed) throw new Error(assignment.message);
        const previous = thread.project_id || null, path = "threads/" + encodeURIComponent(threadId);
        const updated = await api(path, {project_id: assignment.projectId});
        notify("group:" + threadId, assignment.message + " · " + thread.title, async () => {
          if (pending.has(threadId)) throw new Error("Wait for the group change to finish.");
          pending.add(threadId);
          try {
            const current = await api(path);
            // A later assignment takes precedence over this Undo.
            if ((current.project_id || null) !== assignment.projectId) return;
            await onChanged(await api(path, {project_id: previous}));
          } finally { pending.delete(threadId); }
        });
        await onChanged(updated);
      } finally { pending.delete(threadId); }
    }
    doc.addEventListener("dragstart", event => {
      const source = event.target.closest?.("[data-drag-thread]");
      if (!source) return;
      const id = source.dataset.dragThread;
      if (!getThread(id) || pending.has(id)) { event.preventDefault(); return; }
      dragging = id;
      event.dataTransfer.setData("application/x-ribbon-field-thread", id);
      event.dataTransfer.effectAllowed = "move";
      highlight();
    });
    doc.addEventListener("dragover", event => {
      if (!dragging) return;
      const target = event.target.closest?.(tabSelector);
      highlight(target);
      if (target && choice(target.dataset.viewId).allowed) {
        event.preventDefault(); event.dataTransfer.dropEffect = "move";
      } else event.dataTransfer.dropEffect = "none";
    });
    doc.addEventListener("dragleave", event => {
      if (dragging && !event.relatedTarget) message("");
    });
    doc.addEventListener("drop", event => {
      if (!dragging) return;
      const id = dragging, target = event.target.closest?.(tabSelector);
      reset();
      if (!target) return;
      event.preventDefault(); event.stopPropagation();
      return assign(id, target.dataset.viewId).catch(onError);
    });
    doc.addEventListener("dragend", reset);
    return {assign, reset};
  }
  return {plan, create};
})();

if (typeof module !== "undefined") module.exports = ViewDrops;
