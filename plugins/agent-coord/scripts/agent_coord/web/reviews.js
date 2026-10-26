"use strict";
(() => {
  const standard = "Identify material errors, missed requirements, unsupported claims, and assumptions that could change the result. Explain consequences and actionable corrections or checks. Return no findings when none are material. Ignore stylistic preferences unless requested.";
  const phases = {draft: "Drafting", review: "Reviewing", revise: "Revising", recheck: "Rechecking"};
  function element(document, tag, text, className) {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  class ReviewUI {
    constructor({document, api, onError, changed}) {
      Object.assign(this, {document, api, onError, changed});
      this.settings = new Map(); this.requests = new Map();
      this.button = element(document, "button", "Review", "quiet"); this.button.type = "button";
      document.getElementById("schedule-prompt").before(this.button);
      this.button.onclick = () => this.configure().catch(onError);
      this.dialog = element(document, "dialog", "", "review-dialog"); document.body.append(this.dialog);
      this.dialog.setAttribute("aria-label", "Review this request");
    }
    render(detail, key) {
      this.detail = detail; this.key = key;
      const active = detail?.reviews?.some(run => run.active);
      this.button.hidden = !detail?.work_thread?.browser_session || detail?.work_thread?.dispatcher;
      this.button.disabled = !!active || !!detail?.running || detail?.work_thread?.attention === "archived";
      this.button.textContent = this.settings.has(key) ? "Review on ✓" : "Review";
      this.button.setAttribute("aria-pressed", String(this.settings.has(key)));
      if (active) {
        const get = id => this.document.getElementById(id);
        get("stop").hidden = false; get("queue").hidden = true;
        get("send").textContent = "Update request ↑";
        get("composer-hint").textContent = "Sending an update stops this review. Enable Review again for a new reviewed request.";
        if (detail.work_thread?.review?.critic_needs_input) {
          get("status").textContent = "Critic needs input · Open its conversation below"; get("status").hidden = false;
        }
      }
    }
    decorate(body, key) {
      if (!this.settings.has(key) || body.message.trim().startsWith("/")) return;
      if (body.images?.length) throw new Error("Review supports text answers and plans. Remove images to enable it.");
      body.review = {...this.settings.get(key)};
      // Persist the exact submission key across transport retries and page reloads.
      const fingerprint = JSON.stringify([body.message, body.mentions, body.review]);
      const storageKey = "review-request:" + key;
      let prior = this.requests.get(key);
      try { prior ||= JSON.parse(sessionStorage.getItem(storageKey)); } catch { /* Memory is sufficient within this page. */ }
      if (prior?.fingerprint !== fingerprint) prior = {fingerprint, id: crypto.randomUUID()};
      this.requests.set(key, prior); body.requestId = prior.id;
      try { sessionStorage.setItem(storageKey, JSON.stringify(prior)); } catch { /* Keep memory fallback. */ }
    }
    sent(key) {
      this.settings.delete(key); this.requests.delete(key);
      try { sessionStorage.removeItem("review-request:" + key); } catch { /* Storage may be unavailable. */ }
    }
    async configure() {
      const key = this.key, detail = this.detail, dialog = this.dialog;
      const saved = this.settings.get(key);
      dialog.replaceChildren(element(this.document, "h2", "Review this request"),
        element(this.document, "p", "A dedicated critic reviews a text answer or plan before it is delivered. One revision and recheck; at most four turns and ten minutes. This does not prevent tool actions."));
      const form = element(this.document, "form"); dialog.append(form);
      const field = (label, tag) => {
        const wrapper = element(this.document, "label", label), input = element(this.document, tag);
        wrapper.append(input); form.append(wrapper); return input;
      };
      const option = (value, text) => { const node = element(this.document, "option", text); node.value = value; return node; };
      const client = field("Critic provider", "select");
      for (const [value, label] of [["codex", "Codex"], ["claude", "Claude Code"], ["antigravity", "Antigravity"], ["kimi", "Kimi Code"], ["muse", "Muse Code"]]) client.append(option(value, label));
      client.value = saved?.client || detail.session?.client || detail.work_thread.client || "codex";
      const model = field("Critic model", "select"), effort = field("Reasoning", "select");
      const instructions = field("Review standard", "textarea"); instructions.rows = 5; instructions.required = true; instructions.maxLength = 4000;
      instructions.value = saved?.standard || standard;
      form.append(element(this.document, "p", "The critic receives your original request, earlier user messages and repository instructions. Findings distinguish checks from inference and unverified claims. Agreement is not proof of correctness."));
      const error = element(this.document, "p"); error.setAttribute("role", "alert"); form.append(error);
      const controls = element(this.document, "div", "", "review-controls"); form.append(controls);
      const off = element(this.document, "button", "Turn off"), cancel = element(this.document, "button", "Cancel"), save = element(this.document, "button", "Enable review", "primary");
      off.type = cancel.type = "button"; save.type = "submit";
      off.onclick = () => { this.sent(key); dialog.close(); this.changed(); };
      cancel.onclick = () => dialog.close(); controls.append(off, cancel, save);
      let models = [], flight = 0;
      const efforts = () => {
        effort.replaceChildren(option("", "Model default"));
        for (const item of models.find(m => m.model === model.value)?.supportedReasoningEfforts || []) effort.append(option(item.reasoningEffort, item.reasoningEffort));
        if (saved?.effort && [...effort.options].some(o => o.value === saved.effort)) effort.value = saved.effort;
      };
      const load = async () => {
        const current = ++flight; save.disabled = model.disabled = true; error.textContent = "";
        try {
          const data = await this.api("models?client=" + encodeURIComponent(client.value));
          if (current !== flight || !dialog.open) return;
          models = data.data; model.replaceChildren(...models.map(m => option(m.model, m.displayName || m.model)));
          if (!models.length) throw new Error("No models available for this provider.");
          const preferred = saved?.model || (client.value === detail.session?.client ? detail.session.model : null);
          if (models.some(m => m.model === preferred)) model.value = preferred;
          efforts(); save.disabled = model.disabled = false;
        } catch (reason) { if (current === flight && dialog.open) error.textContent = reason.message; }
      };
      client.onchange = load; model.onchange = efforts;
      form.onsubmit = event => {
        event.preventDefault(); if (save.disabled) return;
        this.settings.set(key, {client: client.value, model: model.value, effort: effort.value || null, standard: instructions.value});
        dialog.close(); this.changed();
      };
      dialog.showModal(); await load();
    }
    static turns(turns, runs, threadId) {
      if (!runs.length) return turns;
      const hidden = new Set(runs.flatMap(r => r.steps.filter(s => s.actor_id === threadId).map(s => s.turn_id)).filter(Boolean));
      const receipts = new Set(runs.map(r => "review:" + r.id));
      const output = turns.filter(t => !receipts.has(t.id)).map(t => hidden.has(t.id) || t.items?.some(i => i.type === "reviewStage")
        ? {...t, items: t.items.filter(i => !["userMessage", "agentMessage", "reasoning", "reviewStage"].includes(i.type))} : t);
      for (const run of runs) {
        const first = run.steps.find(s => s.actor_id === threadId && s.turn_id);
        const original = turns.find(t => t.id === first?.turn_id);
        output.push({id: "review:" + run.id, startedAt: original?.startedAt || run.created_at,
          items: [{type: "reviewReceipt", review: run}], status: run.active ? "inProgress" : "completed"});
      }
      const stamp = t => typeof t.startedAt === "number" ? t.startedAt : Date.parse(t.startedAt || t.started_at) / 1000;
      return output.sort((a, b) => (stamp(a) || 0) - (stamp(b) || 0));
    }
    static card(document, run, {render, open, expanded}) {
      const node = (tag, text, cls) => element(document, tag, text, cls);
      const card = node("article", "", "review-card");
      const label = run.active ? phases[run.phase] : ({complete: "Review complete", incomplete: "Review incomplete", cancelled: "Review stopped", superseded: "Review replaced"}[run.state] || run.state);
      card.append(node("p", run.packet.original_request, "review-request"), node("strong", label));
      const summary = run.steps.find(s => s.stage === "recheck")?.result || run.steps.find(s => s.stage === "review")?.result;
      if (run.active) card.append(node("p", "Draft → Review → Revision → Recheck · At most four turns / ten minutes", "muted"));
      if (run.error) card.append(node("p", run.error));
      if (run.state === "complete") {
        const unresolved = summary?.findings?.length || summary?.rechecks?.some(f => f.status !== "resolved");
        card.append(node("p", !unresolved ? "No material findings remain according to the critic. Review is not independent verification." : "Review budget complete. Material findings or disagreements remain; inspect them below."));
        const answer = node("div", "", "markdown"); answer.innerHTML = render(run.draft || ""); card.append(answer);
      }
      const details = node("details"); details.dataset.id = "review:" + run.id; details.open = !!expanded;
      details.append(node("summary", "Inspect draft, feedback and evidence")); card.append(details);
      details.append(node("p", "Review standard: " + run.standard));
      if (run.critic_id) {
        const link = node("button", "Open critic conversation", "quiet"); link.type = "button"; link.onclick = () => open(run.critic_id); details.append(link);
      }
      for (const step of run.steps) {
        if (!step.result) continue;
        const report = node("section"); report.append(node("h3", phases[step.stage]));
        if (step.result.draft) {
          const draft = node("details"); draft.append(node("summary", "Draft · " + (step.stage === "draft" ? "1" : "2") + (run.active || run.state !== "complete" ? " · Provisional" : "")));
          const text = node("div", "", "markdown"); text.innerHTML = render(step.result.draft); draft.append(text); report.append(draft);
        }
        if (step.result.coverage) report.append(node("p", "Coverage: " + step.result.coverage));
        for (const finding of step.result.findings || []) report.append(node("p", finding.id + " · " + finding.verification + ": " + finding.issue + "\nEvidence: " + finding.evidence + "\nAction: " + finding.action, "review-finding"));
        for (const item of step.result.dispositions || step.result.rechecks || []) report.append(node("p", item.id + " · " + (item.decision || item.status) + ": " + item.reason));
        details.append(report);
      }
      return card;
    }
  }
  if (typeof module !== "undefined") module.exports = {ReviewUI, standard};
  globalThis.ReviewUI = ReviewUI;
})();
