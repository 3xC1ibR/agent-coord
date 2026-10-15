/* Tailscale administration stays on the Mac; paired devices get the chat UI. */
(() => {
  "use strict";
  class RemoteAccessPanel {
    constructor({document, request, clipboard, container}) {
      this.document = document; this.request = request; this.clipboard = clipboard;
      this.embedded = !!container;
      this.dialog = container || document.createElement("dialog");
      if (!container) this.dialog.className = "remote-access-dialog";
      this.dialog.setAttribute("aria-labelledby", "remote-access-title");
      this.dialog.innerHTML = `<form method="dialog" class="remote-access-heading"><h2 id="remote-access-title">Remote access</h2><button aria-label="Close remote access">Close</button></form>
        <p>Continue conversations on your iPhone. Keep this Mac awake and connect both devices to the same Tailscale network.</p>
        <p><a href="https://tailscale.com/download" target="_blank" rel="noreferrer">Install Tailscale</a> · <a href="https://login.tailscale.com/admin/dns" target="_blank" rel="noreferrer">Tailscale HTTPS settings</a></p>
        <p class="remote-status" role="status" aria-live="polite"></p>
        <label>HTTPS port <input class="remote-port" type="number" min="1" max="65535" value="443"></label>
        <div class="remote-actions"><button class="remote-enable primary" type="button">Enable Tailscale HTTPS</button><button class="remote-disable" type="button" hidden>Disable remote access</button><button class="remote-refresh" type="button">Refresh status</button></div>
        <p class="remote-address"></p>
        <section class="remote-pair" hidden><h3>Pair your iPhone</h3><p>Create a link and open it in Safari on your phone. Links work once and expire after five minutes. A paired device can read conversations, send messages, and answer approvals.</p>
        <button class="remote-create-link" type="button">Create pairing link</button>
        <div class="remote-link-box" hidden><label>Pairing link <input class="remote-link" type="text" readonly spellcheck="false"></label><button class="remote-copy" type="button">Copy link</button><small>Keep this link private. Creating another link replaces it.</small></div>
        <h3>Paired devices</h3><div class="remote-devices"></div></section>`;
      if (container) this.find(".remote-access-heading button").remove();
      else document.body.append(this.dialog);
      this.find(".remote-enable").onclick = () => this.run(async () => this.render(await request("enable", {port: Number(this.find(".remote-port").value)})));
      this.find(".remote-disable").onclick = () => this.run(async () => {
        this.clearLink(); this.render(await request("disable", {}));
      });
      this.find(".remote-refresh").onclick = () => this.load();
      this.find(".remote-create-link").onclick = () => this.run(async () => {
        const result = await request("pairing", {});
        this.find(".remote-link").value = result.url;
        this.find(".remote-link-box").hidden = false;
        this.find(".remote-status").textContent = "Pairing link ready. Open it on your phone within five minutes.";
      });
      this.find(".remote-copy").onclick = () => this.run(async () => {
        const field = this.find(".remote-link");
        if (this.clipboard?.writeText) {
          await this.clipboard.writeText(field.value);
          this.find(".remote-status").textContent = "Pairing link copied. Open it in Safari on your phone.";
        } else {
          field.focus(); field.select();
          this.find(".remote-status").textContent = "Copy the selected link and open it in Safari on your phone.";
        }
      });
      this.dialog.addEventListener("close", () => this.clearLink());
    }
    find(selector) { return this.dialog.querySelector(selector); }
    clearLink() { this.find(".remote-link").value = ""; this.find(".remote-link-box").hidden = true; }
    async run(task) {
      if (this.busy) return;
      this.busy = true;
      const buttons = [...this.dialog.querySelectorAll("button[type=button]")];
      buttons.forEach(button => { button.disabled = true; });
      this.find(".remote-status").textContent = "Checking Tailscale…";
      try { await task(); }
      catch (error) { this.find(".remote-status").textContent = error.message || "Could not update remote access."; }
      finally { buttons.forEach(button => { button.disabled = false; }); this.busy = false; }
    }
    load() { return this.run(async () => this.render(await this.request("status"))); }
    open() { if (!this.embedded) this.dialog.showModal(); return this.load(); }
    render(status) {
      this.find(".remote-status").textContent = status.error || (status.enabled ? "Tailscale HTTPS is enabled." : "Remote access is off.");
      this.find(".remote-port").value = status.port;
      this.find(".remote-port").disabled = status.enabled;
      this.find(".remote-enable").hidden = status.enabled;
      this.find(".remote-disable").hidden = !status.enabled && !status.configured;
      this.find(".remote-pair").hidden = !status.enabled;
      this.find(".remote-address").textContent = status.url || "";
      const devices = this.find(".remote-devices"); devices.replaceChildren();
      for (const device of status.devices || []) {
        const row = this.document.createElement("div"), name = this.document.createElement("span"), revoke = this.document.createElement("button");
        row.className = "remote-device"; name.textContent = device.name;
        revoke.textContent = "Revoke"; revoke.type = "button";
        revoke.setAttribute("aria-label", `Revoke ${device.name}`);
        revoke.onclick = () => this.run(async () => this.render(await this.request("revoke", {id: device.id})));
        row.append(name, revoke); devices.append(row);
      }
      if (!devices.childNodes.length) devices.textContent = "No paired devices yet.";
    }
  }
  if (typeof module !== "undefined") module.exports = {RemoteAccessPanel};
  if (typeof window === "undefined") return;
  window.RemoteAccessPanel = RemoteAccessPanel;
})();
