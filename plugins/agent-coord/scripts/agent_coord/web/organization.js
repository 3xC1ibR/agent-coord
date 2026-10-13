"use strict";

const threadOrganization = (() => {
  const NONE = "__none__";
  function matches(thread, repository = "", project = "") {
    return [["repository", repository], ["project", project]].every(([field, selected]) =>
      !selected || (selected === NONE ? thread[field + "_id"] == null : thread[field + "_id"] === selected));
  }
  function groupThreads(threads, dimension) {
    if (dimension === "domain") {
      const domains = new Map();
      for (const thread of threads) {
        const name = [thread.domain, thread.scope_cluster].find(value => typeof value === "string" && value.trim());
        const id = name ? name.trim().replace(/\s+/g, " ").toLowerCase() : null;
        if (!domains.has(id)) domains.set(id, {
          key: "domain-" + (id === null ? "missing" : "value-" + encodeURIComponent(id)), id,
          label: id === null ? "No domain" : id === "." ? "Workspace" : id, detail: "", threads: [],
        });
        domains.get(id).threads.push(thread);
      }
      return [...domains.values()].sort((a, b) => Number(a.id === null) - Number(b.id === null) || a.label.localeCompare(b.label));
    }
    if (!["repository", "project"].includes(dimension)) return [{key: "all", label: "Threads", threads}];
    const groups = new Map();
    for (const thread of threads) {
      const id = thread[dimension + "_id"] ?? NONE;
      if (!groups.has(id)) groups.set(id, {
        key: dimension + "-" + id, id,
        label: thread[dimension + "_name"] || (dimension === "repository" ? "No repository" : "No group"),
        detail: dimension === "repository" ? thread.repository_root || "" : "",
        threads: [],
      });
      groups.get(id).threads.push(thread);
    }
    return [...groups.values()].sort((a, b) => (a.id === NONE) - (b.id === NONE) ||
      a.label.localeCompare(b.label) || a.detail.localeCompare(b.detail) || a.id.localeCompare(b.id));
  }
  return {NONE, matches, groupThreads};
})();

if (typeof module !== "undefined") module.exports = threadOrganization;
