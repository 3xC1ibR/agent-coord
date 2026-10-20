"use strict";

(() => {
  const providers = {codex: "Codex", claude: "Claude Code", muse: "Muse Code", kimi: "Kimi", antigravity: "Antigravity"};
  const scopes = {direct: "This agent", including_native_children: "Includes provider subagents", native_children: "Provider subagents"};
  const count = value => Number.isSafeInteger(value) && value >= 0;
  const exact = value => new Intl.NumberFormat("en-US").format(value);
  const compact = value => new Intl.NumberFormat("en-US", {notation: "compact", maximumFractionDigits: 1}).format(value);

  function view(usage) {
    const totals = usage?.totals || {};
    const known = count(totals.total_tokens);
    const partial = usage?.status !== "available";
    const rows = [];
    for (const [key, label] of [["input_tokens", "Input"], ["output_tokens", "Output"], ["total_tokens", "Total"],
      ["cached_input_tokens", "Cache read · part of input"], ["cache_write_input_tokens", "Cache write · part of input"],
      ["reasoning_output_tokens", "Reasoning · part of output"]]) {
      if (count(totals[key])) rows.push([label, exact(totals[key])]);
    }
    const context = usage?.context;
    const hasContext = count(context?.used_tokens) && count(context?.limit_tokens) && context.limit_tokens > 0;
    const contextText = hasContext ? `${exact(context.used_tokens)} / ${exact(context.limit_tokens)} tokens` : null;
    const contextPercent = hasContext ? Math.min(100, Math.round(context.used_tokens / context.limit_tokens * 100)) : null;
    const summary = known ? `${compact(totals.total_tokens)} tokens${partial ? " · partial" : ""}`
      : `Tokens unavailable${hasContext ? ` · context ${contextPercent}%` : ""}`;
    const models = (Array.isArray(usage?.models) ? usage.models : []).filter(model => count(model?.totals?.total_tokens))
      .map(model => [typeof model.model === "string" && model.model ? model.model : "Unknown model", exact(model.totals.total_tokens)]);
    const note = typeof usage?.note === "string" && usage.note ? usage.note
      : known && partial ? "Some usage may be missing from the available records."
      : !known ? "This provider has not supplied consumption totals for this thread." : "";
    return {summary, rows, contextText, models, note,
      provider: providers[usage?.provider] || "Provider",
      scope: scopes[usage?.scope] || "Accounting scope unavailable"};
  }

  function render(element, usage, threadId) {
    if (!element) return;
    element.hidden = !threadId;
    if (!threadId) { element.open = false; element.replaceChildren(); delete element.dataset.signature; return; }
    const signature = JSON.stringify([threadId, usage || null]);
    if (element.dataset.signature === signature) return;
    if (element.dataset.threadId !== threadId) element.open = false;
    element.dataset.threadId = threadId;
    element.dataset.signature = signature;
    const document = element.ownerDocument;
    const make = (tag, text, className) => {
      const node = document.createElement(tag);
      if (text != null) node.textContent = text;
      if (className) node.className = className;
      return node;
    };
    const value = view(usage);
    const summary = make("summary", value.summary);
    const panel = make("div", null, "thread-usage-panel");
    panel.append(make("h3", "Token usage"), make("p", `${value.provider} · ${value.scope}`, "thread-usage-scope"));
    const rows = (entries, className) => {
      const list = make("dl", null, className);
      for (const [label, text] of entries) list.append(make("dt", label), make("dd", text));
      return list;
    };
    if (value.rows.length) panel.append(rows(value.rows));
    if (value.models.length) panel.append(make("h4", "By model"), rows(value.models, "thread-usage-models"));
    if (value.contextText) panel.append(make("h4", "Last reported context"), make("p", value.contextText),
      make("p", "Context measures capacity in use, not total tokens consumed.", "thread-usage-note"));
    if (value.note) panel.append(make("p", value.note, "thread-usage-note"));
    panel.append(make("p", "App-agent threads account for their own usage separately.", "thread-usage-note"));
    element.replaceChildren(summary, panel);
  }

  globalThis.agentCoordThreadUsage = {render, view};
})();
