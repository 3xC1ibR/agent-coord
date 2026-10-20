/* The same preferences surface is used by the browser and native Settings window. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const report = error => { $("settings-error").textContent = error.message || String(error); $("settings-error").hidden = false; };
  const desktop = window.agentCoordDesktop;
  $("close-settings").onclick = () => {
    if (desktop?.closeSettings) desktop.closeSettings().catch(report);
    else if (window.parent !== window) window.parent.postMessage({type: "ribbon-field-close-settings"}, location.origin);
    else if (window.opener) window.close();
    else location.href = "/";
  };
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape" || event.defaultPrevented || event.isComposing) return;
    // Let nested dialogs and choice controls cancel their own interaction first.
    if (document.querySelector("dialog[open]") ||
        event.target?.closest?.('select, [role="combobox"][aria-expanded="true"]')) return;
    event.preventDefault(); event.stopPropagation(); $("close-settings").click();
  }, {capture: true});
  async function boot() {
    const response = await fetch("/api/browser/config", {cache: "no-store"});
    if (!response.ok) throw Error("Could not load Settings. Reload to try again.");
    const config = await response.json();
    async function request(prefix, path, body) {
      const options = {cache: "no-store"};
      if (body !== undefined) Object.assign(options, {method: "POST", headers: {"Content-Type": "application/json", "X-Agent-Coord-Token": config.token}, body: JSON.stringify(body)});
      const response = await fetch(prefix + path, options), data = await response.json();
      if (!response.ok) throw Error(data.error || "Could not update Settings.");
      return data;
    }
    const api = (path, body) => request("/api/browser/", path, body);
    window.agentCoordJev.mountSettings($("jev-settings"), config);
    if (!config.remote) {
      const metrics = await api("launch-metrics");
      if (metrics.available) {
        $("launch-metrics-settings").hidden = false;
        const toggle = $("launch-metrics-enabled");
        toggle.checked = metrics.enabled;
        toggle.onchange = async () => {
          toggle.disabled = true;
          try { toggle.checked = (await api("launch-metrics", {enabled: toggle.checked})).enabled; }
          catch (error) { toggle.checked = !toggle.checked; report(error); }
          finally { toggle.disabled = false; }
        };
      }
    }
    // The iPhone wrapper already owns APNs permission and device registration.
    const nativePhone = config.remote && window.parent.document.querySelector("#native-notification-style");
    if (nativePhone) {
      $("notifications").hidden = true;
      $("notification-description").textContent = "Manage iPhone notifications in Connection settings.";
    } else {
      const Notifications = config.remote ? PhoneNotifications : TurnNotifications;
      const notifications = new Notifications({button: $("notifications"), api, listen: false, selected: () => null,
        openThread: id => { location.href = "/#" + encodeURIComponent(id); }, onError: report});
      if (config.remote) {
        $("notification-description").textContent = "Background alerts for this device, including when Ribbon Field is closed.";
        notifications.start().catch(report);
      }
    }
    window.agentCoordSpeech.mountSettings($("speech-settings"), config).catch(report);
    if (!config.remote) {
      $("remote-settings").hidden = false;
      const remote = new RemoteAccessPanel({document, container: $("remote-settings"), clipboard: navigator.clipboard,
        request: (path, body) => request("/api/remote/", path, body)});
      remote.open();
    }
    if (desktop?.fileEditor) {
      $("file-settings").hidden = false;
      const updateEditor = async action => {
        try {
          const editor = await desktop.fileEditor(action);
          $("file-editor-name").textContent = editor.name;
          $("file-editor-name").title = editor.path || "";
          $("reset-file-editor").disabled = !editor.path;
        } catch (error) { report(error); }
      };
      $("choose-file-editor").onclick = () => updateEditor("choose");
      $("reset-file-editor").onclick = () => updateEditor("reset");
      updateEditor("get");
    }
  }
  boot().catch(report);
})();
