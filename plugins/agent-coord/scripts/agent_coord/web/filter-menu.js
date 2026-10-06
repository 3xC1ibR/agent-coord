"use strict";

function createFilterMenu(doc) {
  const menu = doc.getElementById("filter-menu");
  const toggle = doc.getElementById("filter-toggle");
  const clear = doc.getElementById("clear-filters");
  const view = doc.getElementById("view");
  const phase = doc.getElementById("phase-filter");
  const repository = doc.getElementById("repository");
  const project = doc.getElementById("project");

  clear.addEventListener("click", () => {
    const reload = view.value !== "active";
    view.value = "active";
    phase.value = "";
    repository.value = "";
    project.value = "";
    (reload ? view : phase).dispatchEvent(new Event("change", {bubbles: true}));
    menu.open = false;
    toggle.focus();
  });
  doc.addEventListener("pointerdown", event => {
    if (menu.open && !menu.contains(event.target)) menu.open = false;
  });
  doc.addEventListener("keydown", event => {
    if (event.key !== "Escape" || !menu.open) return;
    menu.open = false;
    toggle.focus();
    event.preventDefault();
  });
}

if (typeof module !== "undefined") module.exports = createFilterMenu;
else createFilterMenu(document);
