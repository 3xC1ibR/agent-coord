/* Shared by workspace windows and the browser/native Settings surface. */
(() => {
  "use strict";
  const key = "agent-coord.new-session-defaults";
  const providers = {codex: "Codex", claude: "Claude Code", antigravity: "Google Antigravity", kimi: "Kimi Code", muse: "Muse Code"};
  function normalize(value) {
    if (!value || !Object.hasOwn(providers, value.client)) return {client: "codex", model: null, effort: null, yolo: false};
    const text = value => typeof value === "string" && value.trim() ? value.trim() : null;
    return {client: value.client, model: text(value.model), effort: text(value.effort), yolo: value.yolo === true};
  }
  function read(storage) {
    try { return normalize(JSON.parse((storage || globalThis.localStorage).getItem(key))); }
    catch { return normalize(null); }
  }
  function write(value, storage = globalThis.localStorage) {
    const saved = normalize(value);
    storage.setItem(key, JSON.stringify(saved));
    return saved;
  }
  function mount({document, api, config, report}) {
    const $ = id => document.getElementById(id);
    const model = $("new-session-model"), effort = $("new-session-effort"), permissions = $("new-session-permissions");
    const status = $("new-session-defaults-status"), catalogs = new Map(), failures = new Map();
    let preference = read(), revision = 0, reasoningUnavailable = false;
    const option = (label, value) => { const item = document.createElement("option"); item.textContent = label; item.value = value; return item; };
    const modelValue = (client, model) => JSON.stringify([client, model || ""]);
    function renderModels() {
      model.replaceChildren();
      for (const [client, label] of Object.entries(providers)) {
        const group = document.createElement("optgroup"); group.label = label;
        group.append(option(label + " configured model", modelValue(client, "")));
        const models = catalogs.get(client) || [];
        for (const item of models) group.append(option(item.displayName || item.model, modelValue(client, item.model)));
        if (preference.client === client && preference.model && !models.some(item => item.model === preference.model))
          group.append(option(preference.model + " (saved)", modelValue(client, preference.model)));
        model.append(group);
      }
      model.value = modelValue(preference.client, preference.model);
      model.disabled = false;
      permissions.value = preference.yolo ? "full" : "default";
      permissions.disabled = false;
    }
    async function renderEfforts() {
      const current = ++revision, selected = {...preference};
      reasoningUnavailable = false;
      effort.replaceChildren(option("Loading reasoning levels…", "")); effort.disabled = true;
      try {
        let item = (catalogs.get(selected.client) || []).find(item => item.model === selected.model);
        if (!selected.model) {
          const effective = await api("session-defaults", {client: selected.client, cwd: config.cwd, model: null});
          item = effective.model && (catalogs.get(selected.client) || []).find(item => item.model === effective.model || item.resolvedModel === effective.model);
        }
        if (current !== revision) return;
        const levels = item?.supportedReasoningEfforts || [];
        effort.replaceChildren(option(levels.length || !item ? "Model default" : "Not configurable", ""));
        for (const level of levels) effort.append(option(level.reasoningEffort, level.reasoningEffort));
        if (preference.effort && !levels.some(level => level.reasoningEffort === preference.effort))
          effort.append(option(preference.effort + " (saved)", preference.effort));
        effort.value = preference.effort || "";
        effort.disabled = !levels.length && !preference.effort;
        if (failures.has(selected.client)) status.textContent = "Could not load models for " + providers[selected.client] + ". Saved choices are retained.";
      } catch (error) {
        if (current !== revision) return;
        reasoningUnavailable = true;
        effort.replaceChildren(option(preference.effort ? preference.effort + " (saved)" : "Model default", preference.effort || ""));
        status.textContent = "Could not load reasoning levels. Saved choices are retained.";
      }
    }
    function save(next) {
      try { preference = write(next); status.textContent = "Saved. Applies to new drafts on this device."; return true; }
      catch { report(Error("Could not save New button defaults. Allow local storage and try again.")); renderModels(); renderEfforts(); return false; }
    }
    model.onchange = () => {
      const [client, selected] = JSON.parse(model.value);
      save({...preference, client, model: selected || null, effort: null});
      renderEfforts();
    };
    effort.onchange = () => { save({...preference, effort: effort.value || null}); };
    permissions.onchange = () => { save({...preference, yolo: permissions.value === "full"}); };
    async function refresh() {
      $("refresh-new-session-models").disabled = true;
      status.textContent = "Loading models…";
      await Promise.all(Object.keys(providers).map(async client => {
        try { catalogs.set(client, (await api("models?client=" + encodeURIComponent(client))).data || []); failures.delete(client); }
        catch (error) { failures.set(client, error); }
        renderModels();
        if (client === preference.client) await renderEfforts();
      }));
      status.textContent = reasoningUnavailable ? "Could not load reasoning levels. Refresh models to try again." :
        failures.size ? "Some providers could not load. Refresh models to try again." : "Changes apply to new drafts on this device.";
      $("refresh-new-session-models").disabled = false;
    }
    $("refresh-new-session-models").onclick = refresh;
    renderModels();
    return refresh();
  }
  globalThis.NewSessionDefaults = {read, write, mount};
})();

/* The same preferences surface is used by the browser and native Settings window. */
(() => {
  "use strict";
  if (typeof document === "undefined" || !document.getElementById("close-settings")) return;
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
    NewSessionDefaults.mount({document, api, config, report}).catch(report);
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
