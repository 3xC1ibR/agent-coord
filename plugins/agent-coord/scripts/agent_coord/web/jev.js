/* Optional Jev setup shared by the workspace and Settings. */
(() => {
  "use strict";
  const disclosure = "When enabled, selected excerpts of your requests, completed replies, and task summaries are sent to TypeSafe. Tool outputs, reasoning, and images are excluded. Usage is billed through your TypeSafe account.";
  function client(config) {
    return async body => {
      const options = {cache: "no-store"};
      if (body !== undefined) Object.assign(options, {method: "POST", headers: {
        "Content-Type": "application/json", "X-Agent-Coord-Token": config.token}, body: JSON.stringify(body)});
      const response = await fetch("/api/browser/jev/settings", options);
      const data = await response.json();
      if (!response.ok) throw Error(data.error || "Could not update Jev settings. Try again.");
      return data;
    };
  }
  function controls(container, config, request = client(config)) {
    if (config.remote) { container.hidden = true; return; }
    container.hidden = false;
    container.innerHTML = `<h2 id="jev-heading">Automatic conversation status</h2>
      <p>Use Jev to distinguish completed work, progress updates, and replies requiring your input. Optional; Ribbon Field works without it.</p>
      <p>${disclosure}</p>
      <p><a href="https://console.typesafe.ai/" target="_blank" rel="noopener noreferrer">Get a TypeSafe API key</a></p>
      <form id="jev-form">
        <label for="jev-key">TypeSafe API key<input id="jev-key" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="4096" placeholder="Paste your API key"></label>
        <p id="jev-storage">Your key stays on this computer and is never saved in conversation history.</p>
        <div class="settings-actions"><button id="jev-enable" type="submit">Test and enable</button>
          <button id="jev-disable" type="button" hidden>Disable</button><button id="jev-remove" type="button" hidden>Remove key</button></div>
      </form>
      <p id="jev-status" role="status" aria-live="polite">Loading Jev settings…</p>
      <p id="jev-error" role="alert" hidden></p>`;
    const $ = id => container.querySelector("#" + id);
    let current, pending = false;
    const report = error => { $("jev-error").textContent = error.message || String(error); $("jev-error").hidden = false; };
    function render(value) {
      current = value;
      $("jev-status").textContent = value.enabled ? (value.verified ? "Connected · Automatic status is enabled." : "Automatic status is enabled. Test your saved key to verify the connection.") : "Automatic status is off.";
      $("jev-disable").hidden = !value.enabled;
      $("jev-remove").hidden = !value.configured;
      $("jev-key").placeholder = value.configured ? "Leave blank to use your saved key" : "Paste your API key";
      $("jev-storage").textContent = value.storage === "keychain" ? "Your key is stored in macOS Keychain. The connection test uses sample text." :
        "Keys are stored in macOS Keychain on Mac, or a private local file on other systems. The connection test uses sample text.";
    }
    async function update(action, event) {
      event?.preventDefault();
      if (pending) return;
      if (action === "test_enable" && !$("jev-key").value.trim() && !current?.configured) {
        report(Error("Enter a TypeSafe API key first.")); $("jev-key").focus(); return;
      }
      const body = {action};
      if (action === "test_enable" && $("jev-key").value.trim()) body.api_key = $("jev-key").value.trim();
      // Clear immediately so failed requests and hidden Settings do not retain secrets.
      $("jev-key").value = "";
      pending = true; $("jev-error").hidden = true;
      for (const node of container.querySelectorAll("button, input")) node.disabled = true;
      $("jev-status").textContent = action === "test_enable" ? "Testing with sample text…" : "Saving…";
      try { render(await request(body)); }
      catch (error) { if (current) render(current); else $("jev-status").textContent = "Could not load Jev settings."; report(error); }
      finally { pending = false; for (const node of container.querySelectorAll("button, input")) node.disabled = false; }
    }
    $("jev-form").onsubmit = event => update("test_enable", event);
    $("jev-disable").onclick = event => update("disable", event);
    $("jev-remove").onclick = event => update("remove", event);
    return request().then(render).catch(error => { $("jev-status").textContent = "Could not load Jev settings."; report(error); });
  }
  async function onboard(config, request = client(config)) {
    if (config.remote || window.parent !== window || new URLSearchParams(location.search).has("detached")) return;
    const value = await request();
    if (value.onboarding_dismissed || value.configured) return;
    const card = document.createElement("aside");
    card.className = "jev-onboarding"; card.setAttribute("aria-labelledby", "jev-welcome-title");
    card.innerHTML = `<h2 id="jev-welcome-title">See what needs your attention</h2>
      <p>Enable automatic conversation status with Jev to distinguish completed work, updates, and replies needing your input.</p>
      <p>Optional. Requires a TypeSafe API key and sends selected conversation excerpts to TypeSafe.</p>
      <div><button type="button" data-jev-setup>Set up</button><button type="button" class="quiet" data-jev-skip>Skip for now</button></div>
      <p class="jev-onboarding-error" role="alert" hidden></p>`;
    document.body.append(card);
    async function dismiss(setup) {
      const buttons = card.querySelectorAll("button");
      for (const node of buttons) node.disabled = true;
      try {
        await request({action: "dismiss"}); card.remove();
        if (setup) document.getElementById("open-settings").click();
      } catch (error) {
        const message = card.querySelector("[role=alert]"); message.textContent = error.message; message.hidden = false;
        for (const node of buttons) node.disabled = false;
      }
    }
    card.querySelector("[data-jev-setup]").onclick = () => dismiss(true);
    card.querySelector("[data-jev-skip]").onclick = () => dismiss(false);
  }
  window.agentCoordJev = {mountSettings: controls, onboard};
  if (!document.body.classList.contains("settings-page")) {
    fetch("/api/browser/config", {cache: "no-store"}).then(response => {
      if (!response.ok) throw Error("Settings unavailable");
      return response.json();
    }).then(config => onboard(config)).catch(() => {});
  }
})();
