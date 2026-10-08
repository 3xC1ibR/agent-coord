#!/usr/bin/env node
/* Capture real Ribbon Field UI states from the seeded demo database.
 *
 * Starts the dependency-free UI server against demo/out/demo.sqlite3, drives it
 * with Playwright, and writes 2x PNG stills plus element bounding boxes to
 * demo/out/shots. The composition uses those boxes to aim callouts and cursor.
 */
"use strict";
const {chromium} = require("playwright");
const {spawn} = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const HERE = __dirname;
const OUT = path.join(HERE, "out");
const SHOTS = path.join(OUT, "shots");
const CLI = path.join(HERE, "..", "plugins", "agent-coord", "scripts", "agent-coord");
const DB = path.join(OUT, "demo.sqlite3");
const PORT = 47311;
const BASE = `http://127.0.0.1:${PORT}`;
const manifest = JSON.parse(fs.readFileSync(path.join(OUT, "manifest.json"), "utf8"));
const boxes = {};

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function waitForPort(port, timeoutMs = 20000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    (function attempt() {
      const socket = net.connect(port, "127.0.0.1");
      socket.once("connect", () => { socket.destroy(); resolve(); });
      socket.once("error", () => {
        socket.destroy();
        if (Date.now() - started > timeoutMs) reject(new Error("UI server did not start"));
        else setTimeout(attempt, 200);
      });
    })();
  });
}

async function record(page, name, selectors = {}) {
  const entry = {};
  for (const [key, selector] of Object.entries(selectors)) {
    try {
      const locator = page.locator(selector).first();
      if (await locator.count()) {
        const box = await locator.boundingBox();
        if (box) entry[key] = box;
      }
    } catch (error) { /* optional element */ }
  }
  boxes[name] = entry;
}

async function shot(page, name, selectors = {}) {
  await page.waitForTimeout(350);
  await page.screenshot({path: path.join(SHOTS, name + ".png"), animations: "disabled"});
  await record(page, name, selectors);
  console.log("captured", name);
}

async function step(label, fn) {
  try { await fn(); } catch (error) { console.warn("step failed:", label, "-", error.message.split("\n")[0]); }
}

async function openThread(page, title) {
  await page.locator("#overview .session", {hasText: title}).first().click();
  await page.locator("#conversation:not([hidden])").waitFor();
  await page.waitForTimeout(600);
}

async function goHome(page) {
  await page.goto(BASE + "/", {waitUntil: "domcontentloaded"});
  await page.locator("#overview .thread-card").first().waitFor();
  await page.waitForTimeout(400);
}

