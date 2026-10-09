/* Stable, read-only app routes shared by links, startup and native navigation. */
(function (root) {
  "use strict";
  const identity = value => typeof value === "string" && /^[a-z\d_.~-]{1,200}$/i.test(value);
  const uuid = value => /^[a-f\d]{8}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{4}-[a-f\d]{12}$/i.test(value);
  function parse(value) {
    if (typeof value !== "string" || value.length > 8192 || /[\s\u0000-\u001f\u007f\\]/.test(value)) throw new Error("Invalid Ribbon Field link.");
    const url = new URL(value), q = url.searchParams;
    if (url.protocol !== "agentcoord:" || !["overview", "view", "thread"].includes(url.host) || url.username || url.password || url.port || url.hash ||
        [...q.keys()].some(key => !["project", "repository", "database", "request", "window", "turn", "item"].includes(key) || q.getAll(key).length !== 1)) {
      throw new Error("Invalid Ribbon Field route.");
    }
    if ((q.has("database") && !/^[a-f\d]{24}$/.test(q.get("database"))) ||
        ["request", "window"].some(key => q.has(key) && !uuid(q.get(key)))) throw new Error("Invalid navigation destination.");
    let route;
    if (["turn", "item"].some(key => q.has(key)) && (url.host !== "thread" ||
        !["turn", "item"].every(key => q.get(key)?.length <= 200 && q.get(key)?.length > 0 && !/[\u0000-\u001f]/.test(q.get(key))))) throw new Error("Invalid message destination.");
    if (url.host === "overview") {
      if (!["", "/"].includes(url.pathname) || ["project", "repository"].some(key => q.has(key) && !identity(q.get(key)))) throw new Error("Invalid overview route.");
      route = {kind: "overview", filters: Object.fromEntries(["project", "repository"].filter(key => q.has(key)).map(key => [key, q.get(key)]))};
    } else {
      const id = decodeURIComponent(url.pathname.slice(1));
      if (!url.pathname.startsWith("/") || !identity(id) || q.has("project") || q.has("repository")) throw new Error("Invalid view or thread route.");
      route = {kind: url.host, id};
      if (q.has("turn")) Object.assign(route, {turn: q.get("turn"), item: q.get("item")});
    }
    return {route, request_id: q.get("request")};
  }
  function safe(value) { try { parse(value); return true; } catch { return false; } }
  function pageURL(snapshot) {
    const query = new URLSearchParams();
    if (snapshot.viewId !== "all") query.set("view", snapshot.viewId);
    else for (const key of ["project", "repository"]) if (snapshot.filters[key]) query.set(key, snapshot.filters[key]);
    return "/" + (query.toString() ? "?" + query : "") + (snapshot.thread ? "#" + encodeURIComponent(snapshot.thread) : "");
  }
  function initialLink(location) {
    const q = new URLSearchParams(location.search);
    if (q.has("navigate")) return q.get("navigate");
    if (q.has("view")) return "agentcoord://view/" + encodeURIComponent(q.get("view"));
    const filters = new URLSearchParams();
    for (const key of ["project", "repository"]) if (q.has(key)) filters.set(key, q.get(key));
    return filters.toString() ? "agentcoord://overview?" + filters : null;
  }
  class Router {
    constructor({api, capture, apply, restore, history, location, windowId, blocked, onError}) {
      Object.assign(this, {api, capture, apply, restore, history, location, windowId, blocked, onError});
      this.applying = false;
      this.pending = Promise.resolve();
    }
    remember() {
      if (this.applying) return;
      const snapshot = this.capture();
      // Streaming pane status events also ask us to remember navigation. Only
      // write real changes: WebKit rate-limits even identical replaceState calls.
      // Compare the current entry so Back/Forward needs no separate cache reset.
      if (JSON.stringify(this.history.state?.agentCoord) === JSON.stringify(snapshot)) return;
      this.history.replaceState({agentCoord: snapshot}, "", pageURL(snapshot));
    }
    open(url, {push = true} = {}) {
      const task = this.pending.catch(() => {}).then(() => this.navigate(url, push));
      this.pending = task;
      return task;
    }
    async navigate(url, push) {
      let request;
      try {
        request = parse(url).request_id;
        const target = await this.api("navigation/resolve", {url});
        if (this.blocked?.()) throw new Error("Finish or close the open dialog before navigating.");
        this.remember();
        this.applying = true;
        await this.apply(target.route);
        const snapshot = this.capture();
        const route = target.route;
        const displayed = route.kind === "thread" ? snapshot.thread === route.id : !snapshot.thread &&
          (route.kind === "view" ? snapshot.viewId === route.id : snapshot.viewId === "all" &&
            ["project", "repository"].every(key => (snapshot.filters[key] || "") === (route.filters[key] || "")));
        if (!displayed) throw new Error("The destination changed before it could be displayed. Open the link again.");
        this.history[push ? "pushState" : "replaceState"]({agentCoord: snapshot}, "", pageURL(snapshot));
        if (request) await this.api("navigation/ack", {request_id: request, status: "displayed", window_id: this.windowId});
        return {status: "displayed"};
      } catch (error) {
        if (request) {
          try { await this.api("navigation/ack", {request_id: request, status: "failed", error: String(error.message).slice(0, 2000), window_id: this.windowId}); }
          catch { /* A different database or unavailable backend cannot acknowledge. */ }
        }
        this.onError(error);
        return {status: "failed", error: error.message};
      } finally { this.applying = false; }
    }
    back(snapshot) {
      const task = this.pending.catch(() => {}).then(async () => {
        if (!snapshot) return;
        this.applying = true;
        try { await this.restore(snapshot); }
        catch (error) { this.onError(error); }
        finally { this.applying = false; }
      });
      this.pending = task;
      return task;
    }
  }
  const api = {parse, safe, pageURL, initialLink, Router};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.agentCoordNavigation = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
