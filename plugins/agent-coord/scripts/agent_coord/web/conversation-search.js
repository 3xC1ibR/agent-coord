/* Shared backend search; the browser owns presentation and stale-response guards. */
(function (root) {
  "use strict";
  function parameters(query, scope, mine, filters) {
    const params = new URLSearchParams({q: query.trim(), scope, my_messages: String(mine)});
    if (scope === "current") for (const key of ["show", "phase", "repository", "project"]) {
      if (filters[key]) params.set(key, filters[key]);
    }
    return params.toString();
  }
  function highlight(document, element, text, query) {
    const terms = [...new Set(query.trim().split(/\s+/).filter(Boolean))];
    const pattern = new RegExp("(" + terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "gi");
    for (const part of String(text).split(pattern)) {
      const matched = terms.some(t => t.toLocaleLowerCase() === part.toLocaleLowerCase());
      const child = matched ? document.createElement("mark") : document.createTextNode(part);
      if (matched) child.textContent = part;
      element.append(child);
    }
  }
  class Search {
    constructor({document, api, filters}) {
      Object.assign(this, {document, api, filters});
      this.$ = id => document.getElementById(id);
      this.serial = 0; this.signature = ""; this.items = [];
      this.$("search-scope").onchange = this.$("search-mine").onchange = () => this.update();
      this.$("search-more").onclick = () => this.fetch(this.signature, this.serial, this.cursor);
      this.$("search-retry").onclick = () => { this.signature = ""; this.update(); };
    }
    node(tag, text, className) {
      const el = this.document.createElement(tag);
      if (text != null) el.textContent = text;
      if (className) el.className = className;
      return el;
    }
    update() {
      const query = this.$("search").value.trim(), active = !!query;
      this.$("search-options").hidden = !active;
      this.$("conversation-results").hidden = !active;
      this.$("overview").hidden = active;
      if (!active) {
        clearTimeout(this.timer); this.serial++; this.signature = "";
        return false;
      }
      const signature = parameters(query, this.$("search-scope").value, this.$("search-mine").checked, this.filters());
      if (signature === this.signature) return true;
      clearTimeout(this.timer);
      const serial = ++this.serial;
      this.signature = signature; this.query = query; this.items = []; this.cursor = null;
      this.$("search-hits").replaceChildren();
      this.$("search-more").hidden = this.$("search-retry").hidden = true;
      this.$("search-status").textContent = "Searching saved conversations…";
      this.$("results-count").textContent = "";
      this.timer = setTimeout(() => this.fetch(signature, serial), 200);
      return true;
    }
    async fetch(signature, serial, cursor = null) {
      this.$("search-more").disabled = true;
      try {
        const page = await this.api("search?" + signature + (cursor ? "&cursor=" + encodeURIComponent(cursor) : ""));
        if (serial !== this.serial) return;
        // Pages are live; a thread moving in rank should not appear twice.
        const known = new Set(this.items.map(item => item.thread_id));
        this.items.push(...page.items.filter(item => !known.has(item.thread_id)));
        this.cursor = page.next_cursor;
        this.render(page.coverage);
      } catch (error) {
        if (serial !== this.serial) return;
        this.$("search-status").textContent = error.message || "Search failed. Try again.";
        this.$("search-retry").hidden = false;
      } finally {
        if (serial === this.serial) this.$("search-more").disabled = false;
      }
    }
    render(coverage) {
      const scope = this.$("search-scope").value === "all" ? "All conversations, including closed" : "Current view";
      let text = scope + " · " + coverage.saved_histories + " of " + coverage.threads + " conversations have saved history.";
      if (coverage.partial_histories) text += " " + coverage.partial_histories + (coverage.partial_histories === 1 ? " has" : " have") + " partial history.";
      if (coverage.missing_histories) text += " " + coverage.missing_histories + (coverage.missing_histories === 1 ? " has" : " have") + " no searchable saved history.";
      text += " Sessions outside Ribbon Field are not included.";
      this.$("search-status").textContent = text;
      this.$("results-count").textContent = this.items.length + (this.cursor ? "+" : "") + " matching conversations";
      const area = this.$("search-hits"); area.replaceChildren();
      if (!this.items.length) area.append(this.node("p", "No matching conversations in this scope. Try fewer words or search all conversations.", "empty"));
      for (const item of this.items) {
        const card = this.node("article", null, "search-hit");
        const title = this.node("a", item.title, "search-title"); title.href = item.url;
        const heading = this.node("h2"); heading.append(title); card.append(heading);
        const location = this.node("p", [item.repository_name || item.cwd?.split("/").filter(Boolean).pop(), item.client === "claude" ? "Claude" : "Codex",
          {now: "Now", later: "Later", archived: "Closed"}[item.attention]].filter(Boolean).join(" · "), "search-meta");
        location.title = item.cwd || ""; card.append(location);
        for (const match of item.matches) {
          const link = this.node("a", null, "search-excerpt"); link.href = match.url;
          const when = match.timestamp == null ? "Date unavailable" : new Date(match.timestamp * 1000).toLocaleString();
          link.append(this.node("span", (match.role === "user" ? "You" : "Assistant") + " · " + when, "search-meta"));
          const excerpt = this.node("span"); highlight(this.document, excerpt, match.excerpt, this.query); link.append(excerpt);
          card.append(link);
        }
        if (!item.matches.length) card.append(this.node("p", item.summary || item.original_request, "search-context"));
        if (item.context_match) card.append(this.node("small", (item.matches.length ? "Also matches" : "Matches") + " saved thread information.", "search-meta"));
        if (item.message_count > item.matches.length) card.append(this.node("small", item.message_count + " matching messages · showing the latest " + item.matches.length, "search-meta"));
        area.append(card);
      }
      this.$("search-more").hidden = !this.cursor;
      this.$("search-retry").hidden = false;
    }
  }
  const exports = {Search, parameters, highlight};
  if (typeof module !== "undefined" && module.exports) module.exports = exports;
  else root.ConversationSearch = exports;
})(typeof globalThis !== "undefined" ? globalThis : this);
