// Run against a disposable HTTP fixture. Requires the demo's Playwright installation.
// node scripts/validate_interface.mjs --output /absolute/evidence --source /path/to/checkout
// Add --web-root /path/to/web to serve separately bundled assets, and --check for a release gate.
import assert from "node:assert/strict";
import {execFileSync, spawn} from "node:child_process";
import {createHash} from "node:crypto";
import {once} from "node:events";
import {readFile, mkdir, mkdtemp, rm, writeFile} from "node:fs/promises";
import {createRequire} from "node:module";
import {dirname, join, resolve} from "node:path";
import {tmpdir} from "node:os";
import {createInterface} from "node:readline";
import {fileURLToPath} from "node:url";
import {setTimeout as delay} from "node:timers/promises";
import {parseArgs} from "node:util";
import {gzipSync} from "node:zlib";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const {values: options} = parseArgs({options: {
  output: {type: "string"},
  source: {type: "string", default: root},
  "web-root": {type: "string"},
  playwright: {type: "string"},
  python: {type: "string", default: "python3"},
  executable: {type: "string"},
  engine: {type: "string", default: "chromium"},
  mode: {type: "string", default: "all"},
  states: {type: "string"},
  surfaces: {type: "string"},
  viewport: {type: "string"},
  checks: {type: "string"},
  switches: {type: "string", default: "120"},
  sends: {type: "string", default: "20"},
  "idle-seconds": {type: "string", default: "60"},
  "boot-trace": {type: "boolean"},
  check: {type: "boolean"},
}});
assert(options.output && ["all", "capture", "interactions", "performance"].includes(options.mode),
  "Specify --output and a valid --mode (all, capture, interactions, performance).");
assert(!options.check || !options.checks, "The acceptance gate requires all scenarios for its selected mode.");
const output = resolve(options.output);
const configuration = JSON.parse(await readFile(join(root, "tests/interface_capture.json"), "utf8"));
const require = createRequire(join(root, "demo/package.json"));
const playwright = require(options.playwright || "playwright");
const browserType = playwright[options.engine];
assert(browserType, "Select an installed Playwright browser.");
const observers = new Set();
const signals = new AbortController();
let activeBrowser;

async function stopFixture(child, exited) {
  if (child.exitCode === null && child.signalCode === null && child.pid) {
    child.kill("SIGTERM");
    const timeout = AbortSignal.timeout(5000);
    await Promise.race([exited, once(timeout, "abort")]);
    if (child.exitCode === null && child.signalCode === null) {
      child.kill("SIGKILL");
      await exited;
    }
  }
}

async function startFixture(state, {scope = "root", directory, resume = false, port} = {}) {
  const args = ["-B", "-W", "error::ResourceWarning", join(root, "tests/interface_fixture_server.py"),
    "--state", state, "--scope", scope, "--source", resolve(options.source)];
  if (options["web-root"]) args.push("--web-root", resolve(options["web-root"]));
  if (directory) args.push("--state-directory", directory);
  if (resume) args.push("--resume");
  if (port) args.push("--port", port);
  const child = spawn(options.python, args, {stdio: ["ignore", "pipe", "pipe"]});
  const exited = new Promise(resolve => child.once("close", (code, signal) => resolve({code, signal})));
  observers.add(child);
  let serverLog = "";
  child.stderr.on("data", chunk => { serverLog += chunk.toString(); });
  const lines = createInterface({input: child.stdout});
  const cleanup = async () => {
    lines.close();
    await stopFixture(child, exited);
    observers.delete(child);
    await writeFile(join(output, `fixture-${state}-${scope}${resume ? "-resumed" : ""}-${child.pid}.log`), serverLog);
  };
  try {
    const [line] = await Promise.race([
      once(lines, "line", {signal: AbortSignal.any([signals.signal, AbortSignal.timeout(30000)])}),
      once(child, "error").then(([error]) => { throw error; }),
      exited.then(status => { throw new Error(`Fixture exited before readiness: ${JSON.stringify(status)}\n${serverLog}`); }),
    ]);
    return {fixture: JSON.parse(line), cleanup};
  } catch (error) {
    await cleanup();
    throw error;
  }
}

async function withFixture(state, run, scope = "root") {
  const {fixture, cleanup} = await startFixture(state, {scope});
  try { return await run(fixture); }
  finally { await cleanup(); }
}

function address(fixture, directory, {thread, dispatcher} = {}) {
  const url = new URL(fixture.base);
  url.searchParams.set("directory", directory);
  if (thread) url.hash = thread.id;
  if (dispatcher) url.searchParams.set("dispatcher", dispatcher.id);
  return url.href;
}

async function shown(page, selector) {
  await page.locator(selector).first().waitFor({state: "visible"});
}

async function browserOpen(page, fixture, directory = fixture.workspace, destination) {
  await page.goto(address(fixture, directory, destination), {waitUntil: "domcontentloaded"});
}

async function expectContains(locator, text) {
  await locator.filter({hasText: text}).first().waitFor({state: "visible"});
}

async function selectTab(page, thread) {
  await page.locator(`#conversation-tabs [data-thread-tab="${thread.id}"]`).click();
}

async function openConversation(page, fixture, thread, directory = fixture.workspace) {
  await browserOpen(page, fixture, directory, {thread});
  await readyConversation(page, thread);
}

async function readyConversation(page, thread) {
  await expectContains(page.locator("#title-heading"), thread.title);
  if (thread.latest) await expectContains(page.locator("#timeline article.message"), thread.latest);
  else assert.equal(await page.locator("#timeline article.message").count(), 0);
  await page.waitForFunction(() => {
    const input = document.querySelector("#message");
    return input?.getClientRects().length && !input.disabled &&
      !/opening conversation/i.test(input.placeholder);
  });
}

function rememberOverview(value) {
  if (window.top === window) localStorage.setItem("agent-coord.overview-layout", value);
}

async function openQueue(page) {
  const link = page.locator("#open-asks-view");
  if (!await link.isVisible()) await page.locator("#menu-toggle").click();
  await link.click();
  await shown(page, "#asks-dialog[open] #asks-list");
  await page.waitForFunction(() => {
    const region = document.querySelector("#asks-list");
    return region && (region.children.length || !document.querySelector("#asks-error")?.hidden);
  });
}

async function openTiling(page, fixture, directory, count) {
  await browserOpen(page, fixture, directory);
  await waitOverview(page, count);
  const button = page.locator("#tile-threads");
  if (await button.isVisible()) await button.click();
  else await page.keyboard.press("Control+Alt+Meta+T");
  await shown(page, "#thread-panes");
  await page.waitForFunction(count => document.querySelectorAll(
    "#thread-panes .conversation-pane-frame").length >= count, count);
}

async function waitOverview(page, minimum = 0) {
  await page.waitForFunction(minimum => {
    const count = document.querySelector("#results-count")?.textContent || "";
    const parsed = count.match(/\d+/)?.[0];
    return parsed !== undefined && Number(parsed) >= minimum;
  }, minimum);
}