async function main() {
  fs.mkdirSync(SHOTS, {recursive: true});
  const server = spawn("python3", [CLI, "--db", DB, "ui", "--no-browser", "--port", String(PORT)],
    {stdio: ["ignore", "pipe", "pipe"], env: {...process.env, AGENT_COORD_DB: DB}});
  server.stdout.on("data", d => process.stdout.write("[ui] " + d));
  server.stderr.on("data", d => process.stderr.write("[ui] " + d));
  try {
    await waitForPort(PORT);
    const browser = await chromium.launch();
    const desktop = await browser.newContext({viewport: {width: 1600, height: 1000}, deviceScaleFactor: 2, colorScheme: "light"});
    const page = await desktop.newPage();
    page.on("pageerror", error => console.warn("page error:", error.message));

    await goHome(page);
    const cardSelectors = {
      attention: "#overview .attention-card",
      attentionGroup: "#overview .thread-group:has(.attention-card), #overview section:has(.attention-card)",
      firstCard: "#overview .thread-card",
      blocked: "#overview .attention-card:has(.attention-reason.blocked)",
      review: "#overview .attention-card:has(.attention-reason.review)",
      findings: "#overview .attention-card:has(.attention-reason.findings)",
      update: "#overview .attention-card:has(.attention-reason.update)",
      done: "#overview .attention-card:has(.attention-reason.done)",
      working: "#overview .session.active-thread",
      sidebar: "#sidebar",
      viewTabs: "#view-tabs",
      tile: "#tile-threads",
      search: ".search-field",
      filters: "#filter-menu",
      placement: ".placement-switch",
      newSession: "#welcome-new",
      rollUp: "#roll-up-start",
      monitorLink: "#monitor",
      notifications: "#notifications",
      stageInvestigating: "#overview h2:has-text('Investigating')",
      stageImplementing: "#overview h2:has-text('Implementing')",
      stageDone: "#overview h2:has-text('Done')",
    };
    await shot(page, "overview-light", cardSelectors);

    await step("stages", async () => {
      await page.evaluate(() => { document.getElementById("welcome").scrollTop = 560; });
      await page.waitForTimeout(400);
      await shot(page, "overview-stages", cardSelectors);
      await page.evaluate(() => { document.getElementById("welcome").scrollTop = 0; });
      await page.waitForTimeout(300);
    });

    await step("dark overview", async () => {
      await page.selectOption("#sidebar [data-theme-select]", "dark");
      await page.waitForTimeout(300);
      await shot(page, "overview-dark", cardSelectors);
      await page.selectOption("#sidebar [data-theme-select]", "light");
      await page.waitForTimeout(300);
    });

    await step("search", async () => {
      await page.fill("#search", "address");
      await page.waitForTimeout(400);
      await shot(page, "overview-search", {search: ".search-field", firstCard: "#overview .thread-card"});
      await page.fill("#search", "");
      await page.waitForTimeout(300);
    });

    await step("filters", async () => {
      await page.evaluate(() => { document.getElementById("filter-menu").open = true; });
      await page.waitForTimeout(300);
      await shot(page, "overview-filters", {panel: ".filter-panel", filters: "#filter-menu"});
      await page.evaluate(() => { document.getElementById("filter-menu").open = false; });
    });

    await step("views", async () => {
      await page.locator("#view-tabs [role=tab]", {hasText: "Checkout redesign"}).click();
      await page.waitForTimeout(700);
      await shot(page, "view-checkout", {...cardSelectors, activeTab: "#view-tabs [aria-selected=true]"});
      await page.locator("#view-tabs [role=tab]", {hasText: "Infra"}).click();
      await page.waitForTimeout(700);
      await shot(page, "view-infra", {...cardSelectors, activeTab: "#view-tabs [aria-selected=true]"});
      await page.locator("#view-tabs [role=tab]", {hasText: "Needs me"}).click();
      await page.waitForTimeout(700);
      await shot(page, "view-needsme", {...cardSelectors, activeTab: "#view-tabs [aria-selected=true]"});
      await page.locator("#view-tabs [role=tab]", {hasText: "All work"}).click();
      await page.waitForTimeout(500);
    });

    await step("later and closed", async () => {
      await page.click("#show-later");
      await page.waitForTimeout(600);
      await shot(page, "overview-later", {placement: ".placement-switch", firstCard: "#overview .thread-card"});
      await page.click("#show-closed");
      await page.waitForTimeout(600);
      await shot(page, "overview-closed", {placement: ".placement-switch", firstCard: "#overview .thread-card"});
      await page.click("#show-now");
      await page.waitForTimeout(500);
    });

    await step("roll up", async () => {
      await page.click("#roll-up-start");
      await page.waitForTimeout(900);
      await shot(page, "rollup", {bar: "#roll-up-bar", handle: "#roll-up-handle", skip: "#roll-up-skip", context: "#context-panel"});
      await page.click("#roll-up-exit");
      await page.waitForTimeout(400);
    });

    await goHome(page);
    await step("thread cart", async () => {
      await openThread(page, "Cart totals rounding fix");
      await page.locator("#timeline .message").first().waitFor({timeout: 8000});
      await page.waitForTimeout(500);
      const threadSelectors = {
        title: "#title-heading", timeline: "#timeline", composer: "#composer", context: "#context-panel",
        checkpoint: "#thread-context", links: ".thread-links", actions: ".session-actions", statusbar: ".session-statusbar",
        firstUser: "#timeline .message.user", lastAgent: "#timeline .message.agent:last-of-type", tool: "#timeline details.tool",
        expand: "#expand-chat", fork: "#fork-thread", park: "#park", organize: "#organize-thread", closeThread: "#close-thread",
        model: "#model-trigger", send: "#send", queue: "#queue",
      };
      await shot(page, "thread-cart", threadSelectors);
      await page.evaluate(() => { const t = document.getElementById("timeline"); t.scrollTop = t.scrollHeight; });
      await page.waitForTimeout(400);
      await shot(page, "thread-cart-bottom", threadSelectors);
      await page.click("#expand-chat");
      await page.waitForTimeout(500);
      await shot(page, "thread-cart-expanded", threadSelectors);
      await page.keyboard.press("Escape");
      await page.waitForTimeout(400);
      await page.fill("#message", "/");
      await page.waitForTimeout(500);
      await shot(page, "thread-slash", {...threadSelectors, slash: "#slash-commands"});
      await page.fill("#message", "");
      await page.waitForTimeout(200);
      await page.fill("#message", "Looks good. Merge it once the receipt renderer tests pass, then deploy to preview.");
      await page.waitForTimeout(300);
      await shot(page, "thread-draft", threadSelectors);
      await page.fill("#message", "");
      await step("model menu", async () => {
        await page.click("#model-trigger", {timeout: 4000});
        await page.locator("#model-menu:not([hidden])").waitFor({timeout: 15000});
        await page.waitForTimeout(400);
        await shot(page, "thread-model-menu", {...threadSelectors, menu: "#model-menu"});
        await page.keyboard.press("Escape");
      });
      await step("checkpoint dialog", async () => {
        await page.click("#edit-checkpoint");
        await page.waitForTimeout(400);
        await shot(page, "dialog-checkpoint", {dialog: "#checkpoint-dialog"});
        await page.keyboard.press("Escape");
        await page.waitForTimeout(200);
      });
      await step("link dialog", async () => {
        await page.click("#add-link");
        await page.waitForTimeout(400);
        await shot(page, "dialog-link", {dialog: "#link-dialog"});
        await page.keyboard.press("Escape");
        await page.waitForTimeout(200);
      });
      await step("organization dialog", async () => {
        await page.click("#organize-thread");
        await page.waitForTimeout(500);
        await shot(page, "dialog-organization", {dialog: "#organization-dialog"});
        await page.keyboard.press("Escape");
        await page.waitForTimeout(200);
      });
    });

    await goHome(page);
    await step("thread working", async () => {
      await openThread(page, "Implement address autocomplete");
      await page.waitForTimeout(700);
      await shot(page, "thread-working", {title: "#title-heading", status: "#status", timeline: "#timeline", context: "#context-panel", stop: "#stop", queue: "#queue"});
    });

    await goHome(page);
    await step("thread search findings", async () => {
      await openThread(page, "Why is /api/search p95");
      await page.waitForTimeout(900);
      await shot(page, "thread-search", {title: "#title-heading", timeline: "#timeline", context: "#context-panel"});
    });

    await goHome(page);
    await step("tiled threads", async () => {
      await page.locator("#view-tabs [role=tab]", {hasText: "Checkout redesign"}).click();
      await page.waitForTimeout(600);
      await page.click("#tile-threads");
      await page.locator("#thread-panes:not([hidden])").waitFor({timeout: 10000});
      await page.waitForTimeout(2500);
      await shot(page, "tiled", {panes: "#thread-panes", pane: "#thread-panes .thread-pane", leave: "#leave-tiles"});
      await page.click("#leave-tiles");
      await page.waitForTimeout(400);
      await page.locator("#view-tabs [role=tab]", {hasText: "All work"}).click();
    });

    await step("new session draft", async () => {
      await goHome(page);
      await page.click("#welcome-new");
      await page.locator("#conversation:not([hidden])").waitFor({timeout: 8000});
      await page.waitForTimeout(600);
      await shot(page, "draft", {composer: "#composer", model: "#model-trigger", title: "#title-heading"});
      await step("draft model menu", async () => {
        await page.click("#model-trigger", {timeout: 4000});
        await page.locator("#model-menu:not([hidden])").waitFor({timeout: 20000});
        await page.waitForTimeout(400);
        await shot(page, "draft-model-menu", {composer: "#composer", menu: "#model-menu"});
        await page.keyboard.press("Escape");
      });
    });

    await step("monitor", async () => {
      await page.goto(BASE + "/monitor", {waitUntil: "domcontentloaded"});
      await page.waitForTimeout(1500);
      await shot(page, "monitor", {tree: ".tree, #tree", detail: "#detail, .detail"});
      const parent = page.locator(".node", {hasText: "Checkout redesign coordinator"}).first();
      if (await parent.count()) { await parent.click(); await page.waitForTimeout(600); }
      await shot(page, "monitor-parent", {tree: ".tree, #tree", detail: "#detail, .detail"});
      const child = page.locator(".node", {hasText: "autocomplete-validator"}).first();
      if (await child.count()) { await child.click(); await page.waitForTimeout(600); }
      await shot(page, "monitor-child", {tree: ".tree, #tree", detail: "#detail, .detail", output: "pre"});
      await page.selectOption(".monitor-header [data-theme-select]", "dark").catch(() => {});
      await page.waitForTimeout(300);
      await shot(page, "monitor-child-dark", {tree: ".tree, #tree", detail: "#detail, .detail", output: "pre"});
      await page.selectOption(".monitor-header [data-theme-select]", "light").catch(() => {});
    });

    await desktop.close();

    await step("mobile", async () => {
      const mobile = await browser.newContext({viewport: {width: 390, height: 844}, deviceScaleFactor: 3, isMobile: true, hasTouch: true, colorScheme: "light"});
      const phone = await mobile.newPage();
      await phone.goto(BASE + "/", {waitUntil: "domcontentloaded"});
      await phone.locator("#overview .thread-card").first().waitFor();
      await phone.waitForTimeout(600);
      await shot(phone, "mobile-overview", {firstCard: "#overview .thread-card"});
      await phone.locator("#overview .session", {hasText: "Cart totals rounding fix"}).first().click();
      await phone.locator("#conversation:not([hidden])").waitFor();
      await phone.waitForTimeout(900);
      await shot(phone, "mobile-thread", {composer: "#composer"});
      await mobile.close();
    });

    await step("dark thread", async () => {
      const dark = await browser.newContext({viewport: {width: 1600, height: 1000}, deviceScaleFactor: 2, colorScheme: "dark"});
      const page2 = await dark.newPage();
      await page2.goto(BASE + "/", {waitUntil: "domcontentloaded"});
      await page2.locator("#overview .thread-card").first().waitFor();
      await page2.selectOption("#sidebar [data-theme-select]", "dark");
      await page2.waitForTimeout(300);
      await openThread(page2, "Cart totals rounding fix");
      await page2.locator("#timeline .message").first().waitFor({timeout: 8000});
      await page2.waitForTimeout(500);
      await shot(page2, "thread-cart-dark", {title: "#title-heading", timeline: "#timeline", context: "#context-panel", composer: "#composer"});
      await dark.close();
    });

    await browser.close();
  } finally {
    fs.writeFileSync(path.join(SHOTS, "boxes.json"), JSON.stringify(boxes, null, 2));
    server.kill("SIGTERM");
  }
}

main().catch(error => { console.error(error); process.exit(1); });
