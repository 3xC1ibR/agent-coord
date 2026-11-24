"use strict";

// Stage, attention reason, and saved placement are independent.
const threadGrouping = (() => {
  const phases = [
    ["new", "New"], ["investigation", "Investigating"],
    ["planning", "Planning"], ["orchestrating", "Orchestrating"], ["implementation", "Implementing"],
    ["validation", "Validating"], ["deployment", "Deploying"], ["finished", "Done"],
  ];
  const reasons = {
    blocked: {rank: 0, label: "Blocked"}, approval: {rank: 1, label: "Approval needed"},
    review: {rank: 1, label: "Review requested"},
    update: {rank: 2, label: "Update"}, findings: {rank: 3, label: "Findings ready"},
    reply: {rank: 3, label: "Reply"}, done: {rank: 4, label: "✓ Done"},
    snooze: {rank: 2, label: "Reminder"},
  };
  function phase(thread) {
    const value = thread.work_phase || thread.checkpoint?.phase;
    return value === "discussion" ? "investigation" : phases.some(([key]) => key === value) ? value : "new";
  }
  function phaseLabel(thread) { return phases.find(([key]) => key === phase(thread))[1]; }
  function reason(thread) {
    if (thread.status === "stalled") return {key: "blocked", rank: 0, label: "Stalled"};
    if (thread.response_state === "failed") return {key: "blocked", rank: 0, label: "Failed"};
    if (thread.open_ask && thread.open_ask.blocking !== false) return {key: "blocked", ...reasons.blocked};
    if (thread.status === "needs input" || thread.attention_key?.startsWith("requests:"))
      return {key: "approval", ...reasons.approval};
    const classification = thread.response_classification;
    const classified = classification?.status === "classified" ? classification.choice : null;
    const key = thread.attention_reason || classified ||
      ({input: "blocked", action: "review", update: "update"}[thread.response_state]) || "reply";
    if (key === "done" && thread.response_state === "update" && !thread.checkpoint_stale &&
        thread.checkpoint?.phase === "finished" && thread.checkpoint.next_actor === "nobody" &&
        thread.work_phase === "finished" && thread.turn_completion?.status === "completed" &&
        !thread.open_ask && !["running", "needs input"].includes(thread.status))
      return {key, rank: reasons.done.rank, label: "✓ Done · No action needed"};
    return {key, ...(reasons[key] || reasons.reply)};
  }
  function awaitsUser(thread) {
    return thread.attention !== "archived" && !thread.snoozed && (typeof thread.needs_attention === "boolean"
      ? thread.needs_attention : thread.attention === "now" && ["input", "action", "reply", "failed", "update"].includes(thread.response_state));
  }
  function acknowledgedRequest(thread) {
    // Dismiss keeps the saved request for context; live provider input is separate.
    return ["input", "action"].includes(thread.response_state) && thread.needs_attention === false &&
      thread.unhandled_response === false && thread.can_handle_response === false &&
      !["running", "needs input"].includes(thread.status);
  }
  function actionFor(thread) {
    if (thread.can_handle_response === false || thread.attention === "archived" || thread.snoozed ||
        ["running", "needs input"].includes(thread.status) ||
        thread.open_ask && thread.open_ask.blocking !== false || acknowledgedRequest(thread)) return null;
    const items = thread.attention_items;
    const own = Array.isArray(items) ? items.find(item => item.thread_id === thread.thread_id) : null;
    if (items?.length && !own) return null;
    const key = own?.reason || reason(thread).key;
    if (!["blocked", "review"].includes(key)) return null;
    return (thread.can_handle_response ?? thread.unhandled_response ?? awaitsUser(thread)) ? "dismiss" : null;
  }
  const stable = (a, b) => (a.created_at || 0) - (b.created_at || 0) || a.thread_id.localeCompare(b.thread_id);
  const activityAt = thread => thread.updated_at || thread.created_at || 0;
  const attentionAt = thread => thread.attention_since || thread.turn_completion?.completed_at ||
    thread.checkpoint?.created_at || thread.created_at || 0;
  const stageOrder = (a, b) => Number(status(b).key === "running") - Number(status(a).key === "running") ||
    activityAt(b) - activityAt(a) || stable(a, b);
  const attentionOrder = (a, b) =>
    reason(a).rank - reason(b).rank || Number(!!b.pinned) - Number(!!a.pinned) ||
    attentionAt(b) - attentionAt(a) || stable(a, b);

  function status(thread) {
    if (thread.attention === "archived") return {key: "idle", label: "Closed"};
    if (thread.status === "stalled") return {key: "waiting", label: "Stalled"};
    if (thread.response_state === "working" || thread.status === "running" && thread.response_state !== "input")
      return {key: "running", label: "Working"};
    const acknowledged = acknowledgedRequest(thread);
    if (thread.snooze_due && (acknowledged || !["input", "action", "failed"].includes(thread.response_state)))
      return {key: "idle", label: "Reminder"};
    if (!acknowledged && ["input", "action", "reply", "update", "failed"].includes(thread.response_state))
      return {key: thread.response_state === "failed" ? "failed" : ["blocked", "approval", "review"].includes(reason(thread).key) ? "waiting" : "idle", label: reason(thread).label};
    return {key: "idle", label: thread.response_state === "interrupted" ? "Stopped" : phaseLabel(thread)};
  }
  function cardAge(thread, now = Date.now() / 1000) {
    // Required actions and deliberately pinned work must not disappear with age.
    if (thread.pinned || thread.snooze_due || status(thread).key === "running" ||
        !acknowledgedRequest(thread) && (["input", "action", "failed"].includes(thread.response_state) ||
        (!thread.checkpoint_stale && thread.checkpoint?.next_actor === "user" && thread.checkpoint.next_action))) return "fresh";
    const activity = activityAt(thread);
    if (!activity) return "fresh";
    const hours = (now - activity) / 3600;
    return hours >= 24 ? "old" : hours >= 6 ? "settled" : "fresh";
  }
  function groupThreads(threads) {
    const visible = new Set(threads.map(thread => thread.thread_id));
    threads = threads.filter(thread => !visible.has(thread.attention_grouped_under));
    const now = threads.filter(thread => thread.attention === "now");
    const priority = threads.filter(awaitsUser).sort(attentionOrder);
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
  return {awaitsUser, acknowledgedRequest, actionFor, groupThreads, status, phase, phaseLabel, reason, attentionOrder, activityAt, cardAge};
})();

if (typeof module !== "undefined") module.exports = threadGrouping;