async function captureSurface(page, fixture, surface, mobile) {
  let expected = surface;
  const {state, threads} = fixture;
  switch (surface) {
    case "list":
    case "focus":
    case "atlas": {
      await browserOpen(page, fixture);
      await waitOverview(page, state === "empty" ? 0 : 1);
      assert.equal(await workspaceSelection(page), fixture.workspace);
      if (state === "empty") assert.match(await page.locator("#results-count").innerText(), /^0 threads/);
      else if (surface === "atlas" && !mobile) await shown(page, "#atlas-overview [data-domain]");
      if (surface === "atlas" && mobile) {
        expected = await page.locator("#thread-list-overview").isVisible() ? "list-fallback" : "legacy-fallback";
      } else {
        const selectors = {list: "#thread-list-overview", focus: "#working-set-overview", atlas: "#atlas-overview"};
        await shown(page, selectors[surface]);
      }
      if (surface === "list" && state !== "empty") {
        for (const key of ["supervisor", "lead"]) {
          const expand = page.locator(`#thread-list-overview [data-thread="${threads[key].id}"][data-action="agents"]`);
          await expand.click();
        }
        await shown(page, `#thread-list-overview [data-thread="${threads.specialist.id}"][data-action="open"]`);
      }
      break;
    }
    case "dispatcher":
      await browserOpen(page, fixture, state === "empty" ? fixture.vacant : fixture.workspace,
        {dispatcher: threads.dispatcher});
      if (mobile) {
        await shown(page, "body.dispatcher-workspace #dispatcher-empty");
        await page.locator("#dispatcher-mobile-conversation").click();
        await shown(page, "#dispatcher-body #message");
        expected = "dispatcher-mobile-chat";
      } else await shown(page, "#dispatcher-workspace");
      break;
    case "conversation":
      await openConversation(page, fixture,
        threads[state === "empty" ? "empty-conversation" : state === "long-error" ? "failed" : "supervisor"],
        state === "empty" ? fixture.vacant : fixture.workspace);
      if (state === "streaming") {
        await expectContains(page.locator("#timeline article.message"), "Streaming inspection update");
      }
      if (state === "default") {
        await shown(page, "#timeline pre");
        const interactive = page.frameLocator('#timeline iframe[title="Interactive agent response"]');
        await interactive.getByText("Summary", {exact: true}).click();
        await expectContains(interactive.locator("body"), "Three assemblies passed inspection.");
        await shown(page, `#thread-asks .ask-row[data-id="${fixture.asks.conversation}"]`);
      }
      if (state === "long-error") {
        await expectContains(page.locator("#timeline"), "Cutoff length exceeds the stated inspection limit.");
      }
      break;
    case "tiled":
      await openTiling(page, fixture, fixture.pair, mobile ? 1 : 2);
      if (mobile) {
        const visiblePanes = await page.locator("#thread-panes .conversation-pane-frame:visible").count();
        if (visiblePanes === 1) expected = "single-visible-slot";
      }
      for (const key of ["pair-0", "pair-1"]) {
        const thread = threads[key];
        if (state === "empty") continue;
        const iframe = page.locator(`#thread-panes iframe[src*="#${thread.id}"]`);
        if (await iframe.isVisible()) {
          await expectContains(iframe.contentFrame().locator("#timeline article.message"), thread.latest);
        } else {
          const pane = page.locator(`#thread-panes section.conversation-pane-frame[data-thread-id="${thread.id}"]`);
          if (mobile && !await pane.isVisible()) continue;
          await expectContains(pane.locator("[data-pane-timeline] article.message"), thread.latest);
        }
      }
      break;
    case "queue":
      await browserOpen(page, fixture);
      await waitOverview(page);
      await openQueue(page);
      if (state === "long-error") {
        await page.route(/\/api\/browser\/asks\/\d+$/, route => route.continue({headers: {
          ...route.request().headers(), "X-Interface-Fixture-Ask-Error": "1",
        }}));
        const row = page.locator("#asks-list .ask-row").filter({hasText: "Which signed tolerance correction"});
        await row.locator("input.ask-note").fill("Retain the signed inspection limit.");
        await row.getByRole("button", {name: "Hold for drawing revision", exact: true}).click();
        await expectContains(row, "Inspection queue temporarily unavailable.");
        expected = "queue-long-text-and-error";
      }
      break;
    default: throw new Error(`Unknown capture surface: ${surface}`);
  }
  return expected;
}

async function captureMatrix(browser, report) {
  const selected = options.states ? options.states.split(",") : configuration.states;
  const surfaces = options.surfaces ? options.surfaces.split(",") : configuration.surfaces;
  const viewports = Object.entries(configuration.viewports).filter(([name]) => !options.viewport || name === options.viewport);
  assert(viewports.length > 0, "Unknown capture viewport.");
  assert(surfaces.every(surface => configuration.surfaces.includes(surface)), "Unknown capture surface.");
  if (options.check) assert(!options.states && !options.surfaces && !options.viewport, "The capture gate requires the complete matrix.");
  for (const state of selected) {
    assert(configuration.states.includes(state), `Unknown capture state: ${state}`);
    await withFixture(state, async fixture => {
      for (const [viewport, [width, height]] of viewports) {
        for (const colorScheme of configuration.color_schemes) {
          for (const surface of surfaces) {
            signals.signal.throwIfAborted();
            const page = await browser.newPage({viewport: {width, height}, colorScheme,
              reducedMotion: "reduce", hasTouch: viewport === "mobile"});
            page.setDefaultTimeout(3300);
            const name = `${surface}-${state}--${viewport}--${colorScheme}.png`;
            const record = {surface, state, viewport, colorScheme, file: name, status: "capture-failed"};
            report.captures.push(record);
            page.on("pageerror", error => (record.pageErrors ||= []).push(error.message));
            try {
              await page.context().clock.setFixedTime(new Date(fixture.anchor * 1000));
              const preference = {list: "list", atlas: "atlas", focus: "working-set"}[surface] || "list";
              await page.addInitScript(rememberOverview, preference);
              record.rendered = await captureSurface(page, fixture, surface, viewport === "mobile");
              await page.evaluate(async () => {
                await document.fonts.ready;
                await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
              });
              record.theme = await page.locator("html").getAttribute("data-theme");
              assert.equal(record.theme, colorScheme);
              await page.screenshot({path: join(output, name), animations: "disabled", caret: "hide"});
              record.status = "captured";
            } catch (error) {
              record.error = String(error.message);
              record.diagnostic = name.replace(".png", "--diagnostic.png");
              const css = ["#thread-list-overview", "#working-set-overview", "#atlas-overview", "#dispatcher-workspace",
                "#conversation", "#thread-panes", "#asks-dialog", "#results-count", "#error"];
              record.visible = await page.evaluate(selectors => Object.fromEntries(selectors.map(selector => {
                const node = document.querySelector(selector);
                return [selector, node?.getClientRects().length ?
                  (selector === "#results-count" || selector === "#error" ? node.textContent : true) : false];
              })), css).catch(() => ({}));
              await page.screenshot({path: join(output, record.diagnostic), animations: "disabled", caret: "hide"}).catch(() => {});
              console.error(`${name}: ${record.error.split("\n")[0]}`);
            } finally {
              await page.context().close();
            }
          }
        }
      }
      const expected = viewports.length * configuration.color_schemes.length * surfaces.length;
      console.log(`Captured ${state}: ${report.captures.filter(result => result.state === state && result.status === "captured").length}/${expected}`);
    });
  }
}


async function withPage(browser, run, properties = {}) {
  const page = await browser.newPage({viewport: {width: 1440, height: 900}, ...properties});
  page.setDefaultTimeout(4500);
  try { return await run(page); }
  finally { await page.context().close(); }
}

async function recordCheck(report, name, run) {
  if (options.checks && !name.toLowerCase().includes(options.checks.toLowerCase())) return;
  const result = {name};
  report.checks.push(result);
  try {
    result.evidence = await run();
    result.status = "passed";
  } catch (error) {
    result.status = "failed";
    result.error = error.stack;
    console.error(`${name}: ${error.message.split("\n")[0]}`);
  }
}

async function getBrowserAPI(fixture, route, options = {}) {
  const response = await fetch(`${fixture.base}/api/browser/${route}`, options);
  return {status: response.status, body: await response.json()};
}

async function openFromList(page, fixture, thread, {expand = []} = {}) {
  const overview = page.locator('#conversation-tabs [data-thread-tab=""]');
  if (await overview.isVisible()) await overview.click();
  else await browserOpen(page, fixture);
  await shown(page, "#thread-list-overview");
  for (const ancestor of expand) {
    const button = page.locator(`#thread-list-overview [data-thread="${ancestor.id}"][data-action="agents"]`);
    if ((await button.getAttribute("aria-expanded")) !== "true") await button.click();
  }
  const row = page.locator(`#thread-list-overview [data-thread="${thread.id}"][data-action="open"]`);
  await row.focus();
  await row.press("Enter");
  await readyConversation(page, thread);
}

async function workspaceSelection(page) {
  return (await page.locator('#view-tabs [aria-selected="true"]').getAttribute("data-project-cwd"));
}

async function compose(page, text, chord = "Enter") {
  await page.locator("#message").fill(text);
  await page.locator("#message").press(chord);
}

function isSubmission(request) {
  return request.method() === "POST" && /\/api\/browser\/sessions\/[^/]+\/(?:messages|queue)$/.test(new URL(request.url()).pathname);
}

function sentText(request) {
  return request.postDataJSON()?.message;
}

async function askFlow(page, fixture, surface, modifier) {
  const queue = surface === "Queue";
  const thread = fixture.threads[queue ? "other" : "supervisor"];
  if (queue) {
    await browserOpen(page, fixture);
    await openQueue(page);
  } else await openConversation(page, fixture, thread);
  const id = fixture.asks[queue ? "queue" : "conversation"];
  const row = page.locator(`${queue ? "#asks-list" : "#thread-asks"} .ask-row[data-id="${id}"]`);
  const input = row.locator("input.ask-note");
  const text = `${surface} ${modifier || "Enter"} acceptance answer.`;
  const requests = [];
  page.on("request", request => {
    if (request.method() !== "POST") return;
    const path = new URL(request.url()).pathname;
    if (isSubmission(request) || path.startsWith(`/api/browser/asks/${id}`)) {
      requests.push({path, body: request.postDataJSON()});
    }
  });
  await input.fill(text);
  await input.press(modifier ? modifier + "+Enter" : "Enter");
  if (modifier) {
    await expectContains(row, text);
    assert(requests.some(request => request.path.endsWith("/messages") &&
      request.path.includes(`/${thread.id}/`) && request.body.message.includes(text)));
    assert(!requests.some(request => request.path.endsWith(`/asks/${id}`)), "Discuss resolved the Ask.");
    const {body} = await getBrowserAPI(fixture, "asks?status=open");
    assert(body.data.some(ask => ask.id === id), "Discuss removed the Ask from the open list.");
  } else {
    await row.waitFor({state: "hidden"});
    const {body} = await getBrowserAPI(fixture, "asks?status=resolved");
    const resolved = body.data.find(ask => ask.id === id);
    assert.equal(resolved?.resolution, "answered");
    assert.equal(resolved?.answer, text);
    assert(requests.some(request => request.path.endsWith(`/asks/${id}`) &&
      request.body.resolution === "answered" && request.body.answer === text));
  }
  return {id, requests};
}

