"use strict";
(() => {
  const localInput = date => [date.getFullYear(), String(date.getMonth() + 1).padStart(2, "0"), String(date.getDate()).padStart(2, "0")].join("-") +
    "T" + [date.getHours(), date.getMinutes()].map(value => String(value).padStart(2, "0")).join(":");
  function scheduledTime(value, now = Date.now()) {
    const date = new Date(value);
    if (!value || !Number.isFinite(date.getTime()) || date.getTime() <= now || localInput(date) !== value) {
      throw new Error("Choose a future date and time that exists in your local time zone.");
    }
    return date.getTime() / 1000;
  }
  class ScheduledPromptsUI {
    constructor({document, api, getDraft, openThread, onSaved, onError}) {
      Object.assign(this, {document, api, getDraft, openThread, onSaved, onError});
      this.$ = id => document.getElementById(id);
      this.catalogs = new Map();
      if (typeof ThreadMentions !== "undefined") {
        this.mentions = new ThreadMentions({input: this.$("schedule-message"), menu: this.$("schedule-mentions"), references: this.$("schedule-references"),
          getThread: () => this.requestId, canComplete: () => this.$("schedule-dialog").open && !this.saving,
          loadThreads: async () => { const results = await Promise.all([this.api("threads"), this.api("threads?archived=true")]); return results.flatMap(r=>r.data); }});
        this.$("schedule-message").addEventListener("keydown", event => { if (!event.isComposing) this.mentions.keydown(event); });
      }
      this.$("schedule-prompt").onclick = () => this.open().catch(onError);
      this.$("scheduled-prompts").onclick = () => this.showList().catch(onError);
      this.$("new-schedule").onclick = () => this.open().catch(onError);
      this.$("schedule-model").onchange = () => this.efforts();
      this.$("schedule-time").oninput = () => this.previewTime();
      this.$("schedule-form").onsubmit = event => { event.preventDefault(); return this.save(); };
      this.$("schedules-dialog").addEventListener("close", () => { clearInterval(this.timer); });
    }

    node(tag, text, className) {
      const node = this.document.createElement(tag);
      if (text != null) node.textContent = text;
      if (className) node.className = className;
      return node;
    }

    error(id, error) { this.$(id).hidden = !error; this.$(id).textContent = error?.message || ""; }

    async open(item = null) {
      const draft = item ? {settings: item.settings, message: item.message, mentions: item.mentions} : this.getDraft();
      if (draft.images?.length) throw new Error("Scheduled prompts currently support text. Remove the attachments before scheduling.");
      if (this.saving || this.opening) return;
      this.opening = true;
      try {
        this.editing = item;
        this.requestId = item?.id || crypto.randomUUID();
        this.draft = draft;
        this.$("schedules-dialog").close();
        this.$("schedule-title").textContent = item ? "Edit scheduled prompt" : "Schedule prompt";
        this.error("schedule-error", null);
        this.$("schedule-message").value = draft.message || "";
        this.mentions?.restore(this.requestId, draft.message || "", draft.mentions || []);
        this.$("schedule-workspace").value = draft.settings.cwd || "";
        this.$("schedule-full-access").checked = !!draft.settings.yolo;
        const tomorrow = new Date(); tomorrow.setDate(tomorrow.getDate() + 1); tomorrow.setHours(0, 0, 0, 0);
        this.$("schedule-time").value = localInput(item ? new Date(item.run_at * 1000) : tomorrow);
        this.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
        this.$("schedule-timezone").textContent = this.timezone;
        this.previewTime();
        this.$("schedule-save").disabled = true;
        this.$("schedule-model").replaceChildren();
        this.$("schedule-effort").replaceChildren();
        this.$("schedule-dialog").showModal();
        const results = await Promise.allSettled(["codex", "claude"].map(async client => {
          const models = (await this.api("models?client=" + client)).data;
          this.catalogs.set(client, models);
        }));
        for (const [index, client] of ["codex", "claude"].entries()) {
          if (results[index].status === "rejected") this.catalogs.delete(client);
        }
        const picker = this.$("schedule-model");
        for (const [client, models] of this.catalogs) {
          const group = this.node("optgroup"); group.label = client === "codex" ? "Codex" : "Claude Code";
          for (const model of models) {
            const option = this.node("option", model.displayName || model.model);
            option.value = JSON.stringify([client, model.model]); group.append(option);
          }
          picker.append(group);
        }
        const desired = JSON.stringify([draft.settings.client || "codex", draft.settings.model]);
        const options = [...picker.options];
        const preferred = this.catalogs.get(draft.settings.client || "codex") || [];
        const fallback = preferred.find(model => model.isDefault) || preferred[0];
        picker.value = options.some(option => option.value === desired) ? desired :
          fallback ? JSON.stringify([draft.settings.client || "codex", fallback.model]) : options[0]?.value || "";
        this.efforts(draft.settings.effort);
        if (draft.settings.model && !options.some(option => option.value === desired)) {
          this.error("schedule-error", new Error("The saved model is unavailable. Choose an available model before saving."));
          picker.value = ""; this.efforts();
        } else if (!options.length) {
          this.error("schedule-error", new Error("No models are available. Check your provider connection and reopen this form."));
        }
      } finally {
        this.opening = false;
        this.$("schedule-save").disabled = !this.$("schedule-model").value;
      }
    }

    efforts(preferred) {
      const [client, model] = JSON.parse(this.$("schedule-model").value || "[]");
      const selected = this.catalogs.get(client)?.find(item => item.model === model);
      const picker = this.$("schedule-effort"); picker.replaceChildren();
      for (const effort of selected?.supportedReasoningEfforts || []) {
        const option = this.node("option", effort.reasoningEffort); option.value = effort.reasoningEffort; picker.append(option);
      }
      if (!picker.options.length) { const option = this.node("option", "Model default"); option.value = ""; picker.append(option); }
      picker.value = [...picker.options].some(option => option.value === preferred) ? preferred : selected?.defaultReasoningEffort || picker.options[0].value;
      this.$("schedule-save").disabled = !selected || this.saving;
    }

    previewTime() {
      const date = new Date(this.$("schedule-time").value);
      this.$("schedule-preview").textContent = Number.isFinite(date.getTime()) ?
        date.toLocaleString(undefined, {dateStyle: "full", timeStyle: "long"}) : "Choose a date and time.";
    }

    async save() {
      if (this.saving || this.opening) return;
      this.saving = true; this.$("schedule-save").disabled = true; this.error("schedule-error", null);
      try {
        const [client, model] = JSON.parse(this.$("schedule-model").value || "[]");
        if (!model) throw new Error("Choose a model.");
        const settings = {cwd: this.$("schedule-workspace").value, client, model,
          effort: this.$("schedule-effort").value || null, yolo: this.$("schedule-full-access").checked};
        for (const key of ["repository_id", "project_id"]) {
          if (Object.hasOwn(this.draft.settings, key)) settings[key] = this.draft.settings[key];
        }
        const body = {message: this.$("schedule-message").value, run_at: scheduledTime(this.$("schedule-time").value), timezone: this.timezone, settings};
        const mentions = this.mentions?.snapshot() || [];
        if (mentions.length) body.mentions = mentions;
        if (this.editing) await this.api("schedules/" + encodeURIComponent(this.editing.id), {...body, action: "edit", version: this.editing.version});
        else await this.api("schedules", {...body, id: this.requestId});
        this.$("schedule-dialog").close();
        this.onSaved?.(this.draft);
        await this.showList();
      } catch (error) { this.error(this.$("schedule-dialog").open ? "schedule-error" : "schedules-error", error); }
      finally { this.saving = false; this.$("schedule-save").disabled = false; }
    }

    async showList() {
      this.error("schedules-error", null);
      if (!this.$("schedules-dialog").open) this.$("schedules-dialog").showModal();
      await this.refresh();
      clearInterval(this.timer);
      this.timer = setInterval(() => {
        if (this.$("schedules-dialog").open && !this.mutating) this.refresh().catch(error => this.error("schedules-error", error));
      }, 5000);
    }

    async refresh() {
      const items = (await this.api("schedules")).data;
      const list = this.$("schedule-list"); list.replaceChildren();
      if (!items.length) list.append(this.node("p", "No scheduled prompts yet.", "muted"));
      const labels = {scheduled: "Scheduled", starting: "Starting", running: "Running", completed: "Completed", failed: "Failed", missed: "Missed", cancelled: "Cancelled", review: "Needs review"};
      for (const item of items) {
        const card = this.node("article", null, "schedule-card");
        const title = this.node("h3", item.message);
        const time = new Date(item.run_at * 1000).toLocaleString(undefined, {timeZone: item.timezone, dateStyle: "medium", timeStyle: "short"});
        card.append(title, this.node("p", `${labels[item.state] || item.state} · ${time} · ${item.timezone}`, "schedule-meta"),
          this.node("p", `${item.settings.model} · ${item.settings.effort || "Model default"} · ${item.settings.yolo ? "Full access" : "Workspace access"}`, "muted"),
          this.node("p", item.cwd, "muted"));
        if (item.error) card.append(this.node("p", item.error, "schedule-error"));
        const actions = this.node("div", null, "dialog-actions");
        const button = (label, fn) => {
          const node = this.node("button", label, "quiet"); node.type = "button";
          node.onclick = async () => {
            if (this.mutating) return;
            this.mutating = true; node.disabled = true;
            try { await fn(); } catch (error) { this.error("schedules-error", error); }
            finally { this.mutating = false; node.disabled = false; }
          }; actions.append(node);
        };
        if (["scheduled", "missed"].includes(item.state)) {
          button("Edit", () => this.open(item));
          button("Cancel", async () => { await this.api("schedules/" + item.id, {action: "cancel", version: item.version}); await this.refresh(); });
        }
        if (item.state === "missed") button("Run now", async () => {
          await this.api("schedules/" + item.id, {action: "run_now", version: item.version}); await this.refresh();
        });
        if (item.thread_id) button("Open thread", async () => { await this.openThread(item.thread_id); this.$("schedules-dialog").close(); });
        card.append(actions); list.append(card);
      }
    }
  }
  globalThis.ScheduledPromptsUI = ScheduledPromptsUI;
  if (typeof module !== "undefined") module.exports = {ScheduledPromptsUI, localInput, scheduledTime};
})();
