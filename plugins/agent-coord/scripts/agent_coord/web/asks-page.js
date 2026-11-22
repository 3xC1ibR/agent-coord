"use strict";
/* The asks view: open, snoozed and resolved asks, resolved in place. */
(() => {
  const copy = typeof module !== "undefined" ? require("./asks-copy.js").AsksCopy : globalThis.AsksCopy;
  const SECTIONS = ["open", "snoozed", "resolved"];
  const SNOOZE_CHOICES = [["hour", "In 1 hour"], ["three_hours", "In 3 hours"], ["tomorrow", "Tomorrow at 9 AM"], ["week", "Next week"]];
  const REFRESH_MILLISECONDS = 5000;
  const LONG_TEXT_CHARACTERS = 320, LONG_TEXT_LINES = 4;

  function formatAge(seconds) {
    if (!Number.isFinite(seconds) || seconds < 60) return "just now";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m";
    if (seconds < 86400) return Math.floor(seconds / 3600) + "h";
    return Math.floor(seconds / 86400) + "d";
  }
  function formatEffort(minutes) {
    if (!Number.isFinite(minutes) || minutes <= 0) return "";
    if (minutes < 60) return minutes + " min";
    const hours = Math.floor(minutes / 60), rest = minutes % 60;
    return hours + " h" + (rest ? " " + rest + " min" : "");
  }
  function formatDay(iso, now) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    const options = {month: "short", day: "numeric"};
    if (date.getFullYear() !== new Date(now).getFullYear()) options.year = "numeric";
    return date.toLocaleDateString(undefined, options);
  }
  function formatMoment(iso, now) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    return formatDay(iso, now) + ", " + date.toLocaleTimeString(undefined, {hour: "numeric", minute: "2-digit"});
  }
  /* Unix seconds for a snooze choice, in the user's local time. */
  function snoozeUntil(choice, now) {
    const date = new Date(now);
    if (choice === "hour") date.setHours(date.getHours() + 1);
    else if (choice === "three_hours") date.setHours(date.getHours() + 3);
    else if (choice === "tomorrow") { date.setDate(date.getDate() + 1); date.setHours(9, 0, 0, 0); }
    else if (choice === "week") { date.setDate(date.getDate() + 7); date.setHours(9, 0, 0, 0); }
    else return null;
    return Math.floor(date.getTime() / 1000);
  }
  /* The first line of an ask is its title; anything after it is detail. */
  function splitQuestion(question) {
    const text = String(question ?? "").trim();
    const end = text.indexOf("\n");
    return end < 0 ? {title: text, detail: ""} : {title: text.slice(0, end).trim(), detail: text.slice(end + 1).trim()};
  }
  /* Long supporting text starts collapsed so one ask cannot fill the view. */
  function isLong(text) {
    return text.length > LONG_TEXT_CHARACTERS || text.split("\n").length > LONG_TEXT_LINES;
  }
  /* Blocking first, then overdue, then by project; each group oldest first. */
  function openGroups(items) {
    const oldestFirst = (a, b) => b.age_seconds - a.age_seconds || a.id - b.id;
    const blocking = items.filter(ask => ask.blocking).sort(oldestFirst);
    const overdue = items.filter(ask => !ask.blocking && ask.overdue).sort(oldestFirst);
    const projects = new Map();
    for (const ask of items.filter(ask => !ask.blocking && !ask.overdue)) {
      const label = ask.project_label || "";
      if (!projects.has(label)) projects.set(label, []);
      projects.get(label).push(ask);
    }
    const groups = [];
    if (blocking.length) groups.push({key: "blocking", title: copy.groups.blocking, urgent: true, items: blocking});
    if (overdue.length) groups.push({key: "overdue", title: copy.groups.overdue, urgent: true, items: overdue});
    const labels = [...projects.keys()].sort((a, b) => !a - !b || a.localeCompare(b));
    for (const label of labels) {
      groups.push({key: "project:" + label, title: label || copy.groups.unassigned, urgent: false,
        items: projects.get(label).sort(oldestFirst)});
    }
    return groups;
  }
  /* Where an ask's link leads: a web page, a place in the app, an HTML result, or a file path to copy. */
  function linkTarget(ask) {
    const link = ask.link;
    if (!link) return null;
    if (link.startsWith("https://")) {
      let host = "";
      try { host = new URL(link).host; } catch { return null; }
      return {type: "url", href: link, label: "Open " + host};
    }
    if (link.startsWith("agentcoord://")) return {type: "app", href: link, label: "Open in Ribbon Field"};
    if (link.startsWith("/")) {
      const name = link.split("/").filter(Boolean).pop() || link;
      if (/\.html?$/i.test(link)) {
        return {type: "preview", label: "Open " + name,
          href: "/html-preview?" + new URLSearchParams({thread: ask.session_id, path: link})};
      }
      return {type: "path", path: link, label: "Copy path"};
    }
    return null;
  }

  class AsksView {
    constructor({document, api, openThread, navigate, close, copyText, clock = () => Date.now()}) {
      Object.assign(this, {document, api, openThread, navigate, close, copyText, clock});
      const $ = id => document.getElementById(id);
      this.elements = {title: $("asks-title"), summary: $("asks-summary"), close: $("close-asks"),
        sections: $("asks-sections"), filters: $("asks-filters"), project: $("asks-filter-project"),
        kind: $("asks-filter-kind"), since: $("asks-filter-since"), error: $("asks-error"),
        status: $("asks-status"), list: $("asks-list")};
      this.section = "open";
      this.open = [];
      this.resolved = [];
      this.loaded = false;
      this.resolvedLoaded = false;
      this.failure = null;
      this.sequence = 0;
      this.records = new Map();
      this.pending = new Set();
      this.signature = null;
      this.tabs = new Map();
      this.setup();
    }
    node(tag, text, className) {
      const element = this.document.createElement(tag);
      if (text != null && text !== "") element.textContent = text;
      if (className) element.className = className;
      return element;
    }
    button(text, callback, className) {
      const button = this.node("button", text, className); button.type = "button";
      button.onclick = () => Promise.resolve().then(callback).catch(error => this.fail(error));
      return button;
    }
    /* Supporting text that collapses when long, with a control to read all of it. */
    passage(text, className) {
      const paragraph = this.node("p", text, className);
      if (!isLong(text)) return [paragraph];
      paragraph.className = className + " ask-clamped";
      const toggle = this.button("Show more", () => {
        const collapsed = paragraph.className.includes("ask-clamped");
        paragraph.className = collapsed ? className : className + " ask-clamped";
        toggle.textContent = collapsed ? "Show less" : "Show more";
        toggle.setAttribute("aria-expanded", String(collapsed));
      }, "ask-more");
      toggle.setAttribute("aria-expanded", "false");
      return [paragraph, toggle];
    }
    heading(ask) {
      const {title, detail} = splitQuestion(ask.question);
      return {title: this.node("h3", title, "ask-title"), detail: detail ? this.passage(detail, "ask-detail") : []};
    }
    setup() {
      const {elements} = this;
      this.document.title = copy.viewName + " — Ribbon Field";
      elements.title.textContent = copy.viewName;
      elements.summary.textContent = copy.summary;
      elements.sections.setAttribute("aria-label", copy.viewName + " sections");
      for (const section of SECTIONS) {
        const tab = this.button("", () => this.select(section), "asks-section");
        tab.setAttribute("role", "tab");
        tab.setAttribute("aria-controls", "asks-list");
        const count = this.node("span", "", "asks-section-count");
        tab.append(this.node("span", copy.sections[section]), count);
        this.tabs.set(section, {tab, count});
        elements.sections.append(tab);
      }
      if (this.close) { elements.close.hidden = false; elements.close.onclick = () => this.close(); }
      for (const [kind, label] of Object.entries(copy.kinds)) {
        const option = this.node("option", label); option.value = kind;
        elements.kind.append(option);
      }
      for (const control of [elements.project, elements.kind, elements.since]) control.onchange = () => this.render();
      elements.filters.onsubmit = event => event?.preventDefault?.();
      this.render();
    }
    fail(error) {
      this.failure = error?.message || String(error);
      this.render();
    }
    async select(section) {
      this.section = section;
      this.render();
      if (section === "resolved") await this.refresh();
    }
    async refresh() {
      const sequence = ++this.sequence;
      try {
        const requests = [this.api("asks")];
        if (this.section === "resolved") requests.push(this.api("asks?status=resolved"));
        const [open, resolved] = await Promise.all(requests);
        if (sequence !== this.sequence) return;
        this.open = open.data || [];
        if (resolved) { this.resolved = resolved.data || []; this.resolvedLoaded = true; }
        this.loaded = true;
        this.failure = null;
      } catch (error) {
        if (sequence !== this.sequence) return;
        this.failure = error?.message || String(error);
      }
      this.render();
    }
    counts() {
      const snoozed = this.open.filter(ask => ask.snoozed).length;
      return {open: this.open.length - snoozed, snoozed, blocking: this.open.filter(ask => ask.blocking && !ask.snoozed).length};
    }
    filteredResolved() {
      const {project, kind, since} = this.elements;
      let cutoff = null;
      if (since.value) {
        const [year, month, day] = since.value.split("-").map(Number);
        cutoff = new Date(year, month - 1, day).getTime();
      }
      return this.resolved.filter(ask =>
        (!project.value || (ask.project_label || "") === project.value)
        && (!kind.value || ask.kind === kind.value)
        && (cutoff == null || new Date(ask.resolved_at).getTime() >= cutoff));
    }
    render() {
      const {elements} = this, counts = this.counts();
      for (const [section, {tab, count}] of this.tabs) {
        const selected = section === this.section;
        tab.setAttribute("aria-selected", String(selected));
        tab.className = "asks-section" + (selected ? " asks-section-selected" : "");
        const value = section === "resolved" ? "" : String(counts[section] || "");
        count.textContent = value;
        count.hidden = !value;
        count.className = "asks-section-count" + (section === "open" && counts.blocking ? " asks-section-count-blocking" : "");
      }
      elements.error.textContent = this.failure ? this.failure + " Showing the last loaded asks." : "";
      elements.error.hidden = !this.failure;
      elements.filters.hidden = this.section !== "resolved";
      const ready = this.section === "resolved" ? this.resolvedLoaded : this.loaded;
      elements.status.hidden = ready;
      elements.status.textContent = ready ? "" : this.failure ? "" : "Loading…";
      elements.list.setAttribute("aria-label", copy.sections[this.section]);
      if (!ready) { this.signature = null; elements.list.replaceChildren(); return; }
      if (this.section === "resolved") return this.renderResolved();
      this.renderOpen();
    }
    renderOpen() {
      const snoozedSection = this.section === "snoozed";
      const visible = this.open.filter(ask => Boolean(ask.snoozed) === snoozedSection);
      const groups = snoozedSection
        ? (visible.length ? [{key: "snoozed", title: "", urgent: false,
            items: visible.slice().sort((a, b) => String(a.snoozed_until).localeCompare(String(b.snoozed_until)))}] : [])
        : openGroups(visible);
      const live = new Set();
      for (const group of groups) for (const ask of group.items) {
        const key = this.section + ":" + ask.id;
        live.add(key);
        if (!this.records.has(key)) this.records.set(key, this.buildRow(ask, snoozedSection));
        this.records.get(key).update(ask);
      }
      for (const key of [...this.records.keys()]) if (!live.has(key)) this.records.delete(key);
      // Reuse rows across refreshes so a typed note, focus and selection survive.
      const signature = this.section + "|" + groups.map(group => group.key + ":" + group.items.map(ask => ask.id).join(",")).join("|");
      if (signature === this.signature) return;
      this.signature = signature;
      const {list} = this.elements;
      const active = this.document.activeElement;
      const selectionStart = active?.selectionStart, selectionEnd = active?.selectionEnd;
      list.replaceChildren();
      if (!groups.length) {
        list.append(this.node("p", copy.empty[this.section], "asks-empty"));
        return;
      }
      for (const group of groups) {
        const section = this.node("section", "", "ask-group" + (group.urgent ? " ask-group-urgent" : ""));
        if (group.title) {
          const heading = this.node("h2", group.title, "ask-group-title");
          heading.append(this.node("span", String(group.items.length), "ask-group-count"));
          section.append(heading);
        }
        for (const ask of group.items) section.append(this.records.get(this.section + ":" + ask.id).element);
        list.append(section);
      }
      if (active && list.contains?.(active)) {
        active.focus?.();
        if (selectionStart != null) active.setSelectionRange?.(selectionStart, selectionEnd);
      }
    }
    renderResolved() {
      const {elements} = this;
      this.signature = null;
      const labels = [...new Set(this.resolved.map(ask => ask.project_label || "").filter(Boolean))].sort((a, b) => a.localeCompare(b));
      const selected = elements.project.value;
      const first = elements.project.children[0];
      elements.project.replaceChildren(first, ...labels.map(label => {
        const option = this.node("option", label); option.value = label; return option;
      }));
      elements.project.value = labels.includes(selected) ? selected : "";
      const items = this.filteredResolved();
      elements.list.replaceChildren();
      if (!items.length) {
        elements.list.append(this.node("p", this.resolved.length ? copy.empty.filtered : copy.empty.resolved, "asks-empty"));
        return;
      }
      const section = this.node("section", "", "ask-group");
      for (const ask of items) section.append(this.buildResolvedRow(ask));
      elements.list.append(section);
    }
    threadControl(ask) {
      const thread = this.button(ask.thread_title, () => this.openThread(ask.session_id), "ask-thread");
      thread.title = "Open this thread";
      return thread;
    }
    linkControl(ask) {
      const target = linkTarget(ask);
      if (!target) return null;
      if (target.type === "path") {
        const control = this.button(target.label, async () => {
          await this.copyText(target.path);
          control.textContent = "Copied";
        }, "quiet ask-link");
        control.title = target.path;
        return control;
      }
      const link = this.node("a", target.label, "ask-link");
      link.href = target.href;
      link.title = ask.link;
      if (target.type === "app" && this.navigate) {
        link.onclick = event => { event?.preventDefault?.(); this.navigate(target.href); };
      } else {
        link.target = "_blank";
        link.rel = "noopener noreferrer";
      }
      return link;
    }
    buildRow(ask, snoozedSection) {
      const element = this.node("article", "", "ask-row");
      element.dataset.id = String(ask.id);
      element.dataset.kind = ask.kind;
      const head = this.node("div", "", "ask-head");
      const heading = this.heading(ask);
      head.append(heading.title);
      const link = this.linkControl(ask);
      if (link) head.append(link);
      const meta = this.node("p", "", "ask-meta");
      const age = this.node("span", "", "ask-age");
      const blocking = this.node("span", copy.groups.blocking, "ask-flag ask-flag-urgent");
      const stale = this.node("span", "", "ask-flag ask-flag-stale");
      const due = this.node("span", "", "ask-flag");
      const snoozed = this.node("span", "", "ask-flag");
      meta.append(this.node("span", copy.kinds[ask.kind] || ask.kind, "ask-kind"));
      const effort = formatEffort(ask.effort_minutes);
      if (effort) meta.append(this.node("span", "about " + effort, "ask-effort"));
      meta.append(age, this.threadControl(ask), blocking, stale, due, snoozed);
      element.append(head, meta, ...heading.detail);
      if (ask.unblocks) {
        const unblocks = this.node("p", "", "ask-unblocks");
        unblocks.append(this.node("span", "Unblocks", "ask-label"), this.node("span", ask.unblocks));
        element.append(unblocks);
      }
      if (ask.context) element.append(...this.passage(ask.context, "ask-context"));

      const decision = ask.kind === "decision";
      const form = this.node("form", "", "ask-resolve");
      const input = this.node("input", "", "ask-note"); input.type = "text"; input.maxLength = 4000;
      input.placeholder = !decision ? "Note for the agent (optional)"
        : ask.options.length ? "Or answer in your own words…" : "Your answer…";
      input.setAttribute("aria-label", (decision ? "Answer: " : "Note: ") + ask.question);
      const error = this.node("p", "", "ask-row-error"); error.setAttribute("role", "alert"); error.hidden = true;
      const controls = [input];
      const record = {element, controls, error, input};
      const note = () => { const text = input.value.trim(); return text ? {note: text} : {}; };
      const resolveWith = resolution => () => this.resolve(ask, {resolution, ...note()}, record);
      const actions = this.node("div", "", "ask-actions");
      if (decision) {
        if (ask.options.length) {
          const options = this.node("div", "", "ask-options");
          for (const option of ask.options) {
            const choice = this.button(option, () => this.resolve(ask, {resolution: "answered", option, ...note()}, record), "ask-option");
            controls.push(choice); options.append(choice);
          }
          form.append(options);
        }
        const send = this.node("button", copy.resolve.answered, "ask-affirm"); send.type = "submit";
        controls.push(send); actions.append(send);
        form.onsubmit = event => {
          event?.preventDefault?.();
          const text = input.value.trim();
          if (!text) return;
          return this.resolve(ask, {resolution: "answered", answer: text}, record);
        };
      } else {
        const [affirm, ...others] = ask.resolutions.filter(resolution => resolution !== "dismissed");
        const primary = this.button(copy.resolve[affirm] || affirm, resolveWith(affirm), "ask-affirm");
        controls.push(primary); actions.append(primary);
        for (const resolution of others) {
          const other = this.button(copy.resolve[resolution] || resolution, resolveWith(resolution), "quiet");
          controls.push(other); actions.append(other);
        }
        // Enter in the note must not pick a resolution for the user.
        form.onsubmit = event => event?.preventDefault?.();
      }
      const entry = this.node("div", "", "ask-entry");
      form.append(entry);

      const secondary = this.node("div", "", "ask-secondary");
      if (snoozedSection) {
        const show = this.button("Show now", () => this.snooze(ask, null, record), "quiet");
        controls.push(show); secondary.append(show);
      } else {
        const select = this.node("select", "", "ask-snooze");
        select.setAttribute("aria-label", "Snooze: " + ask.question);
        const placeholder = this.node("option", "Snooze…"); placeholder.value = "";
        select.append(placeholder, ...SNOOZE_CHOICES.map(([value, label]) => {
          const option = this.node("option", label); option.value = value; return option;
        }));
        select.onchange = () => {
          const until = snoozeUntil(select.value, this.clock());
          select.value = "";
          if (until != null) return this.snooze(ask, until, record);
        };
        controls.push(select); secondary.append(select);
      }
      const dismiss = this.button(copy.resolve.dismissed, resolveWith("dismissed"), "quiet");
      dismiss.title = "Close this ask without acting on it. The agent is told.";
      controls.push(dismiss); secondary.append(dismiss);
      entry.append(input, actions, secondary);
      element.append(form, error);

      record.update = current => {
        const now = this.clock();
        age.textContent = "asked " + (formatAge(current.age_seconds) === "just now" ? "just now" : formatAge(current.age_seconds) + " ago");
        blocking.hidden = !(snoozedSection && current.blocking);
        stale.hidden = !current.stale;
        stale.textContent = !current.stale ? "" : current.stale_reason === "thread_closed"
          ? "Stale · thread closed" : "Stale · thread idle " + formatAge(current.idle_seconds);
        due.hidden = !current.due_at;
        due.textContent = current.due_at ? (current.overdue ? "Overdue · due " : "Due ") + formatDay(current.due_at, now) : "";
        due.className = "ask-flag" + (current.overdue ? " ask-flag-urgent" : "");
        snoozed.hidden = !current.snoozed;
        snoozed.textContent = current.snoozed ? "Snoozed until " + formatMoment(current.snoozed_until, now) : "";
      };
      return record;
    }
    buildResolvedRow(ask) {
      const element = this.node("article", "", "ask-row ask-row-resolved");
      element.dataset.id = String(ask.id);
      element.dataset.kind = ask.kind;
      const head = this.node("div", "", "ask-head");
      const heading = this.heading(ask);
      head.append(heading.title,
        this.node("span", copy.resolved[ask.resolution] || ask.resolution, "ask-resolution ask-resolution-" + ask.resolution));
      const meta = this.node("p", "", "ask-meta");
      meta.append(this.node("span", copy.kinds[ask.kind] || ask.kind, "ask-kind"),
        this.node("span", formatMoment(ask.resolved_at, this.clock()), "ask-age"), this.threadControl(ask));
      if (ask.project_label) meta.append(this.node("span", ask.project_label, "ask-project"));
      element.append(head, meta, ...heading.detail);
      for (const [label, text] of [["Answer", ask.answer], ["Note", ask.note]]) {
        if (!text) continue;
        const outcome = this.node("p", "", "ask-unblocks");
        outcome.append(this.node("span", label, "ask-label"), this.node("span", text));
        element.append(outcome);
      }
      return element;
    }
    async submit(ask, record, path, body) {
      if (this.pending.has(ask.id)) return null;
      this.pending.add(ask.id);
      for (const control of record.controls) control.disabled = true;
      record.error.hidden = true;
      try {
        return await this.api(path, body);
      } catch (error) {
        // Keep the row and whatever the user typed; say what went wrong beside it.
        for (const control of record.controls) control.disabled = false;
        record.error.textContent = (error?.message || String(error)) + " Your input is kept.";
        record.error.hidden = false;
        return null;
      } finally { this.pending.delete(ask.id); }
    }
    async resolve(ask, body, record) {
      const saved = await this.submit(ask, record, "asks/" + encodeURIComponent(ask.id), body);
      if (!saved) return;
      this.open = this.open.filter(item => item.id !== ask.id);
      if (this.resolvedLoaded) this.resolved = [{...ask, ...saved, stale: false, overdue: false, snoozed: false}, ...this.resolved];
      this.render();
    }
    async snooze(ask, until, record) {
      const saved = await this.submit(ask, record, "asks/" + encodeURIComponent(ask.id) + "/snooze", {until});
      if (!saved) return;
      this.open = this.open.map(item => item.id === ask.id
        ? {...item, snoozed_until: saved.snoozed_until, snoozed: saved.snoozed_until != null} : item);
      this.render();
    }
    start(view) {
      this.refresh();
      const tick = () => { if (!this.document.hidden) this.refresh(); };
      this.timer = view.setInterval(tick, REFRESH_MILLISECONDS);
      this.document.addEventListener("visibilitychange", tick);
      view.addEventListener("pagehide", () => view.clearInterval(this.timer), {once: true});
    }
  }

  const exported = {AsksView, formatAge, formatEffort, snoozeUntil, openGroups, linkTarget, splitQuestion, isLong};
  if (typeof module !== "undefined") { module.exports = exported; return; }
  globalThis.AsksView = AsksView;
  const doc = globalThis.document;
  if (!doc?.getElementById("asks-list")) return;
  const embedded = globalThis.parent !== globalThis && new URLSearchParams(globalThis.location.search).get("embedded") === "1";
  const post = message => globalThis.parent.postMessage(message, globalThis.location.origin);
  let token = null;
  async function api(path, body) {
    const options = {cache: "no-store"};
    if (body !== undefined) {
      if (!token) token = (await api("config")).token;
      Object.assign(options, {method: "POST", body: JSON.stringify(body),
        headers: {"Content-Type": "application/json", "X-Agent-Coord-Token": token}});
    }
    let response;
    try { response = await fetch("/api/browser/" + path, options); } catch { throw Error("Could not reach Ribbon Field. Check the connection."); }
    let result;
    try { result = await response.json(); } catch { throw Error("Could not read the response. Check the connection and try again."); }
    if (!response.ok) throw Error(result.error || "The request failed. Check the connection and try again.");
    return result;
  }
  const view = new AsksView({
    document: doc, api,
    openThread: id => embedded ? post({type: "ribbon-field-asks-thread", thread_id: id})
      : globalThis.location.assign("/#" + encodeURIComponent(id)),
    navigate: embedded ? url => post({type: "ribbon-field-asks-navigate", url}) : null,
    close: embedded ? () => post({type: "ribbon-field-close-asks"}) : null,
    copyText: text => globalThis.navigator.clipboard.writeText(text),
  });
  view.start(globalThis);
})();