async function mobileLayout(page, fixture) {
  const thread = fixture.threads.failed;
  await openConversation(page, fixture, thread);
  await shown(page, "#mobile-chat-actions");
  const view = await page.evaluate(() => {
    const rect = selector => {
      const element = document.querySelector(selector);
      if (!element) return {width: 0, height: 0, display: "absent"};
      const {x, y, width, height} = element.getBoundingClientRect();
      return {x, y, width, height, display: getComputedStyle(element).display};
    };
    const input = document.getElementById("message");
    const send = document.getElementById("send");
    const actions = document.querySelector(".session-actions");
    const previous = actions?.parentElement;
    const next = actions?.nextSibling;
    const wasCompact = document.body.classList.contains("mobile-chat");
    let secondaryWithClassDelayed = "absent";
    if (actions) {
      // Reproduce the short interval before the mobile controller moves the
      // action group. The screen assertions below use the original live DOM.
      document.querySelector(".conversation-controls").append(actions);
      document.body.classList.remove("mobile-chat");
      secondaryWithClassDelayed = getComputedStyle(actions).display;
      previous.insertBefore(actions, next);
      document.body.classList.toggle("mobile-chat", wasCompact);
    }
    return {title: rect("#title-heading"), options: rect("#mobile-chat-actions"),
      secondary: rect(".conversation-head .session-actions"), dock: rect("#dispatcher-dock"),
      secondaryWithClassDelayed,
      input: rect("#message"), send: rect("#send"), attach: rect("#attach-image"),
      placeholder: input.placeholder, font: getComputedStyle(input).fontSize,
      titleOverflow: getComputedStyle(document.getElementById("title-heading")).textOverflow,
      titleButtonOverflow: getComputedStyle(document.getElementById("session-name")).textOverflow,
      horizontalOverflow: document.documentElement.scrollWidth - innerWidth, sendVisible: !!send.getClientRects().length};
  });
  assert(view.title.width >= 72 && view.title.x + view.title.width <= view.options.x + 1, JSON.stringify(view));
  assert([view.titleOverflow, view.titleButtonOverflow].includes("ellipsis"), JSON.stringify(view));
  assert(["none", "absent"].includes(view.secondary.display), JSON.stringify(view));
  assert(["none", "absent"].includes(view.secondaryWithClassDelayed), JSON.stringify(view));
  assert(view.dock.height === 0 && view.sendVisible && view.horizontalOverflow <= 0, JSON.stringify(view));
  assert.equal(view.font, "16px", JSON.stringify(view));
  assert.match(view.placeholder, /^Message agent[.…]*$/, JSON.stringify(view));
  for (const key of ["options", "send", "attach"]) {
    assert(view[key].width >= 44 && view[key].height >= 44, JSON.stringify(view));
  }
  return view;
}

async function familyNavigation(page, fixture) {
  const {supervisor, lead, specialist, sibling} = fixture.threads;
  await browserOpen(page, fixture);
  await openFromList(page, fixture, supervisor);
  const tab = page.locator(`#conversation-tabs [data-thread-tab="${supervisor.id}"]`);
  const title = await tab.innerText();
  const workspace = await workspaceSelection(page);
  assert.equal(workspace, fixture.workspace);
  assert.equal(title, supervisor.title);
  if (await page.locator("#context-panel").isVisible()) await page.locator("#toggle-context").click();
  assert(!await page.locator("#context-panel").isVisible());
  await openFromList(page, fixture, specialist, {expand: [supervisor, lead]});
  const breadcrumb = page.locator(".conversation-head #agent-breadcrumb");
  await breadcrumb.waitFor({state: "visible"});
  const steps = breadcrumb.locator(".agent-breadcrumb-step");
  assert.deepEqual(await steps.evaluateAll(elements => elements.map(el => el.dataset.agentThread)),
    [supervisor.id, lead.id, specialist.id]);
  assert.equal(await breadcrumb.locator('[aria-current="page"]').getAttribute("data-agent-thread"), specialist.id);
  assert.equal(await tab.innerText(), title);
  assert.equal(await tab.getAttribute("aria-selected"), "true");
  assert.equal(await workspaceSelection(page), workspace);
  assert(!await page.locator("#context-panel").isVisible(), "Opening a child displayed the context drawer.");
  await breadcrumb.locator(".agent-breadcrumb-switcher > summary").click();
  await breadcrumb.locator(`.agent-breadcrumb-option[data-agent-thread="${sibling.id}"]`).click();
  await readyConversation(page, sibling);
  assert.equal(await tab.innerText(), title);
  await breadcrumb.locator(`.agent-breadcrumb-step[data-agent-thread="${supervisor.id}"]`).click();
  await readyConversation(page, supervisor);
  return {root: supervisor.id, children: [lead.id, specialist.id, sibling.id]};
}

async function crossRepository(page, fixture) {
  const {supervisor, remote, other} = fixture.threads;
  await openConversation(page, fixture, supervisor);
  await openFromList(page, fixture, other);
  await selectTab(page, supervisor);
  await readyConversation(page, supervisor);
  const workspace = await workspaceSelection(page);
  const visibleTabs = '#conversation-tabs [data-thread-tab]:visible';
  const original = await page.locator(visibleTabs).evaluateAll(tabs => tabs.map(tab => tab.dataset.threadTab));
  const link = page.locator(`#timeline a[href^="agentcoord:"]`).filter({hasText: remote.title});
  await link.click();
  await readyConversation(page, remote);
  assert.equal(await workspaceSelection(page), workspace);
  const current = await page.locator(visibleTabs).evaluateAll(tabs => tabs.map(tab => tab.dataset.threadTab));
  assert(original.every(id => current.includes(id)));
  const {body} = await getBrowserAPI(fixture, "threads/" + remote.id);
  assert.equal(body.project_id, fixture.project);
  assert.equal(body.cwd, remote.cwd);
  return {original, current, outsideRepository: remote.id, workspace};
}

async function attentionReceipts(page, fixture, key, clear, expectedResponse) {
  const thread = fixture.threads[key];
  const {body: before} = await getBrowserAPI(fixture, "threads/" + thread.id);
  assert.equal(before.needs_attention, true, JSON.stringify(before));
  if (expectedResponse) assert.equal(before.response_state, expectedResponse);
  const read = (before.unread_result || clear) && page.waitForResponse(response => response.request().method() === "POST" &&
    new URL(response.url()).pathname === `/api/browser/threads/${thread.id}` &&
    response.request().postDataJSON()?.seen === true);
  if (read) void read.catch(() => {});
  await openConversation(page, fixture, thread);
  if (read) assert((await read).ok());
  const {body: after} = await getBrowserAPI(fixture, "threads/" + thread.id);
  assert.equal(after.needs_attention, !clear, JSON.stringify({
    response: after.response_state, attention: after.attention_reason, handled: after.handled_completion_id,
    seen: after.seen_completion_id, completion: after.turn_completion,
  }));
  if (clear && after.turn_completion) {
    assert(after.handled_completion_id >= after.turn_completion.id);
  }
  return {before: before.response_state, after: after.response_state, needsAttention: after.needs_attention};
}

async function attentionOrder(page, fixture) {
  await browserOpen(page, fixture);
  const section = page.locator('#thread-list-overview [data-section="attention"]');
  const relevant = ["failed", "approval", "blocking", "supervisor"];
  for (const key of relevant) await shown(page, `#thread-list-overview [data-section="attention"] li[data-thread="${fixture.threads[key].id}"]`);
  const order = await section.locator("li.tl-row").evaluateAll(rows => rows.map(row => row.dataset.thread));
  const position = key => order.indexOf(fixture.threads[key].id);
  assert(position("failed") < position("supervisor") && position("blocking") < position("supervisor"), JSON.stringify(order));
  assert(position("approval") < position("supervisor"), JSON.stringify(order));
  return {order};
}

async function quietSubmission(page, fixture) {
  const thread = fixture.threads.update;
  const marker = "Check the next sample quietly.";
  await openConversation(page, fixture, thread);
  const {body: before} = await getBrowserAPI(fixture, "threads/" + thread.id);
  const accepted = page.waitForResponse(response => isSubmission(response.request()) && sentText(response.request()) === marker);
  void accepted.catch(() => {});
  await compose(page, marker, "Alt+Meta+Enter");
  const response = await accepted;
  assert(response.ok());
  assert.equal(response.request().postDataJSON()?.quiet, true);
  await expectContains(page.locator("#timeline article.message.user"), marker);
  for (let attempt = 0; attempt < 20; attempt++) {
    const {body} = await getBrowserAPI(fixture, "threads/" + thread.id);
    if (body.turn_completion?.status === "completed" && body.turn_completion.id > before.turn_completion.id) {
      assert.equal(body.needs_attention, false);
      return {thread: thread.id, attention: body.needs_attention, completion: body.turn_completion.id};
    }
    await delay(50);
  }
  assert.fail("Could not observe the quiet completion.");
}

