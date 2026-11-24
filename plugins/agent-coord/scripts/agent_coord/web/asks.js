"use strict";
/* Asks in the workspace: the sidebar entry and count, the view where the user resolves them, and the
   strip that shows a conversation's own asks. Everything loads from this one established URL, so an
   older backend can serve it before its update. */
(() => {
  // Display copy. The view's name and wording are defaults the owner may change: change them here only.
  const copy = {
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
    legacyNotice: "Showing decisions only. Kinds, notes, snooze, dismissal and the resolved log appear once the backend update is applied.",
    // A conversation's own asks: the open ones above its composer, its resolutions in the side panel.
    thread: {
      label: "Asks from this conversation",
      waiting: count => count + (count === 1 ? " ask" : " asks") + " waiting for you",
      snoozed: count => count + (count === 1 ? " snoozed ask" : " snoozed asks"),
      blocking: count => count + " blocking",
      history: "Resolved asks",
      earlier(count) { return count + " earlier in the " + copy.viewName + "'s " + copy.sections.resolved + " section."; },
    },
    reply: {
      placeholder: "Answer in your own words…",
      label: "Answer",
      answer: "Answer",
      answerTitle: "Record this as your answer and close the ask.",
      hint: "Enter answers the ask. Shift+Enter discusses it and keeps it open.",
      discuss: "Discuss in thread",
      discussTitle: "Send this as a message in the thread and keep the ask open.",
      mark: age => "You replied " + (age === "just now" ? age : age + " ago"),
      external: "This thread runs outside Ribbon Field. Use Answer or choose a resolution to send your text.",
      /* How a reply names its ask to the agent: number, title and that it is still open. */
      reference(ask) {
        const title = splitQuestion(ask.question).title;
        const short = title.length > REPLY_TITLE_CHARACTERS ? title.slice(0, REPLY_TITLE_CHARACTERS - 1).trimEnd() + "…" : title;
        return "Re ask #" + ask.id + " \"" + short + "\" (still open): ";
      },
    },
    entryTitle(count, blocking) {
      if (!count) return this.viewName;
      return count + (count === 1 ? " ask" : " asks") + " waiting for you" + (blocking ? " · " + blocking + " blocking" : "");
    },
  };
  const SECTIONS = ["open", "snoozed", "resolved"];
  const SNOOZE_CHOICES = [["hour", "In 1 hour"], ["three_hours", "In 3 hours"], ["tomorrow", "Tomorrow at 9 AM"], ["week", "Next week"]];
  const REFRESH_MILLISECONDS = 5000;
  // The resolved log changes rarely, so a conversation rereads it at most this often.
  const RESOLVED_REREAD_MILLISECONDS = 60000;
  const LONG_TEXT_CHARACTERS = 240, LONG_TEXT_LINES = 3;
  // A reply quotes this much of its ask's title.
  const REPLY_TITLE_CHARACTERS = 120;
  // Where this browser keeps the user's last reply to each open ask, for a backend that does not record it.
  const REPLIES_STORAGE_KEY = "agent-coord.ask-replies";
  // A conversation's side panel lists this many of its resolutions; the rest are in the view.
  const THREAD_HISTORY_ROWS = 10;

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
      const label = ask.project_label || ask.project_name || "";
      if (!projects.has(label)) projects.set(label, []);
      projects.get(label).push(ask);
    }
    const groups = [];
    if (blocking.length) groups.push({key: "blocking", title: copy.groups.blocking, urgent: true, items: blocking});
    if (overdue.length) groups.push({key: "overdue", title: copy.groups.overdue, urgent: false, items: overdue});
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

  /* Rows for one ask, open or resolved, and the controls that resolve it. The view and a
     conversation's strip build the same rows. A subclass supplies resolve, snooze and legacy. */
  /* Add text to a message draft without losing what is there, and leave the cursor after it. */
  function addToDraft(input, text, view = globalThis) {
    const draft = input.value;
    if (!draft.includes(text.trim())) {
      const separator = !draft || draft.endsWith("\n\n") ? "" : draft.endsWith("\n") ? "\n" : "\n\n";
      input.value = draft + separator + text;
      if (view.Event) input.dispatchEvent?.(new view.Event("input", {bubbles: true}));
    }
    input.focus?.({preventScroll: true});
    input.setSelectionRange?.(input.value.length, input.value.length);
  }

  class AskRows {
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
      return {title: this.node("h4", title, "ask-title"), detail: detail ? this.passage(detail, "ask-detail") : []};
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
    /* An open ask. `snoozed` gives the row the control that shows it again; `grouped` means a
       heading above already says it is blocking; `thread` names the asking thread, and without it
       the row is in that thread's own conversation; `fold` makes the title a control that opens
       and closes the rest of the row. */
    buildRow(ask, {snoozed: snoozedSection = false, grouped = false, thread = true, fold = null} = {}) {
      const element = this.node("article", "", "ask-row");
      element.dataset.id = String(ask.id);
      element.dataset.kind = ask.kind || "decision";
      const head = this.node("div", "", "ask-head");
      const heading = this.heading(ask);
      head.append(heading.title);
      let disclosure = null, caret = null;
      if (fold) {
        const text = heading.title.textContent;
        heading.title.textContent = "";
        disclosure = this.button(text, () => fold(ask), "ask-title-toggle");
        heading.title.append(disclosure);
        caret = this.node("span", "", "thread-asks-caret"); caret.setAttribute("aria-hidden", "true");
        head.append(caret);
      }
      const meta = this.node("p", "", "ask-meta");
      const age = this.node("span", "", "ask-age");
      const blocking = this.node("span", copy.groups.blocking, "ask-flag ask-flag-urgent");
      const stale = this.node("span", "", "ask-flag ask-flag-attention");
      const due = this.node("span", "", "ask-flag");
      const snoozed = this.node("span", "", "ask-flag");
      meta.append(this.node("span", copy.kinds[ask.kind || "decision"] || ask.kind, "ask-kind"));
      const effort = formatEffort(ask.effort_minutes);
      if (effort) meta.append(this.node("span", "takes about " + effort, "ask-effort"));
      meta.append(age, ...(thread ? [this.threadControl(ask)] : []), blocking, stale, due, snoozed);
      // The link is where the user starts, so it sits with the title's meta, not at the far edge.
      const link = this.linkControl(ask);
      if (link) meta.append(link);
      element.append(head, meta, ...heading.detail);
      if (ask.unblocks) {
        const unblocks = this.node("p", "", "ask-unblocks");
        unblocks.append(this.node("span", "Unblocks", "ask-label"), this.node("span", ask.unblocks));
        element.append(unblocks);
      }
      if (ask.context) element.append(...this.passage(ask.context, "ask-context"));

      const legacy = this.legacy;
      const resolutions = ask.resolutions || ["answered", "dismissed"];
      const decision = !ask.kind || ask.kind === "decision";
      const form = this.node("form", "", "ask-resolve");
      const input = this.node("input", "", "ask-note"); input.type = "text"; input.maxLength = 4000;
      input.placeholder = copy.reply.placeholder;
      input.setAttribute("aria-label", copy.reply.label + ": " + ask.question);
      const answer = this.node("button", copy.reply.answer, "ask-send"); answer.type = "submit";
      answer.title = copy.reply.answerTitle;
      answer.setAttribute("aria-keyshortcuts", "Enter");
      const discuss = this.button(copy.reply.discuss, () => discussText(), "quiet ask-reply-discuss");
      discuss.title = copy.reply.discussTitle;
      discuss.setAttribute("aria-keyshortcuts", "Shift+Enter");
      const reply = this.node("div", "", "ask-reply");
      reply.append(input, answer, discuss);
      const hint = this.node("p", copy.reply.hint, "ask-hint"); hint.hidden = true;
      // What the user last replied, while the ask stays open.
      const replied = this.node("p", "", "ask-unblocks ask-replied");
      const repliedWhen = this.node("span", "", "ask-label"), repliedText = this.node("span", "");
      replied.append(repliedWhen, repliedText);
      const error = this.node("p", "", "ask-row-error"); error.setAttribute("role", "alert"); error.hidden = true;
      const controls = [input, answer, discuss];
      const record = {element, controls, error, input};
      // A folded row keeps its title and meta line; what was typed in it stays.
      record.fold = folded => {
        if (!disclosure) return;
        element.className = "ask-row" + (folded ? " ask-row-folded" : "");
        disclosure.setAttribute("aria-expanded", String(!folded));
        caret.textContent = folded ? "▸" : "▾";
      };
      const text = () => input.value.trim();
      const note = () => text() ? {note: text()} : {};
      record.sync = () => { hint.hidden = !text(); };
      input.oninput = record.sync;
      const resolveWith = resolution => () => this.resolve(ask, {resolution, ...note()}, record);
      const discussText = () => text() ? this.reply(ask, text(), record) : this.converse(ask, "", record);
      const answerText = () => {
        if (!text()) { input.focus?.(); return; }
        return this.resolve(ask, legacy ? {answer: text()} : {resolution: "answered", answer: text()}, record);
      };
      input.onkeydown = event => {
        if (event.key !== "Enter") return;
        // Enter confirms the current IME candidate before it can act on an ask.
        if (event.isComposing || event.keyCode === 229) return;
        event.preventDefault();
        if (event.altKey || event.ctrlKey || event.metaKey || event.repeat) return;
        return event.shiftKey ? discussText() : answerText();
      };
      form.onsubmit = event => {
        event?.preventDefault?.();
        return answerText();
      };
      const entry = this.node("div", "", "ask-entry");
      const later = this.node("div", "", "ask-later");
      if (decision) {
        for (const option of ask.options || []) {
          const choice = this.button(option, () => this.resolve(ask,
            legacy ? {option} : {resolution: "answered", option, ...note()}, record), "ask-option");
          controls.push(choice); entry.append(choice);
        }
      } else {
        for (const resolution of resolutions.filter(value => value !== "answered" && value !== "dismissed")) {
          const other = this.button(copy.resolve[resolution] || resolution, resolveWith(resolution), "quiet");
          controls.push(other); entry.append(other);
        }
      }
      form.append(reply, hint);
      if (legacy) {
        // Snooze and dismissal need the updated backend.
      } else if (snoozedSection) {
        const show = this.button("Show now", () => this.snooze(ask, null, record), "ask-text-control");
        controls.push(show); later.append(show);
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
        controls.push(select); later.append(select);
      }
      if (!legacy) {
        const dismiss = this.button(copy.resolve.dismissed, resolveWith("dismissed"), "ask-text-control");
        dismiss.title = "Close this ask without acting on it. The agent is told.";
        controls.push(dismiss); later.append(dismiss);
      }
      entry.append(later);
      form.append(entry);
      element.append(replied, form, error);

      record.update = current => {
        const now = this.clock();
        age.textContent = "asked " + (formatAge(current.age_seconds) === "just now" ? "just now" : formatAge(current.age_seconds) + " ago");
        blocking.hidden = grouped || !current.blocking;
        stale.hidden = !current.stale;
        stale.textContent = !current.stale ? "" : current.stale_reason === "thread_closed"
          ? "Stale · thread closed" : "Stale · thread idle " + formatAge(current.idle_seconds);
        due.hidden = !current.due_at;
        age.hidden = !Number.isFinite(current.age_seconds);
        due.textContent = current.due_at ? (current.overdue ? "Overdue · due " : "Due ") + formatDay(current.due_at, now) : "";
        due.className = "ask-flag" + (current.overdue ? " ask-flag-attention" : "");
        snoozed.hidden = !current.snoozed;
        snoozed.textContent = current.snoozed ? "Snoozed until " + formatMoment(current.snoozed_until, now) : "";
        const mark = this.replyOf(current);
        replied.hidden = !mark;
        repliedWhen.textContent = mark ? copy.reply.mark(formatAge((now - new Date(mark.at).getTime()) / 1000)) : "";
        repliedText.textContent = mark ? mark.text : "";
        if (mark) replied.title = formatMoment(mark.at, now);
      };
      return record;
    }
    buildResolvedRow(ask, {thread = true} = {}) {
      const element = this.node("article", "", "ask-row ask-row-resolved");
      element.dataset.id = String(ask.id);
      element.dataset.kind = ask.kind;
      const head = this.node("div", "", "ask-head");
      const heading = this.heading(ask);
      head.append(heading.title,
        this.node("span", copy.resolved[ask.resolution] || ask.resolution, "ask-resolution"));
      const meta = this.node("p", "", "ask-meta");
      meta.append(this.node("span", copy.kinds[ask.kind] || ask.kind, "ask-kind"),
        this.node("span", "resolved " + formatMoment(ask.resolved_at, this.clock()), "ask-age"));
      if (thread) meta.append(this.threadControl(ask));
      if (thread && ask.project_label) meta.append(this.node("span", ask.project_label, "ask-project"));
      element.append(head, meta, ...heading.detail);
      for (const [label, text] of [["Answer", ask.answer], ["Note", ask.note]]) {
        if (!text) continue;
        const outcome = this.node("p", "", "ask-unblocks");
        outcome.append(this.node("span", label, "ask-label"), this.node("span", text));
        element.append(outcome);
      }
      return element;
    }
    /* Run one change to an ask from its row: one at a time, and a failure keeps the row and what was typed. */
    async attempt(ask, record, work) {
      if (this.pending.has(ask.id)) return null;
      this.pending.add(ask.id);
      for (const control of record.controls) control.disabled = true;
      record.error.hidden = true;
      try {
        return await work();
      } catch (error) {
        for (const control of record.controls) control.disabled = false;
        record.error.textContent = (error?.message || String(error)).replace(/[.\s]*$/, ".") + " Your input is kept.";
        record.error.hidden = false;
        return null;
      } finally { this.pending.delete(ask.id); }
    }
    submit(ask, record, path, body) { return this.attempt(ask, record, () => this.api(path, body)); }
  }

  class AsksView extends AskRows {
    constructor({document, api, openThread, navigate, close, copyText, clock = () => Date.now(),
                 compose, threadInfo, messageSent, storage, requestId = () => globalThis.crypto.randomUUID()}) {
      super();
      Object.assign(this, {document, api, openThread, navigate, close, copyText, clock,
        compose, threadInfo, messageSent, storage, requestId});
      // A backend that records replies says so in its configuration; until then this browser keeps the mark.
      this.replyRecords = false;
      this.replies = this.loadReplies();
      // The dialog is closed until the user opens it; data still loads for the count.
      this.visible = false;
      // An older backend serves this view until its update is applied; it knows only decisions.
      this.legacy = false;
      const $ = id => document.getElementById(id);
      this.elements = {title: $("asks-title"), summary: $("asks-summary"), close: $("close-asks"),
        sections: $("asks-sections"), filters: $("asks-filters"), project: $("asks-filter-project"),
        kind: $("asks-filter-kind"), since: $("asks-filter-since"), notice: $("asks-notice"),
        error: $("asks-error"), status: $("asks-status"), list: $("asks-list")};
      this.section = "open";
      this.open = [];
      this.resolved = [];
      this.loaded = false;
      this.resolvedLoaded = false;
      this.threadResolvedAt = new Map();
      this.failure = null;
      this.sequence = 0;
      this.records = new Map();
      this.pending = new Set();
      this.signature = null;
      this.tabs = new Map();
      this.setup();
    }
    setup() {
      const {elements} = this;
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
      elements.close.onclick = () => this.close();
      elements.notice.textContent = copy.legacyNotice;
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
    /* Show or hide the view; a hidden view keeps its data current without building rows. */
    setVisible(visible) {
      this.visible = visible;
      if (visible) this.render();
    }
    setLegacy(legacy) {
      if (this.legacy === legacy) return;
      this.legacy = legacy;
      if (legacy) this.section = "open";
      this.records.clear();
      this.signature = null;
      this.render();
    }
    async refresh({background = false} = {}) {
      const sequence = ++this.sequence;
      try {
        const options = {priority: background ? "low" : "auto"};
        const requests = [this.api("asks", undefined, options)];
        const log = !this.legacy && (this.resolvedDue || this.visible && this.section === "resolved");
        if (log) requests.push(this.api("asks?status=resolved", undefined, options));
        const [open, resolved] = await Promise.all(requests);
        if (sequence !== this.sequence) return;
        this.open = open.data || [];
        this.forgetReplies();
        if (resolved) {
          this.resolved = resolved.data || [];
          this.resolvedLoaded = true; this.resolvedDue = false; this.resolvedReadAt = this.clock();
        }
        this.loaded = true;
        this.failure = null;
      } catch (error) {
        if (sequence !== this.sequence) return;
        this.failure = error?.message || String(error);
      }
      this.render();
    }
    /* Read the resolved log with the open list. A conversation asks for it; the view reads it only in its own section. */
    readResolved() {
      if (this.legacy) return Promise.resolve();
      this.resolvedDue = true;
      return this.refresh();
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
      if (!this.visible) return;
      const {elements} = this, counts = this.counts();
      elements.sections.hidden = this.legacy;
      elements.notice.hidden = !this.legacy;
      for (const [section, {tab, count}] of this.tabs) {
        const selected = section === this.section;
        tab.setAttribute("aria-selected", String(selected));
        tab.className = "asks-section" + (selected ? " asks-section-selected" : "");
        const value = section === "resolved" ? "" : String(counts[section] || "");
        count.textContent = value;
        count.hidden = !value;
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
        if (!this.records.has(key)) this.records.set(key, this.buildRow(ask, {snoozed: snoozedSection, grouped: !snoozedSection}));
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
          const heading = this.node("h3", group.title, "ask-group-title");
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
    async resolve(ask, body, record) {
      const saved = await this.submit(ask, record, "asks/" + encodeURIComponent(ask.id), body);
      if (!saved) return;
      ++this.sequence; // An open-list request started before the write may still carry this ask.
      this.open = this.open.filter(item => item.id !== ask.id);
      this.resolved = [{...ask, ...saved, stale: false, overdue: false, snoozed: false},
        ...this.resolved.filter(item => item.id !== ask.id)];
      this.render();
    }
    async snooze(ask, until, record) {
      const saved = await this.submit(ask, record, "asks/" + encodeURIComponent(ask.id) + "/snooze", {until});
      if (!saved) return;
      ++this.sequence;
      this.open = this.open.map(item => item.id === ask.id
        ? {...item, snoozed_until: saved.snoozed_until, snoozed: saved.snoozed_until != null} : item);
      this.render();
    }
    /* Send the user's own words to the asking conversation as their message. The ask stays open and shows the reply. */
    async reply(ask, text, record) {
      const sent = await this.attempt(ask, record, async () => {
        if (this.threadInfo?.(ask.session_id)?.browser_session === false) throw new Error(copy.reply.external);
        // The same text sent again after an unconfirmed attempt must not arrive twice.
        if (record.replyAttempt?.text !== text) record.replyAttempt = {text, id: this.requestId()};
        await this.api("sessions/" + encodeURIComponent(ask.session_id) + "/messages",
          {message: copy.reply.reference(ask) + text, requestId: record.replyAttempt.id});
        return true;
      });
      if (!sent) return;
      record.replyAttempt = null;
      for (const control of record.controls) control.disabled = false;
      record.input.value = "";
      record.sync();
      this.replies.set(ask.id, {text, at: new Date(this.clock()).toISOString()});
      this.saveReplies();
      this.render();
      this.messageSent?.(ask.session_id);
      if (!this.replyRecords) return;
      try {
        const saved = await this.api("asks/" + encodeURIComponent(ask.id) + "/reply", {text});
        this.open = this.open.map(item => item.id === ask.id
          ? {...item, reply_text: saved.reply_text, replied_at: saved.replied_at} : item);
        this.render();
      } catch { /* The reply reached the agent. This browser's own mark stands in for the record. */ }
    }
    /* Continue in the asking conversation, with the ask named in the message being written. */
    async converse(ask, text, record) {
      const reference = copy.reply.reference(ask) + text;
      if (!this.compose) return this.openThread(ask.session_id);
      // The typed text moves to the conversation's draft only when that draft took it.
      if (await this.compose(ask.session_id, reference)) { record.input.value = ""; record.sync(); }
    }
    /* The user's last reply to an open ask: the backend's record where it keeps one, else this browser's. */
    replyOf(ask) {
      if (ask.replied_at) return {text: ask.reply_text || "", at: ask.replied_at};
      return this.replies.get(ask.id) || null;
    }
    loadReplies() {
      try {
        const saved = JSON.parse(this.storage?.getItem(REPLIES_STORAGE_KEY) || "{}");
        return new Map(Object.entries(saved).map(([id, mark]) => [Number(id), mark])
          .filter(([id, mark]) => Number.isInteger(id) && typeof mark?.text === "string" && typeof mark?.at === "string"));
      } catch { return new Map(); }
    }
    saveReplies() {
      try { this.storage?.setItem(REPLIES_STORAGE_KEY, JSON.stringify(Object.fromEntries(this.replies))); }
      catch { /* The mark lasts for this page when storage is unavailable. */ }
    }
    /* A reply is kept only while its ask is open. */
    forgetReplies() {
      const open = new Set(this.open.map(ask => ask.id));
      const closed = [...this.replies.keys()].filter(id => !open.has(id));
      if (!closed.length) return;
      for (const id of closed) this.replies.delete(id);
      this.saveReplies();
    }
  }

  /* A conversation's own asks: the open ones above its composer with the view's controls, and its
     resolutions in the side panel. It shows the view's data and resolves through the view. */
  class ThreadAsksView extends AskRows {
    constructor({document, source, element, history, onError}) {
      super();
      Object.assign(this, {document, source, element, history, onError});
      this.thread = null;
      this.records = new Map();
      this.signature = null;
      this.historySignature = null;
      // This conversation's open asks at the last render, to notice one resolved elsewhere.
      this.shown = new Set();
      // What the user folded or unfolded, by thread, until the page reloads.
      this.choices = new Map();
      // The ask shown in full in the open conversation.
      this.full = null;
      if (element) this.setup();
    }
    get legacy() { return this.source.legacy; }
    get clock() { return this.source.clock; }
    get copyText() { return this.source.copyText; }
    get navigate() { return this.source.navigate; }
    fail(error) { this.onError?.(error); }
    resolve(ask, body, record) { return this.source.resolve(ask, body, record); }
    snooze(ask, until, record) { return this.source.snooze(ask, until, record); }
    reply(ask, text, record) { return this.source.reply(ask, text, record); }
    converse(ask, text, record) { return this.source.converse(ask, text, record); }
    replyOf(ask) { return this.source.replyOf(ask); }
    setup() {
      const {element} = this;
      element.setAttribute("aria-label", copy.thread.label);
      this.list = this.node("div", "", "thread-asks-list");
      this.list.id = "thread-asks-open";
      this.toggle = this.button("", () => this.fold(), "thread-asks-toggle thread-asks-open");
      this.toggle.setAttribute("aria-controls", this.list.id);
      this.caret = this.node("span", "", "thread-asks-caret"); this.caret.setAttribute("aria-hidden", "true");
      this.summary = this.node("span", "");
      this.blocking = this.node("span", "", "ask-flag-urgent");
      // Folded, the line names the first ask, so the user knows what waits without unfolding.
      this.first = this.node("span", "", "thread-asks-first");
      this.toggle.append(this.caret, this.summary, this.blocking, this.first);
      element.replaceChildren(this.toggle, this.list);
      this.render();
    }
    /* Show the asks of one conversation, or of none. */
    setThread(thread) {
      thread = thread || null;
      if (thread === this.thread) return Promise.resolve();
      this.thread = thread;
      this.full = null;
      this.records.clear();
      this.shown = new Set();
      this.signature = null;
      this.render();
      // Nothing else keeps the resolved log current, and it changes rarely.
      const {source} = this;
      const lastRead = Math.max(source.resolvedReadAt ?? -Infinity, source.threadResolvedAt.get(thread) ?? -Infinity);
      const due = source.clock() - lastRead >= RESOLVED_REREAD_MILLISECONDS;
      return thread && this.history && due ? source.readResolved() : Promise.resolve();
    }
    fold() {
      this.choices.set(this.thread, {...this.choices.get(this.thread), open: !this.expanded()});
      this.render();
      if (this.expanded()) this.element.scrollTop = 0;
    }
    /* One open ask is shown in full: the one the user opened, else the one already in full, else
       the first in order. The rest show their title and meta line. */
    unfolded(asks) {
      const choice = this.choices.get(this.thread);
      const listed = id => asks.some(ask => ask.id === id);
      if (choice && "ask" in choice && (choice.ask == null || listed(choice.ask))) return choice.ask;
      return listed(this.full) ? this.full : asks[0]?.id ?? null;
    }
    unfold(ask) {
      const choice = this.choices.get(this.thread) || {};
      choice.ask = this.unfolded(this.openAsks()) === ask.id ? null : ask.id;
      this.choices.set(this.thread, choice);
      this.render();
    }
    /* The strip starts unfolded while an ask waits, and folded when every ask is snoozed. */
    expanded() {
      const choice = this.choices.get(this.thread)?.open;
      return choice != null ? choice : this.openAsks().some(ask => !ask.snoozed);
    }
    /* Blocking first, then overdue, then oldest first; snoozed asks last. */
    openAsks() {
      if (!this.thread) return [];
      const rank = ask => ask.snoozed ? 3 : ask.blocking ? 0 : ask.overdue ? 1 : 2;
      return this.source.open.filter(ask => ask.session_id === this.thread)
        .sort((a, b) => rank(a) - rank(b) || b.age_seconds - a.age_seconds || a.id - b.id);
    }
    /* This conversation's resolutions, newest first, as the resolved log lists them. */
    resolvedAsks() {
      if (!this.thread || this.legacy) return [];
      return this.source.resolved.filter(ask => ask.session_id === this.thread);
    }
    render() {
      this.renderHistory();
      const {element, source} = this;
      if (!element) return;
      const asks = this.openAsks();
      // An ask that left the open list and is not in the log was resolved elsewhere: reread the log.
      const ids = new Set(asks.map(ask => ask.id)), logged = new Set(source.resolved.map(ask => ask.id));
      const departed = this.history && [...this.shown].some(id => !ids.has(id) && !logged.has(id));
      this.shown = ids;
      if (departed) source.readResolved().catch(error => this.fail(error));
      // With nothing open, nothing about asks sits above the composer.
      element.hidden = !asks.length;
      if (element.hidden) {
        this.records.clear();
        this.signature = null;
        this.list.replaceChildren();
        return;
      }
      const waiting = asks.filter(ask => !ask.snoozed), blocking = waiting.filter(ask => ask.blocking).length;
      const expanded = this.expanded();
      this.list.hidden = !expanded;
      this.summary.textContent = waiting.length ? copy.thread.waiting(waiting.length) : copy.thread.snoozed(asks.length);
      // Unfolded, the rows carry the Blocking flag and their titles; folded, the line has to.
      this.blocking.textContent = blocking && !expanded ? copy.thread.blocking(blocking) : "";
      this.blocking.hidden = !this.blocking.textContent;
      this.first.textContent = waiting.length && !expanded ? splitQuestion(waiting[0].question).title : "";
      this.first.hidden = !this.first.textContent;
      this.toggle.setAttribute("aria-expanded", String(expanded));
      this.caret.textContent = expanded ? "▾" : "▸";
      // The ask in full stays in place while the conversation is open, even when a more urgent one arrives mid-answer.
      const full = this.full = this.unfolded(asks);
      const live = new Set();
      for (const ask of asks) {
        const key = ask.id + ":" + Boolean(ask.snoozed);
        live.add(key);
        if (!this.records.has(key)) {
          this.records.set(key, this.buildRow(ask, {snoozed: Boolean(ask.snoozed), thread: false, fold: item => this.unfold(item)}));
        }
        const record = this.records.get(key);
        record.update(ask);
        record.fold(ask.id !== full);
      }
      for (const key of [...this.records.keys()]) if (!live.has(key)) this.records.delete(key);
      // Reuse rows across refreshes so a typed reply, focus and selection survive.
      const signature = [this.thread, this.legacy, [...live].join(",")].join("|");
      if (signature === this.signature) return;
      this.signature = signature;
      const active = this.document.activeElement;
      const selectionStart = active?.selectionStart, selectionEnd = active?.selectionEnd;
      this.list.replaceChildren(...[...live].map(key => this.records.get(key).element));
      if (active && this.list.contains?.(active)) {
        active.focus?.();
        if (selectionStart != null) active.setSelectionRange?.(selectionStart, selectionEnd);
      }
    }
    /* The conversation's resolutions, in its side panel with the thread's other context. */
    renderHistory() {
      const {history} = this;
      if (!history) return;
      const log = this.resolvedAsks(), now = this.clock();
      const signature = this.thread + "|" + log.map(ask => ask.id + ":" + ask.resolution + ":" + ask.resolved_at).join(",");
      if (signature === this.historySignature) return;
      this.historySignature = signature;
      history.hidden = !log.length;
      if (!log.length) { history.replaceChildren(); return; }
      const items = log.slice(0, THREAD_HISTORY_ROWS).map(ask => {
        const item = this.node("div", "", "thread-asks-history-item");
        const title = this.node("p", "", "thread-asks-history-title");
        title.append(this.node("strong", splitQuestion(ask.question).title));
        const outcome = this.node("p", [copy.resolved[ask.resolution] || ask.resolution, copy.kinds[ask.kind] || ask.kind,
          formatMoment(ask.resolved_at, now)].filter(Boolean).join(" · "), "thread-asks-history-outcome");
        item.append(title, outcome);
        for (const [label, text] of [["Answer", ask.answer], ["Note", ask.note]]) {
          if (!text) continue;
          const line = this.node("p", "", "thread-asks-history-text");
          line.append(this.node("span", label, "ask-label"), this.node("span", text));
          item.append(line);
        }
        return item;
      });
      history.replaceChildren(this.node("h3", copy.thread.history), ...items);
      if (log.length > THREAD_HISTORY_ROWS) {
        history.append(this.node("p", copy.thread.earlier(log.length - THREAD_HISTORY_ROWS), "thread-asks-history-outcome"));
      }
    }
  }

  /* Workspace entry: the sidebar count, the dialog, the refresh cycle while it is open, the count
     on the navigation toggle, and the open conversation's strip. */
  class AsksUI {
    constructor({document, api, view = globalThis, navigate, onError, copyText, clock, kinds, replyRecords,
                 openComposer, threadInfo, messageSent, storage, requestId, connected}) {
      Object.assign(this, {document, api, view, navigate, onError, openComposer, connected});
      const $ = id => document.getElementById(id);
      this.entry = $("open-asks-view");
      this.label = $("asks-entry-label");
      this.badge = $("asks-count");
      // While the navigation is closed its entry is out of sight, so the toggle carries the count.
      this.toggle = $("menu-toggle");
      this.toggleBadge = $("asks-toggle-count");
      this.dialog = $("asks-dialog");
      if (this.label) this.label.textContent = copy.viewName;
      this.viewer = new AsksView({document, api,
        openThread: id => this.follow("agentcoord://thread/" + encodeURIComponent(id)),
        navigate: url => this.follow(url), close: () => this.dialog.close(),
        copyText: copyText || (text => view.navigator.clipboard.writeText(text)),
        compose: (thread, text) => this.compose(thread, text), threadInfo, messageSent, storage,
        ...(requestId ? {requestId} : {}), ...(clock ? {clock} : {})});
      this.viewer.replyRecords = replyRecords === true;
      // The workspace already knows whether its backend predates kinds; opening the view asks again.
      if (kinds === false) this.viewer.setLegacy(true);
      this.thread = new ThreadAsksView({document, source: this.viewer, element: $("thread-asks"),
        history: $("thread-asks-history"), onError});
      const render = this.viewer.render.bind(this.viewer);
      // Every change to the list, including an in-place resolution, also moves the counts and the strip.
      this.viewer.render = () => { render(); this.updateBadge(); this.thread.render(); };
      if (this.entry) this.entry.onclick = event => { event?.preventDefault?.(); return this.open(); };
      this.dialog.addEventListener?.("close", () => this.closed());
      this.updateBadge();
    }
    get items() { return this.viewer.open; }
    refresh(options) { return this.viewer.refresh(options); }
    hydrateThread(thread, items) {
      if (!Array.isArray(items)) return;
      const {viewer} = this;
      const resolved = new Map(viewer.resolved.map(ask => [ask.id, ask]));
      const open = [];
      for (const ask of items) {
        if (ask.session_id !== thread) continue;
        if (["resolved", "answered"].includes(ask.status)) resolved.set(ask.id, ask);
        else if ((!ask.status || ask.status === "open") && !resolved.has(ask.id)) open.push(ask);
      }
      viewer.open = [...viewer.open.filter(ask => ask.session_id !== thread), ...open];
      viewer.resolved = [...resolved.values()].sort((a, b) => Date.parse(b.resolved_at) - Date.parse(a.resolved_at) || b.id - a.id);
      viewer.threadResolvedAt.set(thread, viewer.clock());
      viewer.render();
    }
    setEventsConnected(connected) {
      this.eventsConnected = connected;
      this.syncTimer();
    }
    syncTimer() {
      if ((this.eventsConnected ?? this.connected?.()) || !this.viewer.visible) {
        this.view.clearInterval?.(this.timer);
        this.timer = null;
      } else if (this.timer == null) {
        this.timer = this.view.setInterval?.(() => {
          if (this.eventsConnected ?? this.connected?.()) this.syncTimer();
          else if (!this.document.hidden) this.refresh({background: true});
        }, REFRESH_MILLISECONDS);
      }
    }
    /* Show one conversation's asks in its strip; no thread hides the strip. */
    showThread(thread) { return this.thread.setThread(thread); }
    updateBadge() {
      // Snoozed asks are out of the open list until their time.
      const {open: count, blocking} = this.viewer.counts();
      for (const badge of [this.badge, this.toggleBadge]) {
        if (!badge) continue;
        badge.hidden = !count;
        badge.textContent = count ? String(count) : "";
        badge.classList?.toggle("asks-count-blocking", blocking > 0);
      }
      const title = copy.entryTitle(count, blocking);
      if (this.entry) this.entry.title = title;
      this.entry?.setAttribute("aria-label", count ? copy.viewName + ": " + title : copy.viewName);
      // The toggle keeps its own name; the count is its description.
      if (count) this.toggle?.setAttribute("aria-description", title);
      else this.toggle?.removeAttribute("aria-description");
    }
    async open() {
      const backdrop = this.document.getElementById("sidebar-backdrop");
      if (backdrop && !backdrop.hidden) backdrop.click();
      this.viewer.setVisible(true);
      if (!this.dialog.open) this.dialog.showModal();
      this.syncTimer();
      try {
        // A backend from before kinds serves this view until its update is applied.
        const config = await this.api("config");
        this.viewer.setLegacy(!config.askKinds);
        this.viewer.replyRecords = config.askReplies === true;
      } catch (error) { this.onError?.(error); }
      await this.refresh();
    }
    follow(url) {
      this.dialog.close();
      Promise.resolve(this.navigate(url)).catch(this.onError);
    }
    /* Open a conversation and put text in its message draft. False when its draft cannot take text. */
    async compose(thread, text) {
      if (this.dialog.open) this.dialog.close();
      const input = await this.openComposer?.(thread);
      if (!input) return false;
      addToDraft(input, text, this.view);
      return true;
    }
    closed() {
      this.viewer.setVisible(false);
      this.syncTimer();
      const visible = this.entry?.getClientRects?.().length;
      (visible ? this.entry : this.document.getElementById("menu-toggle"))?.focus?.({preventScroll: true});
      this.refresh();
    }
  }

  const exported = {AsksUI, AsksView, ThreadAsksView, AsksCopy: copy, addToDraft, formatAge, formatEffort, snoozeUntil, openGroups, linkTarget, splitQuestion, isLong};
  if (typeof module !== "undefined") module.exports = exported;
  globalThis.AsksUI = AsksUI;
})();
