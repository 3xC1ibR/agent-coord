#!/usr/bin/env node
"use strict";
const {chromium} = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const {cwd, epoch, ids, prompts, snapshot} = require("./fixture");
const WEB = path.resolve(__dirname, "../../plugins/agent-coord/scripts/agent_coord/web");
const OUT = path.join(__dirname, "out");
const SHOTS = path.join(OUT, "shots");
const ORIGIN = "https://ribbon-demo.test";
const mime = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".png": "image/png", ".webmanifest": "application/json"};

async function main() {
  fs.mkdirSync(SHOTS, {recursive: true});
  let current = snapshot();
  const errors = [], unhandled = [], boxes = {};
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 740}, deviceScaleFactor: 2,
      colorScheme: "dark", reducedMotion: "reduce", serviceWorkers: "block"});
    // Every request is fulfilled locally. No app runtime, provider process, live
    // user data, remote network, or real agent-coord API is involved.
    await context.route("**/*", async route => {
      const url = new URL(route.request().url());
      if (url.origin !== ORIGIN) { unhandled.push(url.href); return route.abort(); }
      const p = url.pathname.replace("/api/browser/", "");
      let result;
      if (url.pathname.startsWith("/api/")) {
        if (p === "config") result = {token: "fixture-only", cwd, workspaceRoot: cwd, remote: false, completionCursor: 0};
        else if (p === "threads" || p === "sessions") result = {data: current.threads};
        else if (p === "organization") result = {repositories: [], projects: []};
        else if (["views", "schedules", "workspaces"].includes(p)) result = {data: []};
        else if (p === "workspaces/open") result = {cwd, name: "Playground"};
        else if (p === "models") result = {data: [{id: "gpt-6.1-sol", model: "gpt-6.1-sol", displayName: "GPT-6.1-Sol", supportedReasoningEfforts: [{reasoningEffort: "medium", description: "Medium"}], defaultReasoningEffort: "medium"}]};
        else if (p.startsWith("threads/")) {
          const id = p.split("/")[1];
          result = p.endsWith("/messages") ? {data: current.details[id]?.coordinationMessages || []} : current.details[id]?.work_thread;
        } else if (p.startsWith("sessions/")) result = current.details[p.split("/")[1]];
        else if (p === "events") return route.fulfill({status: 200, contentType: "text/event-stream", body: ": fixture\n\n"});
        else if (p === "speech/settings") result = {enabled: false};
        else if (url.pathname === "/api/remote/status") result = {enabled: false};
        else { unhandled.push(url.pathname); result = {data: []}; }
        return route.fulfill({json: result ?? {error: "Missing fixture"}, status: result == null ? 404 : 200});
      }
      const asset = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
      const file = path.resolve(WEB, asset);
      if (!file.startsWith(WEB + path.sep) || !fs.existsSync(file)) { unhandled.push(url.pathname); return route.abort(); }
      return route.fulfill({path: file, contentType: mime[path.extname(file)] || "application/octet-stream"});
    });
    const page = await context.newPage();
    page.on("pageerror", e => errors.push(e.message));
    await page.addInitScript(({epoch}) => {
      Date.now = () => (epoch + 65) * 1000;
      localStorage.setItem("agent-coord.theme", "dark");
      // A fixture transport stays connected; it must never open a live stream.
      window.EventSource = class extends EventTarget {
        constructor() { super(); this.readyState = 1; setTimeout(() => this.onopen?.(new Event("open")), 0); }
        close() { this.readyState = 2; }
      };
    }, {epoch});
    await page.goto(ORIGIN, {waitUntil: "domcontentloaded"});
    await page.locator("#dispatcher-dock:not([hidden])").waitFor({timeout: 10000}).catch(async error => {
      await page.screenshot({path: path.join(OUT, "capture-error.png")});
      console.error({errors, unhandled, uiError: await page.locator("#error").innerText()});
      throw error;
    });
    await page.waitForFunction(() => state.detail && !state.detailRefreshing);
    await page.locator("#menu-toggle").click();
    // Real UI view preferences, not a replacement layout.
    await page.evaluate(() => { $("group-by").value = "none"; state.listSignature = ""; renderList(); });
    await page.addStyleTag({content: "*,*::before,*::after {animation: none !important; transition: none !important; caret-color: transparent !important}"});
    boxes.typography = await page.locator("#message").evaluate(el => {
      const css = getComputedStyle(el), composer = getComputedStyle(document.getElementById("composer"));
      return Object.fromEntries(["fontFamily", "fontSize", "fontWeight", "lineHeight", "color", "paddingTop", "paddingLeft", "paddingRight"].map(key => [key, css[key]])
        .concat([["backgroundColor", composer.backgroundColor]]));
    });
    async function capture(name) {
      await page.waitForTimeout(80);
      await page.evaluate(() => document.activeElement?.blur());
      const selectors = {message: "#message", composer: "#composer", send: "#send", receipt: "#dispatcher-receipt", dock: "#dispatcher-dock",
        ...Object.fromEntries(Object.entries(ids).map(([k,id]) => [k, `#overview [data-card-thread='${id}']`]))};
      boxes[name] = {};
      for (const [key, selector] of Object.entries(selectors)) {
        const locator = page.locator(selector).first();
        const rect = await locator.count() ? await locator.boundingBox() : null;
        if (rect) boxes[name][key] = rect;
      }
      await page.screenshot({path: path.join(SHOTS, name + ".png"), animations: "disabled"});
      console.log("captured", name);
    }
    await capture("00-ready");
    for (let i = 0; i < prompts.length; i++) {
      await page.locator("#message").fill(prompts[i].text);
      await capture(`prompt-${i}`);
      await page.locator("#message").fill("");
      current = snapshot(i, true);
      await page.evaluate(async () => { await refreshDetail(); await refreshList(); });
      await capture(`routing-${i}`);
      current = snapshot(i + 1);
      await page.evaluate(async () => { await refreshDetail(); await refreshList(); });
      await capture(`sent-${i}`);
      assert.equal(await page.locator(`#overview [data-card-thread='${ids[prompts[i].target]}']`).count(), 1);
    }
    assert.equal(current.threads.length, 4, "Follow-ups must reuse the same three app agents");
    assert.equal(current.details[ids.dispatcher].dispatchRequests.length, 6);
    await page.evaluate(id => select(id), ids.snake);
    await page.waitForFunction(() => state.selected === "demo-snake" && !state.detailRefreshing);
    await page.evaluate(() => { $("timeline").scrollTop = $("timeline").scrollHeight; });
    await capture("snake-conversation");
    assert.match(await page.locator("#timeline").innerText(), /swipe controls/);
    await page.evaluate(id => select(id, {dock: true}), ids.dispatcher);
    await page.waitForFunction(() => !state.detailRefreshing);
    await page.locator("#dispatcher-expand").click();
    await page.evaluate(() => { $("timeline").parentElement.scrollTop = 100000; });
    await capture("routing-history");
    assert.deepEqual(errors, [], "Frontend errors");
    assert.deepEqual([...new Set(unhandled)], [], "Unexpected network calls");
    fs.writeFileSync(path.join(SHOTS, "boxes.json"), JSON.stringify(boxes, null, 2));
    fs.writeFileSync(path.join(OUT, "validation.json"), JSON.stringify({frontendErrors: errors, unexpectedRequests: unhandled,
      realAgentsStarted: 0, databaseOpened: false, appAgents: 3, routedRequests: 6, snapshots: Object.keys(boxes).filter(k => k !== "typography").length}, null, 2));
  } finally { await browser.close(); }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