async function optimisticOrdering(page, fixture) {
  const first = fixture.threads.supervisor;
  const second = fixture.threads.other;
  await openConversation(page, fixture, first);
  await openFromList(page, fixture, second);
  await selectTab(page, first);
  await readyConversation(page, first);
  const signals = [];
  let releaseAcceptance;
  const hold = new Promise(resolve => { releaseAcceptance = resolve; });
  const marker = "Synthetic acceptance ";
  const acknowledgements = [];
  const receipts = [];
  page.on("response", response => {
    if (isSubmission(response.request()) && sentText(response.request())?.startsWith(marker)) {
      acknowledgements.push({text: sentText(response.request()), status: response.status()});
      receipts.push(response.json().then(body => body.acceptedMessage || body.transferSubmission || null).catch(() => null));
    }
  });
  await page.route(/\/api\/browser\/sessions\/[^/]+\/(?:messages|queue)$/, async route => {
    const request = route.request();
    const message = sentText(request);
    if (!isSubmission(request) || !message?.startsWith(marker)) return route.continue();
    signals.push({path: new URL(request.url()).pathname, body: request.postDataJSON()});
    await hold;
    if (message.endsWith("reject")) {
      await route.fulfill({status: 400, contentType: "application/json",
        body: JSON.stringify({error: "Synthetic acceptance was rejected."})});
    } else await route.continue();
  });
  try {
    await compose(page, marker + "first");
    await expectContains(page.locator("#timeline article.message.user"), marker + "first");
    await compose(page, marker + "reject");
    await expectContains(page.locator("#timeline article.message.user"), marker + "reject");
    await selectTab(page, second);
    await readyConversation(page, second);
    assert.equal(await page.locator("#timeline article.message.user").filter({hasText: marker}).count(), 0);
    await compose(page, marker + "other");
    await expectContains(page.locator("#timeline article.message.user"), marker + "other");
  } finally {
    releaseAcceptance();
  }
  for (let attempt = 0; acknowledgements.length < 3 && attempt < 80; attempt++) await delay(50);
  assert.equal(acknowledgements.length, 3, JSON.stringify(acknowledgements));
  assert(acknowledgements.find(item => item.text.endsWith("reject"))?.status === 400);
  assert(acknowledgements.filter(item => !item.text.endsWith("reject")).every(item => item.status < 300));
  await selectTab(page, first);
  await expectContains(page.locator("#timeline"), marker + "first");
  await expectContains(page.locator("body"), "Synthetic acceptance was rejected.");
  const firstRows = page.locator("#timeline article.message.user").filter({hasText: marker + "first"});
  const rendered = await firstRows.evaluateAll(rows => rows.map(row => ({
    optimistic: row.hasAttribute("data-outgoing"), turn: row.dataset.searchTurn, item: row.dataset.searchItem,
    status: row.querySelector(".outgoing-status")?.textContent,
  })));
  assert.equal(await page.locator("#timeline article.message.user").filter({hasText: marker + "other"}).count(), 0);
  assert.equal(signals.filter(signal => signal.path.includes(`/${first.id}/`)).length, 2);
  assert.equal(signals.filter(signal => signal.path.includes(`/${second.id}/`)).length, 1);
  const ids = signals.map(signal => signal.body.client_message_id || signal.body.clientMessageId || signal.body.requestId);
  assert(ids.every(Boolean));
  assert.equal(new Set(ids).size, 3);
  const [firstHistory, secondHistory] = await Promise.all([first, second].map(thread =>
    getBrowserAPI(fixture, `sessions/${thread.id}?recent=1`)));
  const canonical = [];
  for (const [history, good, foreign] of [[firstHistory, "first", "other"], [secondHistory, "other", "first"]]) {
    const turns = history.body.thread?.turns || [];
    const text = turns.flatMap(turn => turn.items.filter(item => item.type === "userMessage"))
      .flatMap(item => item.content.map(part => part.text));
    assert.equal(text.filter(value => value === marker + good).length, 1, JSON.stringify(acknowledgements));
    assert(!text.some(value => value === marker + foreign || value === marker + "reject"));
    canonical.push(...turns.flatMap(turn => turn.items.filter(item => item.type === "userMessage" &&
      item.content?.some(part => part.text === marker + good)).map(item => ({turn: turn.id, item: item.id, text: marker + good}))));
  }
  const accepted = await Promise.all(receipts);
  assert.equal(rendered.length, 1, JSON.stringify({rendered, accepted, canonical}));
  return {requests: signals, acknowledgements, rendered, canonical};
}

async function drafts(page, fixture) {
  const first = fixture.threads.other;
  const second = fixture.threads.lead;
  const attachment = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9h8AAAAASUVORK5CYII=", "base64");
  await openConversation(page, fixture, first);
  await page.locator("#message").fill("Preserve the unsent calibration draft.");
  const dataTransfer = await page.evaluateHandle(bytes => {
    const transfer = new DataTransfer();
    transfer.items.add(new File([new Uint8Array(bytes)], "calibration-sample.png", {type: "image/png"}));
    return transfer;
  }, Array.from(attachment));
  await page.locator("#composer").dispatchEvent("drop", {dataTransfer});
  await dataTransfer.dispose();
  const preview = '#image-attachments img[alt="calibration-sample.png"]';
  await shown(page, preview);
  await openFromList(page, fixture, second, {expand: [fixture.threads.supervisor]});
  assert.equal(await page.locator("#message").inputValue(), "");
  await page.locator("#message").fill("Independent draft for the lead.");
  await selectTab(page, first);
  await readyConversation(page, first);
  await page.reload({waitUntil: "domcontentloaded"});
  await readyConversation(page, first);
  await page.waitForFunction(() => document.querySelector("#message").value === "Preserve the unsent calibration draft.");
  await shown(page, preview);
  await openFromList(page, fixture, second, {expand: [fixture.threads.supervisor]});
  await page.waitForFunction(() => document.querySelector("#message").value === "Independent draft for the lead.");
  assert.equal(await page.locator("#image-attachments .image-attachment").count(), 0);
  return {drafts: [first.id, second.id], attachment: "calibration-sample.png"};
}

async function pagingAndOpenUI(page, fixture) {
  const thread = fixture.threads.supervisor;
  const pages = [], full = [];
  page.on("request", request => {
    const route = new URL(request.url()), query = route.searchParams;
    if (query.has("before_seq")) pages.push({cursor: query.get("before_seq"), limit: query.get("limit"), route: route.pathname});
    if (query.get("full") === "1") full.push(route.pathname);
  });
  await openConversation(page, fixture, thread);
  const result = {initial: await page.locator("#timeline article.message").count()};
  const frame = page.frameLocator('#timeline iframe[title="Interactive agent response"]');
  await frame.getByText("Readings", {exact: true}).click();
  await shown(frame, "text=Bore diameter: 24.01 mm");
  await openFromList(page, fixture, fixture.threads.other);
  await selectTab(page, thread);
  await readyConversation(page, thread);
  await shown(frame, "text=Bore diameter: 24.01 mm");
  const count = () => page.locator("#timeline article.message").count();
  const older = page.getByRole("button", {name: /(?:show|load) (?:\d+ )?earlier (?:turns|messages)/i});
  for (let index = 0; index < 4; index++) {
    await page.locator("#timeline").evaluate(el => { el.scrollTop = 0; });
    if (await older.first().isVisible()) await older.first().click();
    await page.waitForFunction(index => [...performance.getEntriesByType("resource")]
      .filter(entry => new URL(entry.name).searchParams.has("before_seq")).length > index ||
      Boolean(document.querySelector("#timeline")?.textContent.includes("inspection record 0001.")), index);
    if (index === 0) assert(pages.length > 0, `No bounded earlier-page request. Full: ${full.join(",")}`);
  }
  assert.equal(full.length, 0);
  assert(pages.length >= 4 && new Set(pages.map(page => page.cursor)).size >= 4);
  assert(pages.every(page => Number(page.limit) > 0 && Number(page.limit) <= 40));
  result.final = await count();
  assert(result.final <= 100, `A 320-turn timeline mounted ${result.final} message articles.`);
  return {...result, pages, openUIState: "Readings remained selected"};
}

