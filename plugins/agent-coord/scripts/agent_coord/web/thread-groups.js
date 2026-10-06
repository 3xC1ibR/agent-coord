"use strict";

// Shared by the overview and navigation. Grouping never changes saved placement.
const threadGrouping = (() => {
  const phases = [
    ["discussion", "Discussing"], ["investigation", "Investigating"],
    ["planning", "Planning"], ["implementation", "Implementing"],
    ["validation", "Validating"], ["deployment", "Deploying"],
    ["finished", "Completed"], ["new", "Getting started"],
  ];

  function awaitsUser(thread) {
    return thread.attention !== "archived" && ["input", "reply", "failed"].includes(thread.response_state);
  }

  function status(thread) {
    if (thread.attention === "archived") return {key: "idle", label: "Closed"};
    const response = {
      input: {key: "waiting", label: "Needs input"},
      reply: {key: "waiting", label: "Your turn"},
      completed: {key: "completed", label: "Completed"},
      working: {key: "running", label: "Working"},
      failed: {key: "failed", label: "Failed"},
      interrupted: {key: "idle", label: "Stopped"},
    }[thread.response_state];
    return response || {key: thread.status, label: {
      running: "Working", idle: "Ready", online: "Ready", saved: "Saved",
      offline: "Offline", stale: "Inactive",
    }[thread.status] || thread.status};
  }

  function groupThreads(threads) {
    const priority = threads.filter(awaitsUser);
    const rest = threads.filter(thread => !awaitsUser(thread));
    const active = rest.filter(thread => thread.attention !== "later");
    const completed = active.filter(thread => thread.attention !== "archived" && thread.response_state === "completed")
      .sort((a, b) => Number(Boolean(b.unread)) - Number(Boolean(a.unread)));
    const ongoing = active.filter(thread => !completed.includes(thread));
    const known = new Set(phases.map(([key]) => key));
    return {
      priority,
      completed,
      phases: phases.map(([key, label]) => ({key, label, threads: ongoing.filter(thread =>
        (known.has(thread.checkpoint?.phase) ? thread.checkpoint.phase : "new") === key)
      })).filter(group => group.threads.length),
      later: rest.filter(thread => thread.attention === "later"),
    };
  }

  return {awaitsUser, groupThreads, status};
})();

if (typeof module !== "undefined") module.exports = threadGrouping;
