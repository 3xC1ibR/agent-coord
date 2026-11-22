"use strict";
(() => {
  // Display copy for the asks view. The view's name and its wording are
  // defaults the owner may change: change them here, not in structure.
  const AsksCopy = {
    viewName: "Queue",
    summary: "What agents need from you.",
    sections: {open: "Open", snoozed: "Snoozed", resolved: "Resolved"},
    kinds: {decision: "Decision", action: "Action", approval: "Approval", review: "Review", request: "Request"},
    // The control that records each resolution, and how a recorded resolution reads.
    resolve: {answered: "Answer", done: "Mark done", not_done: "Not done", approved: "Approve", declined: "Decline",
      accepted: "Accept", changes_requested: "Request changes", granted: "Grant", dismissed: "Dismiss"},
    resolved: {answered: "Answered", done: "Done", not_done: "Not done", approved: "Approved", declined: "Declined",
      accepted: "Accepted", changes_requested: "Changes requested", granted: "Granted", dismissed: "Dismissed"},
    groups: {blocking: "Blocking", overdue: "Overdue", unassigned: "No project"},
    empty: {
      open: "Nothing needs you. Agents file an ask here when they need a decision, an action, an approval, a review or a resource.",
      snoozed: "No snoozed asks.",
      resolved: "No resolved asks yet. Each ask you resolve is recorded here.",
      filtered: "No resolved asks match these filters.",
    },
    entryTitle(count, blocking) {
      if (!count) return this.viewName;
      return count + (count === 1 ? " ask" : " asks") + " waiting for you" + (blocking ? " · " + blocking + " blocking" : "");
    },
  };
  if (typeof module !== "undefined") module.exports = {AsksCopy};
  globalThis.AsksCopy = AsksCopy;
})();