async function sharedPanes(page, fixture) {
  const requests = [];
  page.on("request", request => {
    const url = new URL(request.url());
    if (url.pathname === "/api/browser/events") requests.push(url.pathname);
  });
  for (const [directory, count] of [[fixture.pair, 2], [fixture.six, 6]]) {
    if (count === 2) {
      await openTiling(page, fixture, directory, count);
    } else {
      const switcher = page.locator("#open-project-tab");
      await switcher.click();
      await page.locator("#folder-path").fill(directory);
      await page.locator("#folder-path").press("Enter");
      const destination = page.locator('#project-target-dialog button[value="tab"]');
      await destination.click();
      await page.waitForFunction(directory => document.querySelector('#view-tabs [aria-selected="true"]')
        ?.dataset.projectCwd === directory, directory);
      await page.locator("#tile-threads").click();
    }
    await page.waitForFunction(count => document.querySelectorAll(
      "#thread-panes section.conversation-pane-frame[data-thread-id]").length >= count, count);
    assert.equal(await page.locator("#thread-panes iframe.conversation-pane-frame").count(), 0);
    const panes = page.locator("#thread-panes section.conversation-pane-frame:visible");
    assert.equal(await panes.count(), count);
    assert.equal(await panes.locator("[data-pane-composer]:visible").count(), count);
    assert.equal(requests.length, 1);
  }
  return {paneCounts: [2, 6], eventSources: requests.length};
}


async function retainedOverviewModes(page, fixture) {
  await browserOpen(page, fixture);
  await waitOverview(page, 3);
  await shown(page, "#thread-list-overview");
  assert.equal(await page.locator("#open-asks").count(), 0);
  const toggle = page.getByRole("button", {name: "Choose overview layout"});
  const available = [];
  for (const [name, selector] of [["Focus", "#working-set-overview"], ["Atlas", "#atlas-overview"], ["List", "#thread-list-overview"]]) {
    await toggle.click();
    const layout = page.getByRole("menu", {name: "Overview layout"});
    assert.equal(await layout.getByRole("menuitemradio", {name: "Cards", exact: true}).count(), 0);
    await layout.getByRole("menuitemradio", {name, exact: true}).click();
    await shown(page, selector);
    available.push(name);
  }
  await page.getByRole("button", {name: /Open dispatcher/}).click();
  await shown(page, "#dispatcher-workspace");
  return {default: "List", available: [...available, "Dispatcher Workspace"]};
}

async function compositeOpen(fixture, scoped = false) {
  const {body: config} = await getBrowserAPI(fixture, "config");
  assert.equal(config.conversationOpen, 1);
  const thread = fixture.threads.supervisor;
  const route = id => "conversations/" + encodeURIComponent(id) + "/open";
  const result = await getBrowserAPI(fixture, route(thread.id));
  assert.equal(result.status, 200);
  const body = result.body;
  assert.equal(body.session.thread_id, thread.id);
  assert.equal(body.work_thread.thread_id, thread.id);
  assert(Array.isArray(body.asks) && body.asks.some(ask => ask.id === fixture.asks.conversation));
  assert(Array.isArray(body.schedules));
  assert(body.agent_tree && body.sidecars && "models" in body);
  assert(Array.isArray(body.recent_turns?.turns ?? body.recent_turns));
  for (const name of ["history_revision", "stream_seq", "metadata_revision"]) {
    assert(Number.isInteger(body[name]), "Missing integer revision " + name);
  }
  const unauthenticated = await getBrowserAPI(fixture, route(thread.id), {headers: {"X-Forwarded-Host": "fixture.invalid"}});
  assert([401, 403].includes(unauthenticated.status));
  const managed = await getBrowserAPI(fixture, route(fixture.threads.managed.id));
  assert(managed.status >= 400 && managed.status < 500);
  const outer = await getBrowserAPI(fixture, route(fixture.threads.remote.id));
  assert(scoped ? outer.status >= 400 && outer.status < 500 : outer.status === 200);
  if (!scoped) {
    const holder = await getBrowserAPI(fixture, route(fixture.stream.thread));
    assert.equal(holder.status, 200);
    const response = await fetch(fixture.base + "/fixture/advance");
    assert(response.ok);
    const updated = await getBrowserAPI(fixture, route(fixture.stream.thread));
    assert(updated.body.stream_seq > holder.body.stream_seq || updated.body.history_revision > holder.body.history_revision,
      "Real provider delta did not advance the history or stream revision.");
  }
  return {local: result.status, unauthenticated: unauthenticated.status,
    managedWorker: managed.status, outsideExecutionScope: outer.status, scoped};
}


async function postBrowserAPI(fixture, route, body) {
  const {body: config} = await getBrowserAPI(fixture, "config");
  const result = await getBrowserAPI(fixture, route, {method: "POST",
    headers: {"Content-Type": "application/json", "X-Agent-Coord-Token": config.token},
    body: JSON.stringify(body)});
  assert(result.status >= 200 && result.status < 300, `POST ${route} returned ${result.status}: ${result.body.error || ""}`);
  return result.body;
}

async function metadataUpdates(page, fixture) {
  const thread = fixture.threads.metadata;
  await openConversation(page, fixture, thread);
  await shown(page, `#thread-asks .ask-row[data-id="${fixture.asks.metadata}"]`);
  const route = `conversations/${thread.id}/open`;
  const before = await getBrowserAPI(fixture, route);
  assert.equal(before.status, 200);
  const title = "Revised inspection drawings";
  await postBrowserAPI(fixture, "threads/" + thread.id, {
    title, pinned: false, quiet: true, project_id: fixture.project,
  });
  await postBrowserAPI(fixture, "asks/" + fixture.asks.metadata, {
    resolution: "answered", answer: "Use the marked tolerances.",
  });
  const schedule = "Resample the revised inspection drawings.";
  await postBrowserAPI(fixture, "schedules", {
    id: "f902aca3-84bd-4ce6-8779-49eb9ed43f7b",
    target_thread_id: thread.id, run_at: fixture.anchor + 8 * 86400, timezone: "America/Chicago",
    message: schedule,
  });
  await expectContains(page.locator("#title-heading"), title);
  await page.locator(`#thread-asks .ask-row[data-id="${fixture.asks.metadata}"]`).waitFor({state: "hidden"});
  await page.waitForFunction(() => !document.querySelector("#quiet-thread")?.textContent.includes("Make quiet"));
  const alarm = page.locator("#thread-schedules");
  await alarm.click();
  await expectContains(page.locator("dialog[open]"), schedule);
  const after = await getBrowserAPI(fixture, route);
  assert.equal(after.status, 200);
  assert.equal(after.body.history_revision, before.body.history_revision);
  assert(after.body.metadata_revision > before.body.metadata_revision);
  assert.deepEqual({
    group: after.body.work_thread.project_id, pinned: after.body.work_thread.pinned, quiet: after.body.work_thread.quiet,
  }, {group: fixture.project, pinned: false, quiet: true});
  assert(after.body.schedules.some(item => item.message === schedule));
  const dialogs = page.locator("dialog[open]");
  await page.keyboard.press("Escape");
  await dialogs.waitFor({state: "hidden"});
  await page.locator('#conversation-tabs [data-thread-tab=""]').click();
  const row = page.locator(`#thread-list-overview [data-thread="${thread.id}"][data-action="open"]`);
  await expectContains(row, title);
  assert.equal(await row.locator("xpath=ancestor::section[1]").getAttribute("data-section"), "quiet");
  return {thread: thread.id, before: before.body.metadata_revision, after: after.body.metadata_revision};
}

async function persistedCacheRestart(browser) {
  const directory = await mkdtemp(join(tmpdir(), "ribbon-interface-restart-"));
  let initial, replacement;
  try {
    initial = await startFixture("interactions", {directory});
    const fixture = initial.fixture;
    return await withPage(browser, async page => {
      const thread = fixture.threads.other;
      await openConversation(page, fixture, thread);
      const draft = "Confirm the tolerance table after the coordinator restarts.";
      await page.locator("#message").fill(draft);
      // Let deferred storage commits settle without consulting the app's cache implementation.
      await delay(500);
      await initial.cleanup();
      initial = null;
      replacement = await startFixture("interactions", {directory, resume: true,
        port: new URL(fixture.base).port});
      assert.notEqual(replacement.fixture.pid, fixture.pid);
      assert.equal(replacement.fixture.base, fixture.base);
      let unblockLiveRead;
      let observed = false;
      const hold = new Promise(resolve => { unblockLiveRead = resolve; });
      await page.route(new RegExp(`/api/browser/(?:conversations/${thread.id}/open|sessions/${thread.id}(?:[?]|$))`),
        async route => {
          observed = true;
          await hold;
          await route.continue();
        });
      try {
        await page.reload({waitUntil: "domcontentloaded"});
        await readyConversation(page, thread);
        assert.equal(await page.locator("#message").inputValue(), draft);
        assert(observed, "The refreshed page never attempted a live transcript read.");
      } finally {
        unblockLiveRead();
      }
      return {originalPID: fixture.pid, restartedPID: replacement.fixture.pid,
        renderedBeforeLiveRead: true, draftRestored: true};
    });
  } finally {
    await Promise.all([initial?.cleanup(), replacement?.cleanup()]);
    await rm(directory, {recursive: true, force: true});
  }
}

