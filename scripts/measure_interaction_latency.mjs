#!/usr/bin/env node
// Measure user-visible Ribbon Field interaction latency in a real browser.
//
// Drives the served web interface exactly as a user does: press Enter in the
// composer, open conversations, and type a search. Every timing starts at the
// browser's own input event timestamp and ends when the DOM shows the result.
//
//   node scripts/measure_interaction_latency.mjs --base http://127.0.0.1:PORT \
//     --thread THREAD_ID [--sends 10] [--open THREAD_ID,...] [--search q1,q2] \
//     [--engine chromium|webkit] [--output report.json]
//
// Sends wait for each turn to finish before the next one, so the target thread
// should be a disposable conversation. Opens and searches do not mutate state
// beyond what a user's navigation does.
import {createRequire} from "node:module";
import {writeFileSync} from "node:fs";
import {dirname, join} from "node:path";
import {fileURLToPath} from "node:url";

const require = createRequire(join(dirname(fileURLToPath(import.meta.url)), "..", "demo", "package.json"));
const playwright = require("playwright");

const args = Object.fromEntries(process.argv.slice(2).reduce((pairs, value, index, all) => {
  if (value.startsWith("--")) pairs.push([value.slice(2), all[index + 1]?.startsWith("--") ? "true" : all[index + 1]]);
  return pairs;
}, []));
const base = (args.base || "").replace(/\/$/, "");
if (!base) throw new Error("--base is required");
const sends = Number(args.sends || 0);
const opens = (args.open || "").split(",").filter(Boolean);
const searches = (args.search || "").split(",").filter(Boolean);
const message = args.message || "Reply with only the word OK.";

function quantiles(values) {
  const ordered = [...values].sort((a, b) => a - b);
  const at = q => ordered.length ? Math.round(ordered[Math.min(ordered.length - 1, Math.floor(q * (ordered.length - 1) + 0.5))] * 10) / 10 : null;
  return {n: ordered.length, p50: at(0.5), p90: at(0.9), p95: at(0.95), max: at(1)};
}

const browser = await playwright[args.engine || "chromium"].launch({headless: true});
const page = await browser.newPage({viewport: {width: 1440, height: 900}});
const report = {base, engine: args.engine || "chromium", at: new Date().toISOString(), sends: [], opens: [], searches: [], errors: []};
page.on("pageerror", error => report.errors.push(String(error)));

// In-page probes: timestamps share performance.now()'s clock with event.timeStamp.
await page.addInitScript(() => {
  const probe = globalThis.__latencyProbe = {posts: [], enter: null};
  const original = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const url = String(input?.url || input);
    const started = performance.now();
    const response = await original(input, init);
    if ((init?.method || "GET") === "POST" && /\/(messages|queue)$/.test(url)) probe.posts.push({url, started, finished: performance.now(), status: response.status});
    return response;
  };
  document.addEventListener("keydown", event => {
    if (event.key === "Enter" && event.target?.id === "message") probe.enter = event.timeStamp;
  }, true);
});

async function idle(timeout = 180000) {
  await page.waitForFunction(() => {
    // Send stays disabled until the open conversation's state is known.
    const input = document.getElementById("message"), send = document.getElementById("send"), stop = document.getElementById("stop");
    return input && !input.disabled && send && !send.disabled && !(stop && stop.offsetParent && !stop.hidden);
  }, null, {timeout, polling: 100});
}

async function openThread(id, {empty = false} = {}) {
  const started = Date.now();
  await page.goto(base + "/#" + encodeURIComponent(id), {waitUntil: "domcontentloaded"});
  await page.waitForFunction(empty => empty ? !document.getElementById("conversation")?.hidden && !document.getElementById("message")?.disabled
    : document.querySelectorAll("#timeline article.message").length > 0, empty, {timeout: 60000, polling: 50});
  return Date.now() - started;
}

