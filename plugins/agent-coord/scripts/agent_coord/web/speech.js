/* Application-owned speech: no agent prompts, tools, or provider changes. */
(() => {
  "use strict";
  const completed = detail => (detail?.thread?.turns || []).filter(turn => turn.id && turn.status === "completed" &&
    (turn.items || []).some(item => item.type === "agentMessage" && item.text && (!item.phase || item.phase === "final_answer")));

  class SpeechController {
    constructor({request, audio, storage, locks, changed = () => {}, url = URL}) {
      Object.assign(this, {request, audio, storage, locks, changed, url});
      this.memory = new Map(); this.observed = new Map(); this.queue = []; this.pending = new Set();
      this.selected = null; this.detail = null; this.current = null; this.message = "";
      this.audio.onended = () => { if (this.current) this.finish(); };
      this.audio.onerror = () => { if (this.current) this.fail("Audio could not play. Try again."); };
    }
    read(key) { try { return this.storage.getItem(key) ?? this.memory.get(key); } catch { return this.memory.get(key); } }
    write(key, value) { this.memory.set(key, value); try { this.storage.setItem(key, value); } catch {} }
    enabled(id) { return !!id && this.read("ribbon.speech.enabled:" + id) === "true"; }
    key(job) { return "ribbon.speech.heard:" + job.thread + ":" + job.turn; }
    remember(detail) { return new Set(completed(detail).map(turn => turn.id)); }
    update(id, detail) {
      this.selected = id; this.detail = detail?.work_thread?.thread_id === id ? detail : null;
      if (this.detail) {
        if (!this.observed.has(id)) this.observed.set(id, this.remember(detail));
        else this.observe(id, detail);
      }
      this.changed();
    }
    toggle() {
      const id = this.selected;
      if (!id || !this.detail?.work_thread?.browser_session) return;
      const enabled = !this.enabled(id);
      this.write("ribbon.speech.enabled:" + id, String(enabled));
      this.observed.set(id, this.remember(this.detail));
      // Mark the existing transcript so a later completion never reads old replies.
      for (const turn of completed(this.detail)) this.write(this.key({thread: id, turn: turn.id}), "true");
      if (!enabled) this.cancelThread(id);
      this.changed();
    }
    observe(id, detail) {
      const previous = this.observed.get(id) || new Set();
      for (const turn of completed(detail)) {
        if (!previous.has(turn.id) && this.enabled(id)) this.enqueue({thread: id, turn: turn.id, title: detail.work_thread?.title || "Reply"});
        previous.add(turn.id);
      }
      this.observed.set(id, previous);
    }
    receive(events) {
      for (const event of events) {
        if (event.status !== "completed" || !this.enabled(event.thread_id)) continue;
        // Fetch only enabled chats; the normal agent conversation is unchanged.
        this.request("sessions/" + encodeURIComponent(event.thread_id)).then(detail => {
          if (!this.enabled(event.thread_id)) return;
          // On a newly opened tab, only consider the latest completed reply.
          if (!this.observed.has(event.thread_id)) {
            const turns = completed(detail), latest = turns.at(-1);
            this.observed.set(event.thread_id, new Set(turns.map(turn => turn.id)));
            if (latest) this.enqueue({thread: event.thread_id, turn: latest.id, title: event.title || "Reply"});
          } else this.observe(event.thread_id, detail);
        }).catch(error => { this.message = error.message; this.changed(); });
      }
    }
    enqueue(job) {
      const key = this.key(job);
      if (this.pending.has(key) || this.read(key) === "true") return;
      if (this.queue.length >= 10) { this.message = "Speech queue is full. New replies were skipped."; this.changed(); return; }
      this.pending.add(key); this.queue.push(job); this.pump();
    }
    async pump() {
      if (this.pumping) return;
      this.pumping = true;
      try {
        while (this.queue.length) {
          const job = this.queue.shift();
          this.waiting = job;
          const run = async () => {
            if (job.cancelled || !this.enabled(job.thread) || this.read(this.key(job)) === "true") return;
            this.write(this.key(job), "true");
            await this.run(job);
          };
          // Serialize across tabs/panes on this device without affecting other devices.
          if (this.locks) await this.locks.request("ribbon-field-speech", run);
          else await run();
          this.pending.delete(this.key(job));
          this.waiting = null;
        }
      } catch (error) { this.message = error.message; this.changed(); }
      finally { this.pumping = false; }
    }
    async run(job) {
      this.current = job; this.message = "Preparing speech · " + job.title; this.retry = false;
      this.done = new Promise(resolve => { this.resolve = resolve; });
      this.abort = new AbortController(); this.changed();
      try {
        const blob = await this.request("speech/audio", {thread_id: job.thread, turn_id: job.turn}, this.abort.signal);
        if (this.current !== job || !this.enabled(job.thread)) return;
        this.objectURL = this.url.createObjectURL(blob); this.audio.src = this.objectURL;
        await this.play();
      } catch (error) { if (this.current === job) this.fail(error.message); }
      await this.done;
    }
    async play() {
      const job = this.current;
      if (!job) return;
      try {
        await this.audio.play();
        if (this.current !== job) return;
        this.retry = false; this.message = "Reading aloud · " + job.title;
      } catch {
        if (this.current !== job) return;
        this.retry = true; this.message = "Tap Play to hear the reply · " + job.title;
      }
      this.changed();
    }
    fail(message) {
      this.message = message; this.retry = true; this.changed();
    }
    retryCurrent() {
      if (!this.current) return;
      if (this.objectURL) { this.play(); return; }
      const job = this.current;
      this.finish();
      this.write(this.key(job), "false");
      this.pending.delete(this.key(job)); this.enqueue(job);
    }
    finish() {
      this.abort?.abort(); this.audio.pause(); this.audio.removeAttribute("src"); this.audio.load();
      if (this.objectURL) this.url.revokeObjectURL(this.objectURL);
      this.objectURL = null; this.current = null; this.retry = false; this.message = "";
      this.resolve?.(); this.resolve = null; this.changed();
    }
    stop() {
      if (this.waiting) this.waiting.cancelled = true;
      this.queue = []; this.pending.clear(); this.finish();
    }
    cancelThread(id) {
      if (this.waiting?.thread === id) this.waiting.cancelled = true;
      this.queue = this.queue.filter(job => { if (job.thread !== id) return true; this.pending.delete(this.key(job)); return false; });
      if (this.current?.thread === id) this.finish();
    }
  }

  if (typeof module !== "undefined") module.exports = {SpeechController, completed};
  if (typeof window === "undefined") return;
  const audio = new Audio(); audio.preload = "auto";
  let config, controller, dialog, selected, detail;
  const $ = id => document.getElementById(id);
  async function request(path, body, signal) {
    const options = {cache: "no-store", signal};
    if (body !== undefined) Object.assign(options, {method: "POST", headers: {"Content-Type": "application/json", "X-Agent-Coord-Token": config.token}, body: JSON.stringify(body)});
    const response = await fetch("/api/browser/" + path, options);
    if (!response.ok) { const error = await response.json(); throw Error(error.error || "Speech request failed."); }
    return path === "speech/audio" ? response.blob() : response.json();
  }
  function render() {
    const button = $("speech-toggle");
    if (!button || !controller) return;
    const enabled = controller.enabled(controller.selected);
    button.disabled = !controller.detail?.work_thread?.browser_session;
    button.setAttribute("aria-pressed", String(enabled));
    button.setAttribute("aria-label", enabled ? "Turn off speech for this chat" : "Read replies aloud");
    button.title = enabled ? "Speech on for this chat on this device" : "Read new replies aloud on this device";
    $("speech-player").hidden = !controller.message;
    $("speech-status").textContent = controller.message;
    $("speech-play").hidden = !controller.retry;
    $("speech-play").textContent = controller.objectURL ? "Play" : "Retry speech";
    $("speech-stop").textContent = controller.current ? "Stop speech" : "Dismiss";
  }
  function initialize() {
    if (controller || !config || !$("speech-toggle")) return;
    let storage;
    try { storage = window.localStorage; } catch { /* Session-only preferences. */ }
    controller = new SpeechController({request, audio, storage, locks: navigator.locks, changed: render});
    document.querySelector(".workspace-bar").after($("speech-player"));
    $("speech-toggle").onclick = () => {
      controller.toggle();
      // Prime the same media element within the enabling gesture. Browsers may
      // still reject a later autoplay; the visible Play control remains available.
      if (!controller.enabled(controller.selected) || controller.current) return;
      const bytes = new Uint8Array(46), view = new DataView(bytes.buffer);
      for (const [offset, text] of [[0, "RIFF"], [8, "WAVEfmt "], [36, "data"]]) {
        for (let i = 0; i < text.length; i++) bytes[offset + i] = text.charCodeAt(i);
      }
      view.setUint32(4, 38, true); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
      view.setUint16(22, 1, true); view.setUint32(24, 8000, true); view.setUint32(28, 16000, true);
      view.setUint16(32, 2, true); view.setUint16(34, 16, true); view.setUint32(40, 2, true);
      const silence = URL.createObjectURL(new Blob([bytes], {type: "audio/wav"}));
      audio.src = silence;
      audio.play().catch(() => {}).finally(() => URL.revokeObjectURL(silence));
    };
    $("speech-play").onclick = () => controller.retryCurrent();
    $("speech-stop").onclick = () => controller.stop();
    const button = document.createElement("button"); button.type = "button"; button.className = "notification-toggle";
    button.textContent = "Speech settings"; button.onclick = () => openSettings();
    document.querySelector(".sidebar-footer")?.prepend(button);
    window.addEventListener("storage", event => {
      if (event.key?.startsWith("ribbon.speech.enabled:")) {
        const id = event.key.slice("ribbon.speech.enabled:".length);
        if (!controller.enabled(id)) controller.cancelThread(id);
        render();
      }
    });
    window.addEventListener("pagehide", () => controller.stop());
    controller.update(selected, detail);
  }
  async function openSettings() {
    if (!dialog) {
      dialog = document.createElement("dialog"); dialog.className = "speech-dialog"; dialog.setAttribute("aria-labelledby", "speech-heading");
      dialog.innerHTML = `<h2 id="speech-heading">Speech</h2><p>Read replies aloud with Kokoro running on your Mac. Use the speaker button in a chat to read new replies automatically on this device.</p>
        <p>For a fresh setup, install and start <a href="https://github.com/remsky/Kokoro-FastAPI#readme" target="_blank" rel="noreferrer">Kokoro-FastAPI</a>, then enter its address here. No scripts or checkout paths are needed. Keep the Mac awake when listening on your phone.</p>
        <form><label>Kokoro address<input name="base_url" type="url" required placeholder="http://127.0.0.1:8880/v1"></label><label>Voice<input name="voice" required placeholder="af_heart"></label><label>Speed<input name="speed" type="number" min="0.5" max="2" step="0.1" required></label>
        <p class="speech-settings-status" role="status"></p><footer><button type="submit" class="primary">Save</button><button type="button" class="speech-test">Test voice</button><button type="button" class="speech-close">Close</button></footer></form>`;
      document.body.append(dialog);
      dialog.querySelector(".speech-close").onclick = () => dialog.close();
      const status = dialog.querySelector(".speech-settings-status");
      const form = dialog.querySelector("form");
      const save = async () => request("speech/settings", {base_url: form.elements.base_url.value.trim(), voice: form.elements.voice.value.trim(), speed: Number(form.elements.speed.value)});
      form.onsubmit = async event => { event.preventDefault(); try { await save(); status.textContent = "Speech settings saved."; } catch (error) { status.textContent = error.message; } };
      let testAudio, testURL;
      const cleanup = () => { testAudio?.pause(); testAudio?.remove(); testAudio = null; if (testURL) URL.revokeObjectURL(testURL); testURL = null; };
      dialog.addEventListener("close", cleanup);
      dialog.querySelector(".speech-test").onclick = async event => {
        const button = event.currentTarget; button.disabled = true; status.textContent = "Preparing a voice sample…";
        cleanup(); controller.stop();
        try {
          if (!config.remote) await save();
          const blob = await request("speech/audio", {test: true});
          if (!dialog.open) return;
          testURL = URL.createObjectURL(blob); testAudio = new Audio(testURL); testAudio.controls = true;
          dialog.querySelector("audio")?.remove(); dialog.append(testAudio);
          status.textContent = "Voice sample ready.";
          try { await testAudio.play(); } catch { status.textContent = "Press Play on the sample to hear the voice."; }
        } catch (error) { status.textContent = error.message; }
        finally { button.disabled = false; }
      };
    }
    dialog.showModal();
    const form = dialog.querySelector("form"), status = dialog.querySelector(".speech-settings-status");
    status.textContent = "Loading speech settings…";
    try {
      const settings = await request("speech/settings");
      for (const name of ["base_url", "voice", "speed"]) { form.elements[name].value = settings[name]; form.elements[name].disabled = config.remote; }
      form.querySelector("button[type=submit]").hidden = config.remote;
      status.textContent = config.remote ? "Configure Kokoro on your Mac. You can test playback here." : "Changes apply to all devices. Chat speech switches are saved on each device.";
    } catch (error) { status.textContent = error.message; }
  }
  window.agentCoordSpeech = {
    update(id, snapshot, settings) { selected = id; detail = snapshot; config = settings; initialize(); controller?.update(id, snapshot); },
    receive(events) { controller?.receive(events); },
  };
})();