async function startupAsset(page, fixture, scenario) {
  let requests = 0;
  const pattern = /\/dist\/app-[0-9a-f]{20}\.js(?:\?|$)/;
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route(pattern, async route => {
    requests++;
    if (scenario === "unavailable" || (scenario === "retry" && requests === 1)) await route.abort("failed");
    else {
      if (scenario === "delayed") await delay(4400);
      await route.continue();
    }
  });
  await browserOpen(page, fixture);
  if (scenario === "unavailable") {
    await expectContains(page.locator("#error"), "Could not load Ribbon Field");
    assert(requests >= 2, `Only ${requests} asset requests were made before displaying failure.`);
    assert.equal(await page.locator("html").getAttribute("data-startup"), "failed");
  } else {
    if (scenario === "delayed") {
      await delay(4050);
      assert(!await page.locator("#error").isVisible(), "Loading displayed a premature error.");
    }
    await waitOverview(page, 3);
    assert(scenario === "retry" ? requests >= 2 : requests >= 1, `Observed ${requests} hashed asset requests.`);
    assert.equal(await page.locator("html").getAttribute("data-startup"), "loaded");
    assert.equal(await page.locator("#error").isVisible(), false);
  }
  assert.equal(errors.length, 0, errors.join("\n"));
  return {scenario, requests};
}

async function browserCacheRecovery(page, fixture) {
  const sources = [];
  page.on("request", request => {
    if (new URL(request.url()).pathname === "/api/browser/events") sources.push(Date.now());
  });
  await page.addInitScript(() => {
    window.interfaceReturnEvents = [];
    window.addEventListener("pageshow", event => window.interfaceReturnEvents.push(event.persisted));
  });
  const thread = fixture.threads.background;
  await openConversation(page, fixture, thread);
  await expectContains(page.locator("#timeline"), "Streaming inspection update");
  await page.locator("#message").fill("Keep this unsent until the calibration completes.");
  await page.waitForFunction(() => window.interfaceReturnEvents.length === 1);
  await page.goto(fixture.base + "/fixture/document", {waitUntil: "load"});
  const result = await fetch(fixture.base + "/fixture/advance");
  assert(result.ok);
  await page.goBack({waitUntil: "commit"});
  try {
    await page.waitForFunction(() => window.interfaceReturnEvents?.includes(true));
  } catch {
    const navigation = await page.evaluate(() => {
      const timing = performance.getEntriesByType("navigation")[0];
      return {type: timing?.type, notRestoredReasons: timing?.notRestoredReasons,
        pageShow: window.interfaceReturnEvents};
    });
    assert.fail("Browser did not enter the back-forward cache: " + JSON.stringify(navigation));
  }
  await expectContains(page.locator("#timeline"), "Applied the final calibration sample.");
  assert.equal(await page.locator("#message").inputValue(), "Keep this unsent until the calibration completes.");
  assert.equal(sources.length, 2);
  return {persistedPageShow: true, eventSourceRequests: sources.length};
}

async function interactions(browser, fixture, report) {
  const desktop = run => withPage(browser, async page => {
    await page.addInitScript(rememberOverview, "list");
    return run(page);
  });
  await recordCheck(report, "List default, retained overview modes and one Queue entry",
    () => withPage(browser, page => retainedOverviewModes(page, fixture)));
  await recordCheck(report, "real composite open and authorization", () => compositeOpen(fixture));
  await recordCheck(report, "Attention stages blockers and approvals before informational replies",
    () => desktop(page => attentionOrder(page, fixture)));
  await recordCheck(report, "mobile composer, geometry and header controls",
    () => withPage(browser, page => mobileLayout(page, fixture),
      {viewport: {width: 390, height: 844}, hasTouch: true, colorScheme: "dark"}));
  await recordCheck(report, "uncategorized reply clears when read", () =>
    desktop(page => attentionReceipts(page, fixture, "supervisor", true, "reply")));
  await recordCheck(report, "routine update clears when read", () =>
    desktop(page => attentionReceipts(page, fixture, "update", true, "update")));
  await recordCheck(report, "quiet keyboard submission suppresses clean completion attention",
    () => desktop(page => quietSubmission(page, fixture)));
  await recordCheck(report, "stalled provider stays in Attention after reading", () =>
    desktop(page => attentionReceipts(page, fixture, "stalled", false, "action")));
  // Discuss before Enter, so the same Ask must survive the first operation.
  for (const surface of ["Queue", "Conversation"]) {
    await recordCheck(report, surface + " Shift+Enter discusses without resolution", () =>
      desktop(page => askFlow(page, fixture, surface, "Shift")));
    await recordCheck(report, surface + " Enter resolves with typed text", () =>
      desktop(page => askFlow(page, fixture, surface, "")));
  }
  await recordCheck(report, "pending approval stays in Attention after reading", () =>
    desktop(page => attentionReceipts(page, fixture, "approval", false, "input")));
  await recordCheck(report, "blocking Ask stays in Attention after reading", () =>
    desktop(page => attentionReceipts(page, fixture, "blocking", false, "input")));
  await recordCheck(report, "failed turn stays in Attention after reading", () =>
    desktop(page => attentionReceipts(page, fixture, "failed", false, "failed")));
  await recordCheck(report, "immutable family tab and inline breadcrumb", () =>
    desktop(page => familyNavigation(page, fixture)));
  await recordCheck(report, "cross-repository open retains the active workspace and existing tabs", () =>
    desktop(page => crossRepository(page, fixture)));
  await recordCheck(report, "per-thread optimistic delivery while acceptance is delayed or rejected", () =>
    desktop(page => optimisticOrdering(page, fixture)));
  await recordCheck(report, "per-thread unsent drafts and image survive reload", () =>
    desktop(page => drafts(page, fixture)));
  await recordCheck(report, "bounded earlier pages, mounted timeline and OpenUI state", () =>
    desktop(page => pagingAndOpenUI(page, fixture)));
  await recordCheck(report, "two to six in-process conversation panes use one EventSource", () =>
    desktop(page => sharedPanes(page, fixture)));
  await recordCheck(report, "metadata-only revisions reconcile title, pin, quiet, group, ask and schedule",
    () => desktop(page => metadataUpdates(page, fixture)));
  for (const scenario of ["retry", "unavailable", "delayed"]) {
    await recordCheck(report, "hashed workspace startup " + scenario, () =>
      withPage(browser, page => startupAsset(page, fixture, scenario)));
  }
  await recordCheck(report, "browser back-forward cache resumes SSE and keeps the draft",
    () => desktop(page => browserCacheRecovery(page, fixture)));
}


function percentiles(values) {
  const sorted = [...values].sort((left, right) => left - right);
  const at = fraction => sorted[Math.ceil(sorted.length * fraction) - 1] ?? null;
  return {count: sorted.length, p50: at(.50), p99: at(.99), maximum: at(1)};
}

function elapsedUnion(intervals, from, to) {
  const ordered = intervals.map(([start, end]) => [Math.max(start, from), Math.min(end, to)])
    .filter(([start, end]) => end > start).sort((left, right) => left[0] - right[0]);
  let end = from;
  let elapsed = 0;
  for (const [start, finish] of ordered) {
    elapsed += Math.max(0, finish - Math.max(start, end));
    end = Math.max(end, finish);
  }
  return elapsed;
}

