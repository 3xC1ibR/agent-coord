#!/usr/bin/env node
// Real browser startup regression, using the real HTTP/CSP and disposable fake providers.
// --web-root can select immutable assets directly from a signed application bundle.
// node scripts/validate_web_startup.mjs --python /path/to/python3 --output /tmp/startup.json
import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {mkdirSync, readFileSync, writeFileSync} from "node:fs";
import {createRequire} from "node:module";
import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {createHash} from "node:crypto";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const args = Object.fromEntries(process.argv.slice(2).reduce((pairs, value, index, all) => {
  if (value.startsWith("--")) pairs.push([value.slice(2), all[index + 1]]);
  return pairs;
}, []));
const webRoot = resolve(args["web-root"] || `${root}/plugins/agent-coord/scripts/agent_coord/web`);
const output = resolve(args.output || "/tmp/ribbon-startup.json");
const scenarios = (args.cases || "normal,transient,persistent,late-failure,delayed-app").split(",");
const repetitions = Number(args.repeat || 1);
assert.ok(Number.isInteger(repetitions) && repetitions > 0 && repetitions <= 20);
assert.ok(scenarios.every(name => ["normal", "transient", "persistent", "late-failure", "delayed-app"].includes(name)));
assert.ok(!args.engine || ["chromium", "webkit"].includes(args.engine));
mkdirSync(dirname(output), {recursive: true});
const {chromium, webkit} = createRequire(`${root}/demo/package.json`)("playwright");
const markup = readFileSync(`${webRoot}/index.html`, "utf8");
const report = {webRoot, engine: args.engine || "chromium", at: new Date().toISOString(),
  indexSha256: createHash("sha256").update(markup).digest("hex"), cases: []};
