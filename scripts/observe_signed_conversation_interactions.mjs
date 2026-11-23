// Browser-side observers for validate_signed_runtime_responsiveness.py.
import {appendFileSync, existsSync, readFileSync} from "node:fs";
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
  return u.protocol === "http:" && u.hostname === "127.0.0.1" && u.origin === new URL(state().url).origin ? route.continue() : route.abort();
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
const profiles = [];
if (existsSync(resolve(directory, "measure-browser-execution"))) {
  for (const [name, page] of Object.entries({opening, composing, receiving})) {
    const client = await context.newCDPSession(page);
    await client.send("Profiler.enable");
    await client.send("Profiler.setSamplingInterval", {interval: 10000});
    await client.send("Profiler.start");
    profiles.push({name, client});
    page.on("pageerror", error => log({phase: state().phase, kind: "page_error", page: name,
      error: error.message.slice(0, 250)}));
    const requests = new WeakMap();
    page.on("request", request => requests.set(request, Date.now()));
    const recordRequest = request => {
      const elapsed = Date.now() - requests.get(request);
      if (elapsed >= 500 || request.failure()) {
        log({phase: state().phase, kind: "browser_request", page: name, method: request.method(),
          path: new URL(request.url()).pathname, elapsed_ms: elapsed, failure: request.failure()?.errorText});
      }
    };
    page.on("requestfinished", recordRequest);
    page.on("requestfailed", recordRequest);
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
      log({phase, kind: "browser_open", ok: true, start, navigated, end: Date.now(), state: await openingState(id)});
    } catch (error) {
      log({phase, kind: "browser_open", ok: false, start, navigated, end: Date.now(),
        error: error.message.split("\n")[0], state: await openingState(id)});
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
    log({phase, kind:"browser_send_intent", marker, start});
    try {
      if (current !== url) {
        const address = url + "#" + encodeURIComponent(composer);
        await Promise.all([composing, receiving].map(p => p.goto(address, {waitUntil: "domcontentloaded"})));
        current = url;
      }
      await composing.locator("#message").fill(marker);
      await composing.evaluate(marker => {
        document.querySelector("#message").addEventListener("keydown", e => {
          window.__releaseEnter = e.timeStamp;
          const entry = state.conversations?.get(state.selected);
          window.__releaseComposer = {
            draft_matches: e.target.value === marker,
            selected_matches: state.selected === decodeURIComponent(location.hash.slice(1)),
            has_detail: !!state.detail, refreshing: !!state.detailRefreshing,
            read_pending: !!entry?.pending, opening_submission: !!entry?.openingSubmission,
            sending: !!state.sending?.has(state.selected), send_disabled: document.querySelector("#send").disabled,
            composer_hidden: document.querySelector("#composer").hidden, detail_pending_input: !!state.detail?.requests?.length,
          };
        }, {once:true});
      }, marker);
      const tracked = composing.waitForRequest(r => r.method() === "POST" && /\/messages$/.test(new URL(r.url()).pathname)
        && r.postDataJSON()?.message === marker, {timeout: 7000}).then(async request => {
        const body = request.postDataJSON();
        const response = await request.response();
        return {request_id: body.requestId || null, status: response?.status() || null};
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
      log({phase, kind:"browser_send", marker, ...receipt, controls_at_enter: controlsAtEnter, ok: receipt.status >= 200 && receipt.status < 300 && own !== null && other !== null,
        start, end:Date.now(), own_ms:own === null ? null : own-enter, other_ms:other === null ? null : other-enter});
    } catch (error) {
      current = null;
      log({phase,kind:"browser_send",marker,ok:false,start,end:Date.now(),error:error.message.split("\n")[0]});
    }
    await sleep(interval);
  }
}
try {
  await Promise.all([opens(), sends()]);
} finally {
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