async function traceBoot(page, fixture, thread = null) {
  const browser = page.context().browser();
  const cdp = await browser.newBrowserCDPSession();
  const data = [];
  cdp.on("Tracing.dataCollected", event => data.push(...event.value));
  await cdp.send("Tracing.start", {
    categories: "devtools.timeline,blink.user_timing,toplevel,v8,disabled-by-default-devtools.timeline",
    transferMode: "ReportEvents",
  });
  try {
    await page.addInitScript(({diagnostic, title, latest}) => {
      const observer = new MutationObserver(() => {
        if (title) {
          const composer = document.getElementById("message");
          const headline = document.getElementById("title-heading");
          const turns = document.getElementById("timeline");
          if (!headline?.textContent.includes(title) || !turns?.textContent.includes(latest) ||
              !composer?.getClientRects().length || composer.disabled ||
              /opening conversation/i.test(composer.placeholder)) return;
        } else {
          const status = document.getElementById("results-count");
          if (!status || !/\d+ threads?/.test(status.textContent)) return;
          const selected = document.querySelector('#view-tabs [aria-selected="true"]');
          if (!selected?.getClientRects().length) return;
        }
        performance.mark("ribbon-field-fixture-ready");
        observer.disconnect();
        if (diagnostic) requestAnimationFrame(() => {
          const channel = new MessageChannel();
          channel.port1.onmessage = () => {
            performance.mark("ribbon-field-fixture-rendered");
            channel.port1.close();
            channel.port2.close();
          };
          channel.port2.postMessage(null);
        });
      });
      observer.observe(document, {subtree: true, childList: true, characterData: true, attributes: true});
    }, {diagnostic: Boolean(options["boot-trace"]), title: thread?.title, latest: thread?.latest});
    await browserOpen(page, fixture, fixture.workspace, thread ? {thread} : undefined);
    const mark = options["boot-trace"] ? "ribbon-field-fixture-rendered" : "ribbon-field-fixture-ready";
    await page.waitForFunction(mark => performance.getEntriesByName(mark).length === 1, mark);
  } finally {
    const complete = new Promise(resolve => cdp.once("Tracing.tracingComplete", resolve));
    await cdp.send("Tracing.end");
    await complete;
    await cdp.detach();
  }
  const ready = data.find(event => event.name === "ribbon-field-fixture-ready");
  assert(ready, "Chromium did not record the DOM readiness mark.");
  const events = data.filter(event => event.pid === ready.pid && event.tid === ready.tid);
  const resources = await page.evaluate(() => performance.getEntriesByType("resource").map(entry => ({
    path: new URL(entry.name).pathname, start: entry.startTime, end: entry.responseEnd,
    duration: entry.duration, transferBytes: entry.transferSize,
  })));
  const scripts = resources.filter(entry => entry.path.endsWith(".js") && !entry.path.includes("openui"));
  const bundle = scripts.find(entry => /^\/dist\/app-[0-9a-f]{20}\.js$/.test(entry.path));
  const evaluations = events.filter(event => ["EvaluateScript", "v8.compile"].includes(event.name) &&
    JSON.stringify(event.args).includes(fixture.base));
  const start = Math.min(...evaluations.map(event => event.ts));
  const stop = ready.ts;
  assert(Number.isFinite(start) && start <= stop, "No browser trace execution for first-party workspace JavaScript.");
  const tasks = events.filter(event => event.ph === "X" && ["RunTask", "ThreadControllerImpl::RunTask"].includes(event.name));
  assert(tasks.length, "No renderer main-thread task spans in trace.");
  const timings = {
    mainThreadBootMs: elapsedUnion(tasks.map(event => [event.ts, event.ts + event.dur]), start, stop) / 1000,
    wallFromFirstScriptToReadyMs: (stop - start) / 1000,
    taskSpans: tasks.length, bundle: bundle?.path || null,
    scriptRequests: scripts, httpRequests: resources.filter(entry => entry.path.startsWith("/api/browser/")),
    method: "Chromium renderer main-thread task spans from first local script evaluation or compilation to visible inventory readiness. Overlapping spans are unioned, leaving network waits and other idle gaps out. Includes style, layout, and any other renderer work on that thread.",
  };
  if (!options["boot-trace"]) return timings;

  const traceFile = `boot-${thread ? "conversation" : "inventory"}.json.gz`;
  await writeFile(join(output, traceFile), gzipSync(JSON.stringify({traceEvents: data})));
  const rendered = events.find(event => event.name === "ribbon-field-fixture-rendered");
  const deadline = rendered?.ts || stop;
  const frames = data.flatMap(event => {
    if (event.name === "TracingStartedInBrowser") return event.args?.data?.frames || [];
    if (["CommitLoad", "FrameCommittedInBrowser"].includes(event.name)) return [event.args?.data];
    return [];
  }).filter(frame => frame?.frame);
  const openUIFrames = new Set(frames.filter(frame => frame.parent && frame.url?.includes("/openui-frame.html"))
    .map(frame => frame.frame));
  const spans = events.filter(event => event.ph === "X" && event.ts < deadline && event.ts + event.dur > start);
  const duration = (selected, until = stop) => elapsedUnion(
    selected.map(event => [event.ts, event.ts + event.dur]), start, until) / 1000;
  const selectedScript = event => JSON.stringify(event.args).includes(bundle?.path || fixture.base);
  const frameOf = event => event.args?.data?.frame || event.args?.beginData?.frame;
  const openUICommit = data.find(event => event.name === "CommitLoad" && openUIFrames.has(frameOf(event)));
  const work = name => spans.filter(event => name.test(event.name));
  const paint = work(/^(Paint|PrePaint|CompositeLayers|Layerize|UpdateLayerTree)$/);
  const layout = work(/^(Layout|UpdateLayoutTree|LocalFrameView::performLayout|RecalculateStyles)$/);
  timings.diagnostic = {
    traceFile,
    rendererWorkToRenderingOpportunityMs: duration(tasks, deadline),
    wallToRenderingOpportunityMs: (deadline - start) / 1000,
    workspaceMainThreadCompileMs: duration(work(/compile/i).filter(selectedScript)),
    workspaceBackgroundParseMs: data.filter(event => event.ph === "X" &&
      event.name === "v8.parseOnBackground" && selectedScript(event))
      .reduce((milliseconds, event) => milliseconds + event.dur / 1000, 0),
    workspaceEvaluateMs: duration(work(/^EvaluateScript$/).filter(selectedScript)),
    tracedLayoutAndStyleMs: duration(layout),
    tracedPaintAndLayersMs: duration(paint, deadline),
    identifiableOpenUIFrameWorkMs: duration(spans.filter(event => openUIFrames.has(frameOf(event))), deadline),
    firstOpenUIFrameCommitMs: openUICommit ? (openUICommit.ts - start) / 1000 : null,
    paintEntries: await page.evaluate(() => performance.getEntriesByType("paint").map(entry => ({
      name: entry.name, startMs: entry.startTime,
    }))),
    method: "Category spans overlap and are reported independently. OpenUI work counts trace events on the primary renderer thread explicitly attributed to an OpenUI child frame. Background parsing can precede the first main-thread script and overlap network transfer. The rendering opportunity is a task queued from requestAnimationFrame after the original readiness mark.",
  };
  if (thread) timings.method = timings.method.replace("visible inventory", "visible expected transcript and usable composer");
  return timings;
}

async function installInputObserver(page) {
  await page.addInitScript(() => {
    const sensor = window.interfaceAcceptance = {};
    const shown = element => element && element.getClientRects().length &&
      getComputedStyle(element).visibility !== "hidden";
    function onInput(event) {
      const expected = sensor.expected;
      if (!expected || !event.isTrusted || expected.kind !== event.type) return;
      if (event.type === "click" && event.target.closest("[data-thread-tab]")?.dataset.threadTab !== expected.thread) return;
      if (event.type === "keydown" && (event.key !== "Enter" || event.target.id !== "message")) return;
      sensor.expected = null;
      const {timeStamp} = event;
      const match = () => {
        const composer = document.getElementById("message");
        const timeline = document.getElementById("timeline");
        if (!timeline || !shown(composer) || composer.disabled) return false;
        if (event.type === "click" && (
          !document.querySelector('[data-thread-tab="' + expected.thread + '"][aria-selected="true"]') ||
          document.activeElement !== composer)) return false;
        const query = event.type === "keydown" ? "article.message.user" : "article.message";
        const item = [...timeline.querySelectorAll(query)].find(node => node.textContent.includes(expected.text));
        if (!shown(item)) return false;
        const itemRect = item.getBoundingClientRect(), region = timeline.getBoundingClientRect();
        return itemRect.bottom > region.top && itemRect.top < region.bottom;
      };
      let firstDOM;
      const observer = new MutationObserver(schedule);
      const channel = new MessageChannel();
      let pending = false;
      const timeout = setTimeout(() => {
        close();
        sensor.result = {error: "Timed out waiting for the expected visible turns and composer.", ...expected};
      }, 3500);
      function close() {
        clearTimeout(timeout);
        observer.disconnect();
        document.removeEventListener("focusin", schedule);
        channel.port1.close();
        channel.port2.close();
      }
      channel.port1.onmessage = () => {
        if (!match()) { pending = false; schedule(); return; }
        close();
        sensor.result = {trusted: event.isTrusted, browserEventTime: timeStamp,
          domMs: firstDOM - timeStamp, afterRenderingOpportunityMs: performance.now() - timeStamp};
      };
      function schedule() {
        if (pending) return;
        if (!match()) return;
        firstDOM ??= performance.now();
        pending = true;
        requestAnimationFrame(() => channel.port2.postMessage(null));
      }
      observer.observe(document.body, {subtree: true, childList: true, characterData: true, attributes: true});
      // A route may focus the composer after updating the transcript.
      document.addEventListener("focusin", schedule);
      requestAnimationFrame(schedule);
    }
    document.addEventListener("click", onInput, true);
    document.addEventListener("keydown", onInput, true);
  });
}

async function measureInput(page, expected, input) {
  await page.evaluate(expected => {
    window.interfaceAcceptance.expected = expected;
    window.interfaceAcceptance.result = null;
  }, expected);
  await input();
  await page.waitForFunction(() => window.interfaceAcceptance.result !== null,
    undefined, {timeout: 5000, polling: 10});
  const result = await page.evaluate(() => window.interfaceAcceptance.result);
  assert(!result.error && result.trusted, JSON.stringify(result));
  return result;
}

async function switchSamples(page, fixture, count, samples) {
  await installInputObserver(page);
  const first = fixture.threads.supervisor, second = fixture.threads.other;
  await openConversation(page, fixture, first);
  await openFromList(page, fixture, second);
  for (let index = 0; index < count; index++) {
    const thread = index % 2 ? second : first;
    const result = await measureInput(page, {kind: "click", thread: thread.id, text: thread.latest},
      () => selectTab(page, thread));
    samples.push({thread: thread.id, ...result});
  }
}

