"use strict";

// Stage, attention reason, and saved placement are independent.
const threadGrouping = (() => {
  const phases = [
    ["new", "New"], ["investigation", "Investigating"],
    ["planning", "Planning"], ["orchestrating", "Orchestrating"], ["implementation", "Implementing"],
    ["validation", "Validating"], ["deployment", "Deploying"], ["finished", "Done"],
  ];
  const reasons = {
    blocked: {rank: 0, label: "Blocked"}, review: {rank: 1, label: "Review requested"},
    update: {rank: 2, label: "Update"}, findings: {rank: 3, label: "Findings ready"},
    reply: {rank: 3, label: "Reply"}, done: {rank: 4, label: "✓ Done"},
    snooze: {rank: 2, label: "Snooze ended"},
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
  const activityAt = thread => thread.updated_at || thread.created_at || 0;
  const stageOrder = (a, b) => Number(status(b).key === "running") - Number(status(a).key === "running") ||
    activityAt(b) - activityAt(a) || stable(a, b);
  const attentionOrder = (a, b) => reason(a).rank - reason(b).rank ||
    (a.attention_since || a.created_at || 0) - (b.attention_since || b.created_at || 0) || stable(a, b);

  function status(thread) {
    if (thread.attention === "archived") return {key: "idle", label: "Closed"};
    if (thread.response_state === "working" || thread.status === "running" && thread.response_state !== "input")
      return {key: "running", label: "Working"};
    if (thread.snooze_due && !["input", "action", "failed"].includes(thread.response_state))
      return {key: "idle", label: "Snooze ended"};
    if (["input", "action", "reply", "update", "failed"].includes(thread.response_state))
      return {key: thread.response_state === "failed" ? "failed" : ["blocked", "review"].includes(reason(thread).key) ? "waiting" : "idle", label: reason(thread).label};
    return {key: "idle", label: thread.response_state === "interrupted" ? "Stopped" : phaseLabel(thread)};
  }
  function cardAge(thread, now = Date.now() / 1000) {
    // Required actions and deliberately pinned work must not disappear with age.
    if (thread.pinned || thread.snooze_due || status(thread).key === "running" ||
        ["input", "action", "failed"].includes(thread.response_state) ||
        (!thread.checkpoint_stale && thread.checkpoint?.next_actor === "user" && thread.checkpoint.next_action)) return "fresh";
    const activity = activityAt(thread);
    if (!activity) return "fresh";
    const hours = (now - activity) / 3600;
    return hours >= 24 ? "old" : hours >= 6 ? "settled" : "fresh";
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
  return {awaitsUser, groupThreads, status, phase, phaseLabel, reason, attentionOrder, activityAt, cardAge};
})();

if (typeof module !== "undefined") module.exports = threadGrouping;
