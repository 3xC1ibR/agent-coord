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
    try {
      await opening.goto(url + "#" + encodeURIComponent(id), {waitUntil: "domcontentloaded"});
      await visible(opening, id);
      log({phase, kind: "browser_open", ok: true, start, end: Date.now()});
    } catch (error) {
      log({phase, kind: "browser_open", ok: false, start, end: Date.now(), error: error.message.split("\n")[0]});
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
      await composing.evaluate(() => {
        document.querySelector("#message").addEventListener("keydown", e => { window.__releaseEnter = e.timeStamp; }, {once:true});
      });
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
      const enter = await composing.evaluate(() => performance.timeOrigin + window.__releaseEnter);
      log({phase, kind:"browser_send", marker, ...receipt, ok: receipt.status >= 200 && receipt.status < 300 && own !== null && other !== null,
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
  await browser.close();
}
