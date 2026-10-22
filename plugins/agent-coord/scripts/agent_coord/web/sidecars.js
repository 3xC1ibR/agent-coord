"use strict";
(() => {
  const labels = {watching: "Watching", reviewing: "Reviewing", paused: "Paused", needs_attention: "Needs attention", complete: "Complete", detached: "Detached"};
  class SidecarsUI {
    constructor({document, api, getThreads, openThread, onError}) {
      Object.assign(this, {document, api, getThreads, openThread, onError});
      this.sequence = 0;
      this.catalogSequence = 0;
      this.dialog = this.node("dialog", "", "sidecar-dialog");
      this.dialog.setAttribute("aria-labelledby", "sidecar-heading");
      this.document.body.append(this.dialog);
      this.timer = setInterval(() => { if (this.thread && !this.document.hidden) this.refresh().catch(onError); }, 5000);
    }
    node(tag, text, className) {
      const element = this.document.createElement(tag);
      if (text) element.textContent = text;
      if (className) element.className = className;
      return element;
    }
    button(text, callback) {
      const button = this.node("button", text, "quiet"); button.type = "button";
      button.onclick = () => Promise.resolve().then(callback).catch(this.onError);
      return button;
    }
    field(label, input, parent) {
      const wrapper = this.node("label", label); wrapper.append(input); parent.append(wrapper); return input;
    }
    option(value, label) {
      const option = this.node("option", label); option.value = value; return option;
    }
    showThread(thread, container) {
      const id = thread?.browser_session ? thread.thread_id : null;
      this.container = container;
      if (this.thread === id) return;
      this.thread = id; this.sequence++;
      container.replaceChildren(); container.hidden = true;
      if (id) this.refresh().catch(this.onError);
    }
    async refresh() {
      const id = this.thread, sequence = ++this.sequence;
      if (!id) return;
      const data = await this.api("threads/" + encodeURIComponent(id) + "/sidecars");
      if (id !== this.thread || sequence !== this.sequence) return;
      this.render(data);
    }
    render(data) {
      const container = this.container;
      if (!container) return;
      container.replaceChildren();
      for (const watch of data.watches) {
        const targetView = watch.target_id === this.thread;
        const row = this.node("div", "", "sidecar-row");
        const description = this.node("div", "", "sidecar-description");
        description.append(this.node("strong", targetView ? watch.sidecar_title : "Watching " + watch.target_title),
          this.node("span", labels[watch.status] || watch.status, "sidecar-status"));
        if (watch.waiting_reason || watch.finding) description.append(this.node("p", watch.waiting_reason || watch.finding));
        const controls = this.node("div", "", "sidecar-controls");
        controls.append(this.button(targetView ? "Open" : "Open target", () => this.openThread(targetView ? watch.sidecar_id : watch.target_id)));
        if (targetView && !["complete", "detached"].includes(watch.state)) {
          const change = async action => {
            for (const button of controls.children) button.disabled = true;
            try {
              await this.api("threads/" + encodeURIComponent(watch.target_id) + "/sidecars", {action, id: watch.id});
              await this.refresh();
            } finally { for (const button of controls.children) button.disabled = false; }
          };
          controls.append(this.button(watch.state === "active" ? "Pause" : "Resume", () => change(watch.state === "active" ? "pause" : "resume")),
            this.button("Detach", () => change("detach")));
        }
        row.append(description, controls); container.append(row);
      }
      for (const request of data.requests) {
        const row = this.node("div", "", "sidecar-row");
        row.append(this.node("span", request.error || (request.status === "queued" ? "Sidecar queued for attachment…" : "Attaching sidecar…")));
        if (request.thread_id) row.append(this.button("Open agent", () => this.openThread(request.thread_id)));
        if (request.status === "queued") {
          const target = this.thread;
          row.append(this.button("Cancel", async () => {
            await this.api("threads/" + encodeURIComponent(target) + "/sidecars", {cancel_request: request.request_id});
            await this.refresh();
          }));
        }
        container.append(row);
      }
      container.hidden = !container.children.length;
    }
    async open(thread) {
      if (!thread?.browser_session || thread.attention === "archived") return;
      const dialog = this.dialog;
      this.target = thread; this.requestId = crypto.randomUUID(); this.submitted = null;
      const generation = ++this.catalogSequence;
      dialog.replaceChildren();
      const heading = this.node("h2", "Attach sidecar"); heading.id = "sidecar-heading";
      dialog.append(heading, this.node("p", "Watch “" + thread.title + "” for unfinished work or missing evidence."));
      const form = this.node("form"); dialog.append(form);
      this.form = form;
      const purpose = this.field("What should it watch for?", this.node("textarea"), form);
      purpose.required = true; purpose.maxLength = 4000; purpose.rows = 4;
      purpose.value = "Ensure the current task meets the user's acceptance criteria. Check completion claims against evidence and identify unfinished work.";
      const mode = this.field("What can it do?", this.node("select"), form);
      mode.append(this.option("notify", "Notify me"), this.option("message", "Notify me and message the agent"));
      const existing = this.field("Sidecar agent", this.node("select"), form);
      existing.append(this.option("", "Create a dedicated agent"));
      for (const peer of this.getThreads()) {
        if (peer.thread_id !== thread.thread_id && peer.browser_session && peer.attention !== "archived" && peer.cwd === thread.cwd && !peer.turn_active && peer.status !== "running") {
          existing.append(this.option(peer.thread_id, peer.title));
        }
      }
      const settings = this.node("div", "", "sidecar-settings"); form.append(settings);
      const client = this.field("Provider", this.node("select"), settings);
      for (const [value, label] of [["codex", "Codex"], ["claude", "Claude Code"], ["antigravity", "Antigravity"], ["kimi", "Kimi Code"], ["muse", "Muse Code"]]) client.append(this.option(value, label));
      client.value = thread.client || "codex";
      const model = this.field("Model", this.node("select"), settings);
      const effort = this.field("Reasoning", this.node("select"), settings);
      const continuous = this.field("Keep watching future assignments", this.node("input"), form); continuous.type = "checkbox";
      const limits = this.node("details"); limits.append(this.node("summary", "Review limits")); form.append(limits);
      const reviews = this.field("Maximum reviews", this.node("input"), limits); reviews.type = "number"; reviews.min = "1"; reviews.max = "100"; reviews.value = "20"; reviews.required = true;
      const interventions = this.field("Maximum corrective messages", this.node("input"), limits); interventions.type = "number"; interventions.min = "1"; interventions.max = "10"; interventions.value = "2"; interventions.required = true;
      form.append(this.node("p", "Reviews use the selected agent's model. The sidecar receives saved messages and tool results from this conversation. This monitors work after it happens; it does not block actions.", "sidecar-note"));
      const error = this.node("p", "", "sidecar-error"); error.setAttribute("role", "alert"); form.append(error);
      const actions = this.node("div", "", "sidecar-controls"); form.append(actions);
      const cancel = this.button("Cancel", () => dialog.close());
      const submit = this.node("button", "Attach"); submit.type = "submit"; submit.disabled = true;
      actions.append(cancel, submit);
      let models = [], loading = false, flight = 0;
      const updateEfforts = () => {
        effort.replaceChildren(this.option("", "Model default"));
        const selected = models.find(item => item.model === model.value);
        for (const item of selected?.supportedReasoningEfforts || []) effort.append(this.option(item.reasoningEffort, item.reasoningEffort));
      };
      const loadModels = async () => {
        const current = ++flight; loading = true; submit.disabled = !existing.value; error.textContent = "";
        model.disabled = true;
        try {
          const result = await this.api("models?client=" + encodeURIComponent(client.value));
          if (generation !== this.catalogSequence || current !== flight) return;
          models = result.data; model.replaceChildren();
          for (const item of models) model.append(this.option(item.model, item.displayName || item.model));
          if (!models.length) throw new Error("No models available for this provider.");
          updateEfforts();
        } catch (reason) {
          if (generation === this.catalogSequence && current === flight) { models = []; error.textContent = reason.message; }
        } finally {
          if (generation === this.catalogSequence && current === flight) {
            loading = false; model.disabled = !models.length; submit.disabled = !existing.value && !models.length;
          }
        }
      };
      client.onchange = loadModels; model.onchange = updateEfforts;
      existing.onchange = () => { settings.hidden = !!existing.value; submit.disabled = !existing.value && (loading || !models.length); };
      form.onsubmit = async event => {
        event.preventDefault(); if (submit.disabled) return;
        // A failed transport retry keeps the exact key and body; never create twice.
        this.submitted ||= {request_id: this.requestId, purpose: purpose.value, mode: mode.value,
          continuous: continuous.checked, max_reviews: Number(reviews.value), max_interventions: Number(interventions.value),
          ...(existing.value ? {existing_id: existing.value} : {client: client.value, model: model.value, effort: effort.value || null})};
        for (const control of form.querySelectorAll("input, select, textarea, button")) control.disabled = true;
        try {
          await this.api("threads/" + encodeURIComponent(thread.thread_id) + "/sidecars", this.submitted);
          dialog.close();
          if (this.thread !== thread.thread_id) await this.openThread(thread.thread_id);
          else await this.refresh();
        } catch (reason) {
          if (reason.status === 400) {
            this.submitted = null; this.requestId = crypto.randomUUID();
            for (const control of form.querySelectorAll("input, select, textarea, button")) control.disabled = false;
            submit.textContent = "Attach"; error.textContent = reason.message;
          } else {
            error.textContent = reason.message + " Retry uses the same attachment request.";
            submit.disabled = false; submit.textContent = "Retry attachment"; cancel.disabled = false;
          }
        }
      };
      dialog.showModal(); purpose.focus(); await loadModels();
    }
  }
  if (typeof module !== "undefined") module.exports = {SidecarsUI, labels};
  globalThis.SidecarsUI = SidecarsUI;
})();
