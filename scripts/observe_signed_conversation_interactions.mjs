// Browser-side observers for validate_signed_runtime_responsiveness.py.
import {appendFileSync, existsSync, readFileSync, renameSync, writeFileSync} from "node:fs";
import {createRequire} from "node:module";
import {resolve} from "node:path";
import {randomUUID} from "node:crypto";

const [directory, moduleName] = process.argv.slice(2);
const {chromium} = createRequire(new URL("../demo/package.json", import.meta.url))(moduleName || "playwright");
const state = () => JSON.parse(readFileSync(resolve(directory, "browser-state.json"), "utf8"));
const output = resolve(directory, "browser-observations.jsonl");
const stopped = () => existsSync(resolve(directory, "stop-browser"));
const sleep = ms => new Promise(done => setTimeout(done, ms));
const log = record => appendFileSync(output, JSON.stringify(record) + "\n", {mode: 0o600});
const browser = await chromium.launch({headless: true});
const context = await browser.newContext();
await context.route("**/*", route => {
  const u = new URL(route.request().url());
  const active = state();
  const visit = u.origin === active.visit_origin && u.pathname === "/api/browser/activity/events" &&
    !u.search && ["POST", "OPTIONS"].includes(route.request().method());
  return u.protocol === "http:" && u.hostname === "127.0.0.1" &&
    (u.origin === new URL(active.url).origin || visit) ? route.continue() : route.abort();
});
const interval = 250;
const pageFor = async () => {
  const page = await context.newPage();
  page.setDefaultTimeout(7000);
  return page;
};
const opening = await pageFor();
const composing = await pageFor();
const receiving = await pageFor();
// Keep a bounded navigation trace in the observer. A refresh can replace the
// whole document, so retain document identity and distinguish fragment events.
await opening.addInitScript(() => {
  window.__releaseNavigation = [];
  for (const type of ["popstate", "hashchange", "pageshow"]) {
    addEventListener(type, () => {
      window.__releaseNavigation.push({type, at: Math.round(performance.now()),
        hash: location.hash, saved: history.state?.agentCoord?.thread});
      if (window.__releaseNavigation.length > 10) window.__releaseNavigation.shift();
    });
  }
});
async function openingState(thread) {
  return opening.evaluate(id => {
    const ready = typeof state !== "undefined", selected = ready && state.selected;
    const entry = ready && state.conversations?.get(id);
    const matches = hash => hash === "#" + encodeURIComponent(id);
    return {document_started: performance.timeOrigin, hash_matches: matches(location.hash),
      has_directory: new URLSearchParams(location.search).has("directory"),
      router_ready: typeof navigation !== "undefined" && !!navigation,
      router_applying: typeof navigation !== "undefined" && !!navigation?.applying,
      selected_matches: selected === id, saved_matches: history.state?.agentCoord?.thread === id,
      timeline_matches: ready && state.timelineThread === id, navigation_revision: ready && state.navigationRevision,
      detail_available: !!entry?.detail, read_pending: !!entry?.pending, read_failed: !!entry?.refreshError,
      messages: document.querySelectorAll("#timeline article.message").length,
      events: (window.__releaseNavigation || []).map(event => ({
        type: event.type, at: event.at, hash_matches: matches(event.hash), saved_matches: event.saved === id,
      }))};
  }, thread).catch(() => null);
}
// Keep only the route class, loopback origin and timing. In particular, the SSE
// query and the browser config contain credentials, and sessions carry messages.
function localOrigin(url) {
  const address = new URL(url);
  return address.protocol === "http:" && address.hostname === "127.0.0.1" ? address.origin : null;
}
function routeName(request) {
  const path = new URL(request.url()).pathname;
  if (request.isNavigationRequest()) return "document";
  if (path === "/app.js") return "application";
  if (!path.startsWith("/api/browser/")) return null;
  const parts = path.slice(13).split("/");
  if (parts[0] === "threads" && parts.length === 2 && request.method() === "POST") {
    const body = request.postData();
    if (body && body.length < 512) {
      try {
        const fields = JSON.parse(body);
        if (fields?.seen === true && Object.keys(fields).every(key =>
          ["seen", "seen_checkpoint_id", "seen_completion_id"].includes(key))) return "threads.mark-seen";
      } catch { /* The trace records only recognized route classes. */ }
    }
  }
  if (parts[0] === "activity" && parts[1] === "events") return "activity.events";
  if (parts[0] === "workspaces" && parts[1] === "open") return "workspaces.open";
  if (parts[0] === "notifications" && parts[1] === "claim") return "notifications.claim";
  if (parts[0] === "activity" && parts[1] === "settings") return "activity.settings";
  if (["threads", "sessions"].includes(parts[0])) {
    if (parts.length === 1) return parts[0];
    const action = ["messages", "sidecars", "requests", "queue", "agent-tree", "scratchpad", "preview", "close-plan", "prewarm", "close-when-done"].includes(parts[2]) ? parts[2] : "detail";
    return parts[0] + "." + action;
  }
  return ["config", "events", "search", "models", "permission-modes", "session-defaults", "organization", "views",
    "asks", "schedules", "desktop-projects", "update-submissions"].includes(parts[0]) ? parts[0] : "other-api";
}
function browserError(error) {
  return error?.name === "TimeoutError" ? "timeout" :
    /ERR_ABORTED|interrupted by another navigation/.test(error?.message || "") ? "navigation-interrupted" : "browser-error";
}
const activeRequests = new Map();
const history = [];
const counts = new Map();
const slowest = new Map();
const attachments = new Set();
for (const [name, page] of Object.entries({opening, composing, receiving})) {
  page.on("request", request => {
    const route = routeName(request), origin = localOrigin(request.url());
    if (route && origin) activeRequests.set(request, {page: name, origin, route, method: request.method(),
      phase: state().phase, start: Date.now()});
  });
  page.on("response", async response => {
    const row = activeRequests.get(response.request());
    if (!row) return;
    row.status = response.status();
    if (row.route !== "config" || row.status !== 200) return;
    const key = name + "/" + row.origin;
    if (attachments.has(key)) return;
    try {
      const config = await response.json();
      if (typeof config.token !== "string" || !config.token) return;
      attachments.add(key);
      log({...row, kind: "browser_attachment", end: Date.now(), continuity: config.interactionContinuity === true});
    } catch { /* A navigation can cancel an in-flight response. */ }
  });
  function finish(request) {
    const row = activeRequests.get(request);
    if (!row) return;
    activeRequests.delete(request);
    const failure = request.failure()?.errorText;
    if (failure) row.failure = failure.match(/net::(ERR_[A-Z_]+)/)?.[1] || "network-failure";
    row.end = Date.now();
    row.elapsed_ms = row.end - row.start;
    const key = [row.phase, row.page, row.origin, row.method, row.route, row.failure || row.status || "unknown"].join(" ");
    counts.set(key, (counts.get(key) || 0) + 1);
    const timing = request.timing();
    if (timing.requestStart >= 0 && timing.responseStart >= timing.requestStart) {
      row.before_send_ms = timing.requestStart;
      row.until_headers_ms = timing.responseStart - timing.requestStart;
    }
    if (!slowest.has(key) || slowest.get(key).elapsed_ms < row.elapsed_ms) slowest.set(key, row);
    history.push(row);
    if (history.length > 192) history.shift();
  }
  page.on("requestfinished", finish);
  page.on("requestfailed", finish);
}
function saveNetwork() {
  const path = resolve(directory, "browser-network.json");
  writeFileSync(path + ".partial", JSON.stringify({at: Date.now(), recent: history,
    pending: Array.from(activeRequests.values()).slice(-192), counts: Object.fromEntries(counts),
    slowest: Array.from(slowest.values())}), {mode: 0o600});
  renameSync(path + ".partial", path);
}
const networkClock = setInterval(saveNetwork, 1000);
const profiles = [];
if (existsSync(resolve(directory, "measure-browser-execution"))) {
  await receiving.addInitScript(() => {
    const NativeEventSource = window.EventSource;
    window.EventSource = class extends NativeEventSource {
      constructor(...args) {
        super(...args);
        this.addEventListener("message", event => {
          if (window.__releaseObservedMarker && window.__releaseStreamAt == null &&
              event.data.includes(window.__releaseObservedMarker)) {
            window.__releaseStreamAt = performance.timeOrigin + performance.now();
            window.__releaseServerSentAt = JSON.parse(event.data).serverSentAt ?? null;
          }
        });
      }
    };
  });
  for (const [name, page] of Object.entries({opening, composing, receiving})) {
    const client = await context.newCDPSession(page);
    await client.send("Profiler.enable");
    await client.send("Profiler.setSamplingInterval", {interval: 10000});
    await client.send("Profiler.start");
    profiles.push({name, client});
    page.on("pageerror", error => log({phase: state().phase, kind: "page_error", page: name,
      error: browserError(error)}));
  }
}
let counter = 0;
const visible = async (page, id) => {
  await page.waitForFunction(id => typeof state !== "undefined" && state.timelineThread === id
    && document.querySelector("#timeline article.message"), id, {timeout: 7000});
};
async function opens() {
  while (!stopped()) {
    const {phase, url, peers} = state();
    const id = peers[counter++ % peers.length];
    const start = Date.now();
    let navigated;
    try {
      await opening.goto(url + "#" + encodeURIComponent(id), {waitUntil: "domcontentloaded"});
      navigated = Date.now();
      await visible(opening, id);
      log({phase, kind: "browser_open", ok: true, origin: localOrigin(url), start, navigated, end: Date.now(), state: await openingState(id)});
    } catch (error) {
      log({phase, kind: "browser_open", ok: false, origin: localOrigin(url), start, navigated, end: Date.now(),
        error: browserError(error), state: await openingState(id)});
    }
    await sleep(interval);
  }
}
async function sends() {
  let current = null;
  while (!stopped()) {
    const {phase, url, composer} = state();
    const start = Date.now();
    const marker = randomUUID();
    log({phase, kind:"browser_send_intent", origin:localOrigin(url), marker, start});
    try {
      if (current !== url) {
        const address = url + "#" + encodeURIComponent(composer);
        await Promise.all([composing, receiving].map(p => p.goto(address, {waitUntil: "domcontentloaded"})));
        current = url;
      }
      await composing.locator("#message").fill(marker);
      if (profiles.length) await receiving.evaluate(token => {
        window.__releaseObservedMarker = token;
        window.__releaseStreamAt = null;
        window.__releaseServerSentAt = null;
      }, marker);
      await composing.evaluate(marker => {
        document.querySelector("#message").addEventListener("keydown", e => {
          window.__releaseEnter = e.timeStamp;
          const entry = state.conversations?.get(state.selected);
          window.__releaseComposer = {
            draft_matches: e.target.value === marker,
            selected_matches: state.selected === decodeURIComponent(location.hash.slice(1)),
            detail_running: !!state.detail?.running, active_turn_present: !!state.detail?.activeTurn,
            pending_transfer: !!state.outgoing?.pendingTransferCounts?.has(state.selected),
            ordered_composer: document.querySelector("#send").getAttribute("aria-label") === "Send after pending messages",
            accepts_untargeted_input: !!state.detail?.acceptsUntargetedInput, has_detail: !!state.detail, refreshing: !!state.detailRefreshing,
            read_pending: !!entry?.pending, opening_submission: !!entry?.openingSubmission,
            sending: !!state.sending?.has(state.selected), send_disabled: document.querySelector("#send").disabled,
            composer_hidden: document.querySelector("#composer").hidden, detail_pending_input: !!state.detail?.requests?.length,
          };
        }, {once:true, capture:true});
      }, marker);
      const tracked = composing.waitForRequest(r => r.method() === "POST" && /\/messages$/.test(new URL(r.url()).pathname)
        && r.postDataJSON()?.message === marker, {timeout: 7000}).then(async request => {
        const body = request.postDataJSON(), requested = Date.now();
        const response = await request.response();
        const responded = Date.now(), timing = request.timing();
        const serverTiming = response?.headers()["server-timing"]?.match(/submission;dur=([\d.]+)/);
        const actual = await response?.json().catch(() => ({}));
        const refusal = response && !response.ok() ? [
          ["active_turn_changed", /active turn changed|turn .*changed/i],
          ["pending_approval", /decision is pending|approval/i],
          ["receiver_changed", /receiving coordinator changed|database changed/i],
          ["unavailable_input_state", /input state is unavailable/i],
          ["receiving_coordinator_declined", /receiving coordinator declined/i],
          ["handover_completed", /handover just completed/i],
        ].find(([, pattern]) => pattern.test(actual?.error || ""))?.[0] || "other_refusal" : null;
        return {request_id: body.requestId || null, expected_turn_id: body.expectedTurnId || null,
          receipt_turn_id: actual?.turn?.id || actual?.turnId || null, refusal, post_origin: localOrigin(request.url()), status: response?.status() || null, requested, responded,
          sent_at: timing.requestStart >= 0 ? timing.startTime + timing.requestStart : null,
          server_ms: serverTiming ? Number(serverTiming[1]) : null};
      }).catch(() => ({request_id: null, status: null}));
      const see = page => page.evaluate(token => new Promise(resolve => {
        const check = () => [...document.querySelectorAll("#timeline article.message.user")].some(el => el.textContent.includes(token));
        if (check()) {resolve(performance.timeOrigin + performance.now());return;}
        const obs = new MutationObserver(() => {if(check()){obs.disconnect();resolve(performance.timeOrigin + performance.now());}});
        obs.observe(document.body, {childList:true,subtree:true,characterData:true});
        setTimeout(()=>{obs.disconnect();resolve(null);},7000);
      }), marker).catch(() => null);
      const first = see(composing), second = see(receiving);
      await composing.locator("#message").press("Enter");
      const [own,other,receipt] = await Promise.all([first,second,tracked]);
      const [enter, controlsAtEnter] = await composing.evaluate(() => [performance.timeOrigin + window.__releaseEnter, window.__releaseComposer]);
      const [streamAt, serverSentAt] = profiles.length ? await receiving.evaluate(() =>
        [window.__releaseStreamAt, window.__releaseServerSentAt]) : [null, null];
      log({phase, kind:"browser_send", origin:localOrigin(url), marker, ...receipt, enter, stream_at: streamAt, server_sent_at: serverSentAt, controls_at_enter: controlsAtEnter, ok: receipt.status >= 200 && receipt.status < 300 && own !== null && other !== null,
        start, end:Date.now(), own_ms:own === null ? null : own-enter, other_ms:other === null ? null : other-enter});
    } catch (error) {
      current = null;
      log({phase,kind:"browser_send",origin:localOrigin(url),marker,ok:false,start,end:Date.now(),error:browserError(error)});
    }
    await sleep(interval);
  }
}
try {
  await Promise.all([opens(), sends()]);
} finally {
  clearInterval(networkClock);
  saveNetwork();
  await Promise.all(profiles.map(async ({name, client}) => {
    const {profile} = await client.send("Profiler.stop");
    const nodes = new Map(profile.nodes.map(node => [node.id, node]));
    const elapsed = new Map();
    for (const [index, id] of profile.samples.entries()) {
      elapsed.set(id, (elapsed.get(id) || 0) + profile.timeDeltas[index] / 1000);
    }
    const frames = [...elapsed].sort((a, b) => b[1] - a[1]).slice(0, 20).map(([id, ms]) => {
      const frame = nodes.get(id).callFrame;
      return {name: frame.functionName, file: frame.url.split("/").pop().split("?")[0],
        line: frame.lineNumber + 1, elapsed_ms: Math.round(ms)};
    });
    log({phase: "profiling", kind: "browser_cpu", page: name, frames});
  }));
  await browser.close();
}