if (sends) {
  report.coldOpenMs = await openThread(args.thread, {empty: true});
  for (let index = 0; index < sends; index++) {
    await idle();
    await page.waitForTimeout(Number(args.pause || 1500));
    const marker = `${message} [probe ${Date.now().toString(36)}-${index}]`;
    await page.fill("#message", marker);
    await page.evaluate(() => { globalThis.__latencyProbe.posts = []; globalThis.__latencyProbe.enter = null; });
    const visible = page.evaluate(token => new Promise(resolve => {
      const seen = () => [...document.querySelectorAll("#timeline article.message.user")].some(node => node.textContent.includes(token));
      if (seen()) return resolve(performance.now());
      const observer = new MutationObserver(() => { if (seen()) { observer.disconnect(); resolve(performance.now()); } });
      observer.observe(document.getElementById("timeline"), {childList: true, subtree: true, characterData: true});
    }), marker.slice(-14));
    await page.focus("#message");
    await page.keyboard.press("Enter");
    const shownAt = await visible;
    const replyStarted = Date.now();
    await page.waitForFunction(() => globalThis.__latencyProbe.posts.some(post => post.finished), null, {timeout: 120000, polling: 20});
    const probe = await page.evaluate(() => globalThis.__latencyProbe);
    const post = probe.posts[0];
    await idle();
    report.sends.push({index, visibleMs: shownAt - probe.enter, acceptedMs: post.finished - probe.enter,
      postMs: post.finished - post.started, status: post.status, turnMs: Date.now() - replyStarted});
    console.log(JSON.stringify(report.sends.at(-1)));
  }
}

for (const id of opens) {
  for (let round = 0; round < 3; round++) {
    const ms = await openThread(id);
    report.opens.push({thread: id, round, ms});
    console.log(JSON.stringify(report.opens.at(-1)));
  }
}

// In-app switching, as from a tab or list: the page is already loaded.
const switches = (args.switch || "").split(",").filter(Boolean);
if (switches.length) {
  await page.goto(base + "/#" + encodeURIComponent(switches[0]), {waitUntil: "domcontentloaded"});
  await page.waitForFunction(() => document.querySelectorAll("#timeline article.message").length > 0, null, {timeout: 60000});
  for (let round = 0; round < Number(args.rounds || 4); round++) {
    for (const id of switches) {
      const timing = await page.evaluate(async id => {
        const started = performance.now();
        const opening = select(id);
        // First paint of the conversation, cached or fresh.
        await new Promise(resolve => {
          const check = () => state.selected === id && state.timelineThread === id &&
            document.querySelectorAll("#timeline article.message").length ? resolve() : requestAnimationFrame(check);
          check();
        });
        const painted = performance.now() - started;
        await opening;
        // Current data (no refresh pending) and the cost of one full timeline render.
        while (state.detailRefreshing || conversationEntry(id).pending) await new Promise(resolve => setTimeout(resolve, 10));
        const current = performance.now() - started;
        const renderStart = performance.now(); renderTimeline();
        return {painted, current, render: performance.now() - renderStart,
          messages: document.querySelectorAll("#timeline article.message").length};
      }, id);
      report.switches = report.switches || [];
      report.switches.push({thread: id, round, ...timing});
      console.log(JSON.stringify(report.switches.at(-1)));
    }
  }
}

for (const query of searches) {
  await page.goto(base + "/", {waitUntil: "domcontentloaded"});
  try { await page.waitForSelector("#search", {state: "visible", timeout: 15000}); }
  catch (error) { report.errors.push("search field not visible: " + error.message.split("\n")[0]); break; }
  await page.waitForTimeout(1000);
  for (let round = 0; round < 3; round++) {
    await page.fill("#search", "");
    await page.waitForTimeout(300);
    const before = await page.evaluate(() => performance.now());
    const answered = page.waitForResponse(response => response.url().includes("/api/browser/search") &&
      response.url().includes("q=" + encodeURIComponent(query)), {timeout: 60000});
    await page.fill("#search", query);
    await answered;
    const ms = await page.evaluate(start => new Promise(resolve => requestAnimationFrame(() => resolve(performance.now() - start))), before);
    report.searches.push({query, round, ms});
    console.log(JSON.stringify(report.searches.at(-1)));
  }
}

report.summary = {
  visibleMs: quantiles(report.sends.map(s => s.visibleMs)),
  acceptedMs: quantiles(report.sends.map(s => s.acceptedMs)),
  postMs: quantiles(report.sends.map(s => s.postMs)),
  openMs: quantiles(report.opens.map(o => o.ms)),
  switchPaintedMs: quantiles((report.switches || []).map(s => s.painted)),
  switchCurrentMs: quantiles((report.switches || []).map(s => s.current)),
  timelineRenderMs: quantiles((report.switches || []).map(s => s.render)),
  searchMs: quantiles(report.searches.map(s => s.ms)),
};
console.log(JSON.stringify(report.summary, null, 1));
if (args.output) writeFileSync(args.output, JSON.stringify(report, null, 1));
await browser.close();