const scripts = [...markup.matchAll(/(?:src|href)="\/([\w/-]+\.js)"/g)].map(match => match[1]);
const env = {...process.env};
for (const key of Object.keys(env)) {
  if (/^(AGENT_COORD_|CODEX_|CLAUDE_CODE_|PYTHONPATH$)/.test(key)) delete env[key];
}
const fixture = spawn(args.python || "python3", ["-B", "-c", `
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1] + '/tests')
import asks_fixture_server as fixture
import agent_coord.ui as ui
import test_codex_app_server as providers
from agent_coord.jev_settings import JevSettings
ui._WEB_ROOT = Path(sys.argv[2])
class CompletingCodex(providers.FakeCodex):
    def __init__(self, *args):
        super().__init__(*args)
        self.fast_turn = True
providers.FakeCodex = CompletingCodex
fixture.seed = lambda *args: None
def seed_conversations(store, sessions, clock, directory):
    JevSettings(store.database_path).configure({'action': 'dismiss'})
    thread = sessions.create({'name': 'Startup recovery verification', 'cwd': str(directory)})['session']['thread_id']
    sessions.send(thread, {'message': 'Retain this conversation while startup recovers.'})
    return thread
fixture.seed_conversations = seed_conversations
fixture.PERMITTED_POSTS += ('/api/browser/threads/', '/api/browser/activity/events', '/api/browser/desktop-projects')
fixture.PERMITTED_MESSAGES += ('/prewarm',)
sys.argv = ['startup-fixture', '--port', '0']
fixture.main()
`, root, webRoot], {cwd: root, env, stdio: ["ignore", "pipe", "pipe"]});
let fixtureLog = "";
fixture.stderr.on("data", data => { fixtureLog += data; });
let browser;
try {
  const {base, thread} = await new Promise((resolveReady, reject) => {
    let text = "";
    const timer = setTimeout(() => reject(new Error(`Fixture startup timed out: ${fixtureLog}`)), 30000);
    fixture.once("error", reject);
    fixture.once("exit", code => { clearTimeout(timer); reject(new Error(`Fixture exited ${code}: ${fixtureLog}`)); });
    fixture.stdout.on("data", data => {
      text += data;
      const base = text.match(/Asks fixture: (http:\/\/127\.0\.0\.1:\d+)/)?.[1];
      const thread = text.match(/Browser conversation: (\S+)/)?.[1];
      if (base && thread) { clearTimeout(timer); resolveReady({base, thread}); }
    });
  });
  browser = await (report.engine === "webkit" ? webkit : chromium).launch({headless: true,
    ...(args.executable ? {executablePath: args.executable} : {})});
  for (const scenario of Array.from({length: repetitions}, () => scenarios).flat()) {
    const context = await browser.newContext({viewport: {width: 1440, height: 900}});
    const page = await context.newPage();
    const result = {scenario, requests: {}, failures: [], errors: []};
    report.cases.push(result);
    page.setDefaultTimeout(6000);
    page.on("pageerror", error => result.errors.push(String(error)));
    await page.addInitScript(() => {
      window.__startupProbe = {config: 0, streams: 0, loaded: [], timeOrigin: performance.timeOrigin};
      document.addEventListener("load", event => {
        if (event.target.tagName === "SCRIPT" && event.target.src) {
          window.__startupProbe.loaded.push(new URL(event.target.src).pathname.slice(1));
        }
      }, true);
      const fetchOriginal = window.fetch;
      window.fetch = function(input, ...rest) {
        if (String(input).split("?")[0].endsWith("/api/browser/config")) window.__startupProbe.config++;
        return fetchOriginal.call(this, input, ...rest);
      };
      const EventSourceOriginal = window.EventSource;
      window.EventSource = class extends EventSourceOriginal {
        constructor(...values) { super(...values); window.__startupProbe.streams++; }
      };
    });
    const transient = new Set(["theme.js", "dist/app.js"]);
    let releaseApp;
    const appResponse = new Promise(done => { releaseApp = done; });
    await page.route("**/*", async route => {
      const url = new URL(route.request().url());
      if (url.origin !== base) return route.abort();
      const asset = url.pathname.slice(1);
      if (!asset.endsWith(".js")) return route.continue();
      const count = result.requests[asset] = (result.requests[asset] || 0) + 1;
      const fail = scenario === "transient" && transient.has(asset) && count === 1 ||
        scenario === "persistent" && asset === "dist/app.js" ||
        scenario === "late-failure" && asset === "dist/app.js" && count === 1;
      if (scenario === "delayed-app" && asset === "dist/app.js" && count === 1) {
        await appResponse;
        return route.continue();
      }
      if (fail) { result.failures.push(asset); return route.abort("internetdisconnected"); }
      return route.continue();
    });
    try {
      await page.goto(`${base}/#${thread}`, {waitUntil: "commit"});
      await page.waitForSelector("#message", {state: "attached"});
      const timeOrigin = await page.evaluate(() => performance.timeOrigin);
      if (scenario === "transient") {
        await page.evaluate(() => { location.hash = "startup-recovery-pending"; });
        await page.evaluate(id => { location.hash = id; }, thread);
      }
      if (scenario === "delayed-app") {
        await page.waitForFunction(() => !document.querySelector("#error").hidden &&
          document.querySelector("#error").textContent.includes("Could not load Ribbon Field"), null, {timeout: 5000});
        await page.waitForTimeout(100);
        result.beforeLateResponse = await page.evaluate(() => ({at: performance.now(),
          status: document.documentElement.dataset.startup, appExecuted: window.__startupProbe.loaded.includes("dist/app.js")}));
        assert.equal(result.beforeLateResponse.appExecuted, false, "hold the actual app bundle beyond the deadline");
        result.releaseMs = await page.evaluate(() => performance.now());
        releaseApp();
      }
      if (scenario === "persistent") {
        await page.waitForTimeout(4500);
        result.initialization = await page.evaluate(() => window.__startupProbe);
        result.notice = await page.locator("#error").innerText();
        assert.ok(!result.initialization.loaded.includes("dist/app.js"), "do not initialize a missing bundle");
        assert.equal(result.errors.length, 0, "do not execute broken bundle scripts");
        assert.ok(await page.locator("#error").isVisible(), "persistent failure must be visible");
        const attempts = result.requests["dist/app.js"];
        assert.ok(attempts >= 2 && attempts <= 8, `bounded attempts: ${attempts}`);
        await page.evaluate(() => { location.hash = "startup-still-failed"; });
        await page.waitForTimeout(1000);
        assert.equal(result.requests["dist/app.js"], attempts, "hash navigation must not restart retries");
        result.screenshot = output.replace(/\.json$/, "") + `-${scenario}-${report.cases.length}.png`;
        await page.screenshot({path: result.screenshot});
      } else {
        await page.waitForFunction(() => typeof state !== "undefined" && state.dispatchReady &&
          state.selected && !document.querySelector("#conversation").hidden &&
          !document.querySelector("#message").disabled && !document.querySelector("#send").disabled,
        null, {timeout: 5000, polling: 20});
        result.readyMs = await page.evaluate(() => performance.now());
        if (scenario !== "delayed-app") assert.ok(result.readyMs <= 5000, `startup took ${result.readyMs}ms`);
        if (scenario === "transient") assert.deepEqual(new Set(result.failures), transient);
        const draft = `Startup retained draft: ${scenario}`;
        await page.locator("#message").fill(draft);
        await page.waitForFunction(() => typeof threadHover !== "undefined", null,
          {timeout: scenario === "delayed-app" ? 1000 : 5000});
        result.recoveredMs = await page.evaluate(() => performance.now());
        if (scenario !== "delayed-app") assert.ok(result.recoveredMs <= 5000, `asset recovery took ${result.recoveredMs}ms`);
        if (scenario === "delayed-app") {
          assert.ok(result.recoveredMs - result.releaseMs <= 1000, "finish promptly after late success");
          assert.equal(result.requests["dist/app.js"], 1, "never retry an uncertain bundle execution");
          assert.equal(await page.locator("#error").isVisible(), false, "clear the resolved startup notice");
          await page.evaluate(() => { document.querySelector("#filter-menu").open = true; });
          await page.keyboard.press("Escape");
          assert.equal(await page.locator("#filter-menu").evaluate(menu => menu.open), false);
          result.lateCompletionFunctional = true;
        }
        await page.waitForTimeout(400);
        assert.equal(await page.locator("#message").inputValue(), draft, "recovery must retain a draft");
        assert.equal(await page.evaluate(() => performance.timeOrigin), timeOrigin, "recovery must retain the document");
        result.initialization = await page.evaluate(() => window.__startupProbe);
        assert.equal(result.initialization.loaded.filter(asset => asset === "dist/app.js").length, 1, "initialize once");
        assert.deepEqual(result.initialization.loaded, scripts, "execute theme.js then dist/app.js once");
        assert.equal(result.initialization.streams, 1, "connect once");
        assert.deepEqual(result.errors, []);
        result.screenshot = output.replace(/\.json$/, "") + `-${scenario}-${report.cases.length}.png`;
        await page.screenshot({path: result.screenshot});
        const accepted = page.waitForResponse(response => response.url().endsWith(`/sessions/${thread}/messages`) &&
          response.request().method() === "POST");
        await page.locator("#message").press("Enter");
        assert.equal((await accepted).status(), 200);
        await page.waitForFunction(text => [...document.querySelectorAll("#timeline article.message.user")]
          .some(node => node.textContent.includes(text)), draft, {timeout: 5000});
        result.draftPreserved = true;
        result.submissionAccepted = true;
      }
      result.passed = true;
    } catch (error) {
      result.passed = false;
      result.failure = String(error);
      result.initialization = await page.evaluate(() => window.__startupProbe).catch(() => null);
      result.screenshot = output.replace(/\.json$/, "") + `-${scenario}-failed.png`;
      await page.screenshot({path: result.screenshot}).catch(() => {});
    } finally {
      releaseApp();
      await context.close();
    }
    console.log(JSON.stringify({scenario, passed: result.passed, readyMs: result.readyMs, failure: result.failure}));
  }
} catch (error) {
  report.failure = String(error);
  console.error(report.failure);
} finally {
  await browser?.close();
  fixture.kill("SIGINT");
  await new Promise(done => { if (fixture.exitCode !== null) done(); else fixture.once("exit", done); });
  report.passed = !report.failure && report.cases.length === scenarios.length * repetitions && report.cases.every(item => item.passed);
  writeFileSync(output, JSON.stringify(report, null, 2) + "\n");
}
if (!report.passed) process.exitCode = 1;