async function enterSamples(page, fixture, count, samples) {
  await installInputObserver(page);
  const thread = fixture.threads.other;
  await openConversation(page, fixture, thread);
  await page.route(/\/api\/browser\/sessions\/[^/]+\/(?:messages|queue)$/, async route => {
    if (isSubmission(route.request())) await delay(400);
    await route.continue();
  });
  for (let index = 0; index < count; index++) {
    const text = `Optimistic sample ${String(index + 1).padStart(3, "0")} of inspection input.`;
    const accepted = page.waitForResponse(response => isSubmission(response.request()) && sentText(response.request()) === text);
    void accepted.catch(() => {});
    await page.locator("#message").fill(text);
    const result = await measureInput(page, {kind: "keydown", text}, () => page.locator("#message").press("Enter"));
    samples.push(result);
    const response = await accepted;
    assert(response.ok(), `Sample acknowledgement was HTTP ${response.status()}.`);
  }
}

async function idleNetwork(page, fixture, duration, result) {
  await page.addInitScript(() => {
    const NativeEventSource = window.EventSource;
    const observed = window.interfaceEventSources = [];
    window.EventSource = class extends NativeEventSource {
      constructor(...args) {
        super(...args);
        const entry = {source: this, opens: 0, errors: 0};
        this.addEventListener("open", () => entry.opens++);
        this.addEventListener("error", () => entry.errors++);
        observed.push(entry);
      }
    };
  });
  const snapshot = () => page.evaluate(() => window.interfaceEventSources.map(({source, opens, errors}) =>
    ({readyState: source.readyState, opens, errors})));
  const requests = new Map();
  const collect = request => {
    const url = new URL(request.url());
    if (!url.pathname.startsWith("/api/browser/")) return;
    const entry = {method: request.method(), path: url.pathname, time: Date.now()};
    if (entry.method === "POST" && entry.path === "/api/browser/activity/events") {
      entry.visits = (request.postDataJSON()?.events || []).map(visit => ({
        kind: visit.kind, source: visit.source,
        identity: createHash("sha256").update(visit.event_id).digest("hex").slice(0, 12),
      }));
    }
    requests.set(request, entry);
  };
  const observeResponse = response => {
    const entry = requests.get(response.request());
    if (entry) entry.status = response.status();
  };
  page.on("request", collect);
  page.on("response", observeResponse);
  try {
    await openConversation(page, fixture, fixture.threads.other);
    await page.waitForFunction(() => window.interfaceEventSources.some(entry => entry.source.readyState === EventSource.OPEN));
    // One-time health and visit receipts can overlap opening the conversation.
    // Use a fixed settling window so recurring requests cannot postpone sampling.
    await delay(2500);
    const before = await snapshot();
    const settlingRequests = new Set(requests.keys());
    const start = performance.now();
    await delay(duration * 1000);
    const elapsed = performance.now() - start;
    const after = await snapshot();
    const startupVisits = [...settlingRequests].map(request => requests.get(request)).filter(entry => entry.visits);
    const traffic = [...requests].filter(([request]) => !settlingRequests.has(request)).map(([, entry]) => entry);
    const polling = traffic.filter(request => request.path !== "/api/browser/events");
    Object.assign(result, {elapsed, startupVisits, traffic, polling, requestsPerMinute: polling.length * 60000 / elapsed,
      eventSourcesBefore: before, eventSourcesAfter: after});
    assert(startupVisits.every(entry => entry.status >= 200 && entry.status < 300),
      "A startup visit was rejected or unfinished before the observation; inspect the startup receipts.");
    assert.deepEqual(before, [{readyState: 1, opens: 1, errors: 0}], JSON.stringify(result));
    assert.deepEqual(after, before, "EventSource disconnected or reopened during the idle observation.");
    assert.equal(polling.length, 0, `${polling.length} HTTP requests during healthy idle SSE.`);
  } finally {
    page.off("request", collect);
    page.off("response", observeResponse);
  }
}

async function performanceChecks(browser, fixture, report) {
  const switches = Number(options.switches), sends = Number(options.sends), idle = Number(options["idle-seconds"]);
  assert(Number.isInteger(switches) && switches > 0);
  assert(Number.isInteger(sends) && sends > 0);
  assert(Number.isFinite(idle) && idle > 0);
  if (options.check) assert(switches >= 120 && sends >= 20 && idle >= 60,
    "The acceptance gate requires at least 120 switches, 20 sends and 60 idle seconds.");
  const results = report.performance;
  const open = run => withPage(browser, async page => {
    await page.addInitScript(rememberOverview, "list");
    return run(page);
  });
  await recordCheck(report, "Renderer compile and boot work <80 ms", () => open(async page => {
    results.boot = await traceBoot(page, fixture);
    if (options["boot-trace"]) {
      results.conversationBoot = await open(conversation => traceBoot(conversation, fixture, fixture.threads.other));
    }
    assert(results.boot.mainThreadBootMs < 80, `Renderer boot was ${results.boot.mainThreadBootMs} ms.`);
    assert(results.boot.bundle && results.boot.scriptRequests.length === 1,
      `Expected one hashed workspace script. Observed ${results.boot.scriptRequests.length} scripts.`);
    return {mainThreadBootMs: results.boot.mainThreadBootMs, bundle: results.boot.bundle};
  }));
  await recordCheck(report, "Cached thread switch click to painted turns and active composer p50 <25 ms, p99 <80 ms",
    () => open(async page => {
      results.switches = {samples: [], method: "Trusted browser click to visible expected DOM and focused composer, followed by a task queued from the first matching animation frame (post-rendering opportunity)."};
      await switchSamples(page, fixture, switches, results.switches.samples);
      const summary = results.switches.summary = percentiles(results.switches.samples.map(result => result.afterRenderingOpportunityMs));
      assert(summary.p50 < 25 && summary.p99 < 80, JSON.stringify(summary));
      return summary;
    }));
  await recordCheck(report, "Enter to visible optimistic DOM <16 ms with acceptance delayed 400 ms",
    () => open(async page => {
      results.enter = {samples: [], method: "Trusted browser keydown to the first rendered-layout user article intersecting the viewport. The next rendering opportunity is reported independently in each sample."};
      await enterSamples(page, fixture, sends, results.enter.samples);
      const summary = results.enter.summary = percentiles(results.enter.samples.map(result => result.domMs));
      assert(summary.maximum < 16, JSON.stringify(summary));
      return summary;
    }));
  await recordCheck(report, "Healthy idle SSE has zero HTTP polls", () => open(async page => {
    results.idle = {};
    await idleNetwork(page, fixture, idle, results.idle);
    return {seconds: results.idle.elapsed / 1000, requestsPerMinute: results.idle.requestsPerMinute};
  }));
}

async function main() {
  await mkdir(output, {recursive: true});
  const report = {at: new Date().toISOString(), source: resolve(options.source), webRoot: options["web-root"] || null,
    mode: options.mode, engine: options.engine, captures: [], checks: [], performance: {}};
  report.sourceRevision = execFileSync("git", ["rev-parse", "HEAD"], {
    cwd: report.source, encoding: "utf8",
  }).trim();
  report.python = execFileSync(options.python, ["--version"], {encoding: "utf8"}).trim();
  if (options["web-root"]) {
    const stamped = await readFile(join(options["web-root"], "index.html"));
    report.webEntrySHA256 = createHash("sha256").update(stamped).digest("hex");
  }
  const browser = activeBrowser = await browserType.launch({headless: true, executablePath: options.executable,
    ...(options.engine === "chromium" ? {ignoreDefaultArgs: ["--disable-back-forward-cache"]} : {})});
  report.browserVersion = browser.version();
  try {
    if (["all", "capture"].includes(options.mode)) await captureMatrix(browser, report);
    if (["all", "interactions"].includes(options.mode) && typeof interactions === "function") {
      await withFixture("interactions", fixture => interactions(browser, fixture, report));
      await withFixture("interactions",
        fixture => recordCheck(report, "scoped composite open denies a different execution repository",
          () => compositeOpen(fixture, true)), "workspace");
      await recordCheck(report, "persisted frontend cache and composer after coordinator process replacement",
        () => persistedCacheRestart(browser));
    }
    if (["all", "performance"].includes(options.mode) && typeof performanceChecks === "function") {
      await withFixture("interactions", fixture => performanceChecks(browser, fixture, report));
    }
  } catch (error) {
    report.error = error.stack;
    throw error;
  } finally {
    await browser.close();
    const endingRevision = execFileSync("git", ["rev-parse", "HEAD"], {cwd: report.source, encoding: "utf8"}).trim();
    if (endingRevision !== report.sourceRevision) {
      report.sourceChangedDuringValidation = endingRevision;
      if (options.check) process.exitCode = 1;
    }
    await writeFile(join(output, "report.json"), JSON.stringify(report, null, 2) + "\n");
  }
  if (options.checks) assert(report.checks.length, "No scenarios matched --checks.");
  if (options.check && (report.captures.some(capture => capture.status !== "captured") ||
      report.checks.some(check => check.status !== "passed"))) process.exitCode = 1;
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.once(signal, () => {
    signals.abort();
    for (const child of observers) child.kill("SIGTERM");
    void activeBrowser?.close().catch(() => {});
    process.exitCode = signal === "SIGINT" ? 130 : 143;
  });
}

await main();
