"use strict";

// Stage, attention reason, and saved placement are independent.
const threadGrouping = (() => {
  const phases = [
    ["new", "Getting started"], ["investigation", "Investigating"],
    ["planning", "Planning"], ["implementation", "Implementing"],
    ["validation", "Validating"], ["deployment", "Deploying"], ["finished", "Done"],
  ];
  const reasons = {
    blocked: {rank: 0, label: "Blocked"}, review: {rank: 1, label: "Review requested"},
    update: {rank: 2, label: "Update"}, findings: {rank: 3, label: "Findings ready"},
    reply: {rank: 3, label: "Reply"}, done: {rank: 4, label: "✓ Done"},
  };
  function phase(thread) {
    const value = thread.work_phase || thread.checkpoint?.phase;
    return value === "discussion" ? "investigation" : phases.some(([key]) => key === value) ? value : "new";
  }
  function phaseLabel(thread) { return phases.find(([key]) => key === phase(thread))[1]; }
  function reason(thread) {
    const key = thread.attention_reason || ({input: "blocked", action: "review", failed: "blocked", update: "update"}[thread.response_state]) || "reply";
    return {key, ...(reasons[key] || reasons.reply)};
  }
  function awaitsUser(thread) {
    return thread.attention === "now" && (typeof thread.needs_attention === "boolean"
      ? thread.needs_attention : ["input", "action", "reply", "failed", "update"].includes(thread.response_state));
  }
  const stable = (a, b) => (a.created_at || 0) - (b.created_at || 0) || a.thread_id.localeCompare(b.thread_id);
  const stageOrder = (a, b) => Number(Boolean(b.pinned)) - Number(Boolean(a.pinned)) || stable(a, b);
  const attentionOrder = (a, b) => reason(a).rank - reason(b).rank ||
    (a.attention_since || a.created_at || 0) - (b.attention_since || b.created_at || 0) || stable(a, b);

  function status(thread) {
    if (thread.attention === "archived") return {key: "idle", label: "Closed"};
    if (thread.response_state === "working" || thread.status === "running" && thread.response_state !== "input")
      return {key: "running", label: "Working"};
    if (["input", "action", "reply", "update", "failed"].includes(thread.response_state))
      return {key: thread.response_state === "failed" ? "failed" : ["blocked", "review"].includes(reason(thread).key) ? "waiting" : "idle", label: reason(thread).label};
    return {key: "idle", label: thread.response_state === "interrupted" ? "Stopped" : phaseLabel(thread)};
  }
  function groupThreads(threads) {
    const now = threads.filter(thread => thread.attention === "now");
    const priority = now.filter(awaitsUser).sort(attentionOrder);
    return {
      priority,
      phases: phases.map(([key, label]) => ({key, label,
        threads: now.filter(thread => !awaitsUser(thread) && phase(thread) === key).sort(stageOrder),
        attention: priority.filter(thread => phase(thread) === key).length,
      })),
      later: threads.filter(thread => thread.attention === "later").sort(stageOrder),
      closed: threads.filter(thread => thread.attention === "archived").sort(stable),
    };
  }
  return {awaitsUser, groupThreads, status, phase, phaseLabel, reason, attentionOrder};
})();

if (typeof module !== "undefined") module.exports = threadGrouping;
