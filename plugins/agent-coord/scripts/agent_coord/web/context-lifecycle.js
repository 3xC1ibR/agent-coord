"use strict";

(() => {
  const labels = {reuse: "Reuse this conversation", fresh: "Fresh context recommended", either: "Either context works"};
  const lists = [
    ["reuse_for", "Useful context for"], ["fresh_for", "Prefer fresh context for"],
  ];
  const handoverLists = [
    ["confirmed_facts", "Confirmed facts"], ["constraints", "Constraints"],
    ["tried_and_failed", "Tried and ruled out"], ["open_questions", "Open questions"],
  ];

  function render(host, work, {sameThread = false, opened = new Set()} = {}) {
    if (!host || !work) return;
    const document = host.ownerDocument;
    const make = (tag, text, className) => {
      const element = document.createElement(tag);
      if (text != null) element.textContent = text;
      if (className) element.className = className;
      return element;
    };
    const section = make("section", null, "context-lifecycle-panel");
    section.setAttribute("aria-label", "Context reuse");
    section.append(make("h3", "Context reuse"));
    const checkpoint = work.checkpoint;
    const report = checkpoint?.context_lifecycle;
    if (!report || !Object.hasOwn(labels, report.recommendation)) {
      section.append(make("p", "No context reuse advice saved yet.", "muted"));
      host.append(section);
      return;
    }
    section.append(make("p", labels[report.recommendation], "context-lifecycle-recommendation"));
    if (work.checkpoint_stale) section.append(make("p", "Saved before the current turn. Review before reusing.", "context-lifecycle-stale"));
    if (typeof report.reason === "string" && report.reason) section.append(make("p", report.reason));
    const appendList = (parent, [key, title], source) => {
      const items = Array.isArray(source?.[key]) ? source[key].filter(item => typeof item === "string" && item.trim()) : [];
      if (!items.length) return false;
      const list = make("ul");
      for (const item of items) list.append(make("li", item));
      parent.append(make("h4", title), list);
      return true;
    };
    for (const entry of lists) appendList(section, entry, report);
    const handover = make("details");
    handover.dataset.section = "context-handover";
    handover.open = sameThread && opened.has("context-handover");
    handover.append(make("summary", "Handover"));
    let hasHandover = false;
    for (const entry of handoverLists) hasHandover = appendList(handover, entry, report.handover) || hasHandover;
    if (hasHandover) section.append(handover);
    host.append(section);
  }

  globalThis.agentCoordContextLifecycle = {render};
})();
