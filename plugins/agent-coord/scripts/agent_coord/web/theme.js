/* Run in the head so a saved theme is applied before the first paint. */
(() => {
  "use strict";
  const key = "agent-coord.theme";
  const themeColors = {light: "#f5f5f4", dark: "#121215"};
  const normalize = value => ["light", "dark"].includes(value) ? value : "system";
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  let preference = "system";
  try { preference = normalize(window.localStorage.getItem(key)); } catch { /* Storage is optional. */ }

  function apply() {
    const theme = preference === "system" ? (media.matches ? "dark" : "light") : preference;
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    if (typeof document.querySelector === "function") {
      const meta = document.querySelector('meta[name="theme-color"]');
      if (meta) meta.setAttribute("content", themeColors[theme]);
    }
    document.querySelectorAll("[data-theme-select]").forEach(select => { select.value = preference; });
  }
  apply();
  media.addEventListener("change", apply);
  window.addEventListener("storage", event => {
    if (event.key !== key && event.key !== null) return;
    // Ignore sessionStorage events; native windows also forward local preferences.
    if (event.storageArea && event.storageArea !== window.localStorage) return;
    preference = normalize(event.newValue);
    apply();
  });
  document.addEventListener("DOMContentLoaded", () => {
    apply();
    document.querySelectorAll("[data-theme-select]").forEach(select => {
      select.addEventListener("change", () => {
        preference = normalize(select.value);
        try { window.localStorage.setItem(key, preference); } catch { /* Keep the choice for this page. */ }
        apply();
      });
    });
  }, {once: true});
})();
