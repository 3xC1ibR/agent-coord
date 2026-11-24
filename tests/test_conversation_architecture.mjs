// Browser-level conversation regression with a disposable server, real app assets,
// and deterministic large histories. RIBBON_WEB_DIST may identify a packaged build.
import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {createServer, request as forwardRequest} from "node:http";
import {readFile, mkdir, writeFile} from "node:fs/promises";
import {createRequire} from "node:module";
import {dirname, resolve, join} from "node:path";
import {fileURLToPath} from "node:url";
import {once} from "node:events";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dist = process.env.RIBBON_WEB_DIST || join(root, "plugins/agent-coord/scripts/agent_coord/web/dist");
const engines = createRequire(join(root, "demo/package.json"))("playwright");
const engine = process.env.RIBBON_BROWSER_ENGINE || "chromium";
assert.ok(["chromium", "webkit"].includes(engine));
const index = await readFile(join(dist, "index.html"), "utf8");
const output = process.env.RIBBON_FRONTEND_EVIDENCE;
const env = Object.fromEntries(
  Object.entries(process.env).filter(([key]) => !/^(AGENT_COORD_|CODEX_|CLAUDE_CODE_|PYTHONPATH$)/.test(key)),
);
const fixture = spawn(
  process.env.PYTHON || "python3",
  [
    "-Bu",
    "-c",
    `
import sys
sys.path.insert(0, "tests")
import asks_fixture_server as f
from agent_coord.jev_settings import JevSettings
def seed(store, sessions, clock, directory):
    JevSettings(store.database_path).configure({"action":"dismiss"})
    first = None
    for index in range(6):
        thread = sessions.create({"name": "Conversation " + str(index+1), "cwd":str(directory)})["session"]["thread_id"]
        if first is None: first = thread
    return first
f.seed_conversations = seed
f.seed = lambda *args: None
f.PERMITTED_POSTS += ("/api/browser/threads/", "/api/browser/activity/events", "/api/browser/desktop-projects")
f.PERMITTED_MESSAGES += ("/prewarm",)
f.main()
`,
    "--port",
    "0",
  ],
  {cwd: root, env, stdio: ["ignore", "pipe", "pipe"]},
);
let browser, server;
const report = {engine, switches: [], optimistic: [], errors: [], requests: {}, polling: []};
const quantile = (values, percentile) =>
  [...values].sort((a, b) => a - b)[Math.min(values.length - 1, Math.ceil(values.length * percentile) - 1)];
try {
  const upstream = await new Promise((resolve, reject) => {
    let logs = "";
    const timer = setTimeout(() => reject(Error("Fixture startup timed out: " + logs)), 20000);
    fixture.once("error", reject);
    fixture.once("exit", (code) => {
      clearTimeout(timer);
      reject(Error("Fixture exited " + code + ": " + logs));
    });
    for (const stream of [fixture.stdout, fixture.stderr])
      stream.on("data", (data) => {
        logs += data;
        const match = logs.match(/Asks fixture: (http:\/\/127\.0\.0\.1:\d+)/);
        if (match) {
          clearTimeout(timer);
          resolve(match[1]);
        }
      });
  });
  browser = await engines[engine].launch({
    headless: true,
    ...(engine === "chromium" && process.env.RIBBON_BROWSER_CHANNEL
      ? {channel: process.env.RIBBON_BROWSER_CHANNEL}
      : {}),
  });
  const context = await browser.newContext({viewport: {width: 1440, height: 900}});
  const page = await context.newPage();
  page.on("pageerror", (error) => report.errors.push(String(error)));
  page.setDefaultTimeout(15000);
  if (process.env.RIBBON_NO_ATOMIC_MOVE)
    await page.addInitScript(() => {
      Object.defineProperty(Element.prototype, "moveBefore", {value: undefined, configurable: true});
      Object.defineProperty(HTMLFormElement.prototype, "requestSubmit", {
        value: undefined,
        configurable: true,
      });
      Object.defineProperty(crypto, "randomUUID", {value: undefined, configurable: true});
      Object.defineProperty(Array.prototype, "at", {value: undefined, configurable: true});
      Object.defineProperty(Object, "hasOwn", {value: undefined, configurable: true});
    });
  await page.addInitScript(() => {
    window.streamCount = 0;
    const Original = window.EventSource;
    window.EventSource = class extends Original {
      constructor(...args) {
        super(...args);
        window.streamCount++;
        window.testStream = this;
      }
    };
  });
  const config = await (await context.request.get(upstream + "/api/browser/config")).json();
  const inventory = await (
    await context.request.get(upstream + "/api/browser/threads?archived=false")
  ).json();
  const ids = inventory.data.map((t) => t.thread_id);
  assert.equal(ids.length, 6);
  const snapshots = new Map(),
    histories = new Map();
  const message = "Compilation completed with all diagnostics recorded. ".repeat(55);
  for (const id of ids) {
    const template = await (
      await context.request.get(upstream + "/api/browser/sessions/" + id, {
        headers: {"X-Agent-Coord-Thread-Controls": "1", "X-Agent-Coord-History-View": "recent"},
      })
    ).json();
    const turns = Array.from({length: 331}, (_, i) => ({
      id: id + "-turn-" + i,
      status: "completed",
      startedAt: 1700000000 + i * 30,
      items: [
        {id: "user", type: "userMessage", content: [{type: "text", text: "Verify change " + i}]},
        {
          id: "tool" + i,
          type: "commandExecution",
          status: "completed",
          aggregatedOutput: "output record\n".repeat(1200),
          command: "verify",
        },
        {
          id: "a" + i,
          type: "agentMessage",
          text:
            message +
            (i === 330
              ? '\n\n\x60\x60\x60openui\nroot = TextContent("Retained response")\n\x60\x60\x60'
              : ""),
        },
      ],
    }));
    histories.set(id, turns);
    const recent = {
      ...template.thread,
      id,
      turns: turns.slice(-40),
      beforeSeq: 291,
      earlierTurns: 291,
      totalTurns: 331,
      view: "recent",
    };
    snapshots.set(id, {
      ...template,
      thread: undefined,
      recent_turns: recent,
      work_thread: await (await context.request.get(upstream + "/api/browser/threads/" + id)).json(),
      history_revision: 1,
      metadata_revision: 1,
      stream_seq: 0,
      asks: [],
      agent_tree: {root_thread_id: id, data: []},
      sidecars: {watches: [], requests: []},
      schedules: [],
      models: [],
      scratchpad: {text: "", updated_at: null},
    });
  }
  report.recentBytes = Buffer.byteLength(JSON.stringify(snapshots.get(ids[0])));
  const settings = {
    ...config,
    conversationOpen: 1,
    conversationHistoryPages: 1,
    conversationCacheScope: "integration-test",
    conversationRevisionEpoch: "fixture",
    ordinarySubmissionReceipts: 1,
    quietSubmissions: 1,
    conversationMetadataEvents: 1,
    conversationMetadataCursor: 0,
  };
  let submitting = false,
    maximum = 0,
    active = 0,
    interruptAttachmentReply = true;
  const accepted = new Set(),
    submissions = [];
  const newId = "created-conversation";
  let coldOpen, releaseColdOpen;
  // Serve the fixture using real browser requests, including WebKit keepalive
  // delivery on reload. Only the composite and submission contracts are simulated.
  async function serve(request, response) {
    const url = new URL(request.url, upstream),
      route = url.pathname;
    const send = (data, type = "application/json") => {
      const bytes = type === "application/json" ? Buffer.from(JSON.stringify(data)) : Buffer.from(data);
      response.writeHead(200, {"Content-Type": type, "Content-Length": bytes.length});
      response.end(bytes);
    };
    if (route === "/") return send(index, "text/html");
    if (/^\/dist\/app-[a-f0-9]{20}\.(css|js)$/.test(route))
      return send(
        await readFile(join(dist, route.split("/").at(-1))),
        route.endsWith(".css") ? "text/css" : "text/javascript",
      );
    if (route === "/api/browser/config") return send(settings);
    if (route.startsWith("/api/browser/conversations/") && route.endsWith("/open")) {
      if (coldOpen) await coldOpen;
      return send(snapshots.get(route.split("/").at(-2)));
    }
    if (route.startsWith("/api/browser/sessions/") && url.searchParams.has("before_seq")) {
      const id = route.split("/").at(-1),
        end = Number(url.searchParams.get("before_seq")),
        start = Math.max(0, end - 40);
      return send({
        thread: {
          id,
          turns: histories.get(id).slice(start, end),
          beforeSeq: start,
          earlierTurns: start,
          totalTurns: 331,
        },
      });
    }
    if (route === "/api/browser/sessions" && request.method === "POST" && snapshots.has(newId)) {
      request.resume();
      await new Promise((resolve) => setTimeout(resolve, 150));
      return send({session: snapshots.get(newId).session, thread: snapshots.get(newId).recent_turns});
    }
    if (/^\/api\/browser\/sessions\/[^/]+\/messages$/.test(route) && request.method === "POST") {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const body = JSON.parse(Buffer.concat(chunks)),
        id = route.split("/").at(-2);
      submissions.push({id, requestId: body.requestId, text: body.message, images: body.images?.length || 0});
      maximum = Math.max(maximum, ++active);
      if (submitting) await new Promise((resolve) => setTimeout(resolve, 150));
      active--;
      accepted.add(body.requestId);
      if (body.message === "Stored image attachments" && interruptAttachmentReply) {
        interruptAttachmentReply = false;
        // The intake committed, but a gateway failed to return the response.
        response.writeHead(502, {"Content-Type": "application/json"});
        return response.end(JSON.stringify({error: "Lost intake response"}));
      }
      const newTurn = body.message === "First ordered input";
      return send({
        acceptedMessage: {
          threadId: id,
          requestId: body.requestId,
          turnId: id + (newTurn ? "-turn-331" : "-turn-330"),
          message: body.message,
          imageCount: body.images?.length || 0,
          afterItemId: newTurn ? null : "a330",
        },
      });
    }
    const headers = {...request.headers, host: new URL(upstream).host};
    if (headers.origin === base) headers.origin = upstream;
    const upstreamRequest = forwardRequest(url, {method: request.method, headers}, (upstreamResponse) => {
      response.writeHead(upstreamResponse.statusCode, upstreamResponse.headers);
      upstreamResponse.pipe(response);
    });
    upstreamRequest.on("error", () => response.destroy());
    response.on("close", () => upstreamRequest.destroy());
    request.pipe(upstreamRequest);
  }
  server = createServer((request, response) =>
    serve(request, response).catch((error) => {
      report.errors.push(String(error));
      response.destroy();
    }),
  );
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const base = "http://127.0.0.1:" + server.address().port;
  let measureIdle = false;
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith("/api/browser/")) {
      report.requests[path] = (report.requests[path] || 0) + 1;
      if (measureIdle) report.polling.push({method: request.method(), path});
    }
  });
  await page.goto(base + "/#" + ids[0], {waitUntil: "domcontentloaded"});
  await page.waitForFunction(
    () =>
      document.querySelector("#timeline article") &&
      !document.querySelector("#send").disabled &&
      performance.getEntriesByName("workspace-ready").length,
  );
  report.boot = await page.evaluate(() => {
    const script = performance.getEntriesByType("resource").find((e) => /\/dist\/app-.*\.js/.test(e.name));
    return {
      bundleEnd: script.responseEnd,
      domContentLoaded: performance.getEntriesByType("navigation")[0].domContentLoadedEventEnd,
      synchronousEvaluation:
        performance.getEntriesByName("workspace-evaluated")[0]?.startTime - script.responseEnd,
      ready: performance.getEntriesByName("workspace-ready")[0]?.startTime,
    };
  });
  const iframe = page.locator("#timeline article:not([hidden]) iframe[title='Interactive agent response']");
  await iframe.waitFor();
  const responseFrame = page.frames().find((frame) => new URL(frame.url()).pathname === "/openui-frame.html");
  await responseFrame.evaluate(() => {
    window.savedFormSentinel = "retained";
  });
  await page.evaluate(
    () => (window.originalOpenUI = document.querySelector("#timeline article:not([hidden]) iframe")),
  );
  const switchTo = (id) => page.evaluate((id) => select(id, {focus: false, refreshInventory: false}), id);
  await switchTo(ids[1]);
  await switchTo(ids[0]);
  assert.ok(
    await page.evaluate(
      () => window.originalOpenUI === document.querySelector("#timeline article:not([hidden]) iframe"),
    ),
  );
  assert.equal(await responseFrame.evaluate(() => window.savedFormSentinel), "retained");
  report.retainedOpenUIDocument = true;
  report.moveBefore = await page.evaluate(() => typeof Element.prototype.moveBefore === "function");
  await page.waitForTimeout(150);
  for (let iteration = 0; iteration < 40; iteration++) {
    const id = ids[1 - (iteration % 2)];
    const sample = await page.evaluate(async (id) => {
      const tab = [...document.querySelectorAll("#conversation-tabs [data-thread-tab]")].find(
        (el) => el.dataset.threadTab === id,
      );
      const selectTab = tab?.querySelector("button") || tab;
      if (!selectTab) throw Error("Missing conversation tab for " + id);
      const started = performance.now();
      selectTab.click();
      await Promise.resolve();
      const elapsed = performance.now() - started;
      await new Promise(requestAnimationFrame);
      if (
        !document.querySelector("#timeline article") ||
        document.querySelector("#message").disabled ||
        state.selected !== id
      )
        throw Error("Cached tab did not display its conversation");
      return {synchronous: elapsed, beforePaint: performance.now() - started};
    }, id);
    report.switches.push(sample);
  }
  await page.evaluate(() => {
    state.timelineScroll.hold();
    state.timelineScroll.scroller().scrollTop = 0;
  });
  await page.waitForFunction(() => Boolean(window.originalOpenUI.closest("[hidden]")));
  assert.equal(await responseFrame.evaluate(() => window.savedFormSentinel), "retained");
  await page.evaluate(() => state.timelineScroll.latest());
  await page.waitForFunction(
    () => window.originalOpenUI.isConnected && !window.originalOpenUI.closest("[hidden]"),
  );
  assert.equal(await responseFrame.evaluate(() => window.savedFormSentinel), "retained");
  report.virtualizedOpenUIDocument = true;
  submitting = true;
  for (const text of ["First ordered input", "Second ordered input"]) {
    const sample = await page.evaluate((text) => {
      const input = document.getElementById("message");
      input.focus();
      input.value = text;
      input.dispatchEvent(new Event("input", {bubbles: true}));
      const begin = performance.now();
      input.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true}));
      if (
        input.value ||
        ![...document.querySelectorAll("#timeline [data-outgoing]")].some((n) => n.textContent.includes(text))
      )
        throw Error("Optimistic input was not visible before returning from Enter");
      return performance.now() - begin;
    }, text);
    report.optimistic.push(sample);
  }
  await page.waitForFunction(() => document.querySelectorAll("#timeline [data-status='sent']").length >= 2);
  assert.deepEqual(
    submissions.map((s) => s.text),
    ["First ordered input", "Second ordered input"],
  );
  assert.equal(new Set(submissions.map((s) => s.requestId)).size, 2);
  assert.equal(maximum, 1);
  // The immutable receipt anchor can precede tools or agent text. Reopening a
  // cached conversation must replace each echo with its one canonical row.
  await switchTo(ids[1]);
  const acceptedThread = snapshots.get(ids[0]);
  const acceptedTurn = acceptedThread.recent_turns.turns.at(-1);
  const userItem = (id, text) => ({id, type: "userMessage", content: [{type: "text", text}]});
  const newTurn = {
    id: ids[0] + "-turn-331",
    status: "completed",
    items: [userItem("user", submissions[0].text)],
  };
  acceptedTurn.items.push(userItem("additional-user", submissions[1].text));
  histories.get(ids[0]).push(newTurn);
  Object.assign(acceptedThread.recent_turns, {
    turns: [...acceptedThread.recent_turns.turns.slice(1), newTurn],
    totalTurns: 332,
    beforeSeq: 292,
    earlierTurns: 292,
  });
  acceptedThread.acceptedMessages = submissions.map((row, index) => ({
    threadId: ids[0],
    requestId: row.requestId,
    turnId: index ? acceptedTurn.id : newTurn.id,
    message: row.text,
    imageCount: 0,
    afterItemId: index ? "a330" : null,
  }));
  acceptedThread.history_revision++;
  await page.evaluate(
    (id) =>
      window.testStream.dispatchEvent(
        new MessageEvent("message", {
          data: JSON.stringify({
            seq: 1,
            completionSeq: 0,
            events: [
              {
                method: "browser/conversationInvalidated",
                params: {revision: 1, threadIds: [id], topics: ["threads"]},
              },
            ],
          }),
        }),
      ),
    ids[0],
  );
  await switchTo(ids[0]);
  for (const row of submissions)
    assert.equal(await page.locator("#timeline article.message.user").filter({hasText: row.text}).count(), 1);
  assert.equal(await page.locator("#timeline [data-outgoing]").count(), 0);
  report.canonicalEchoes = 2;
  await page.locator("#timeline .timeline-earlier").evaluate((button) => button.click());
  await page.waitForFunction(() => state.detail.thread.turns.length === 80);
  report.renderedRows = await page.locator("#timeline article").count();
  assert.ok(report.renderedRows > 0 && report.renderedRows < 60);
  await page.evaluate(() => {
    goHome({restoreDispatcher: false});
    window.agentCoordPanes.tileCurrent();
  });
  await page.waitForFunction(() => document.querySelectorAll("[data-pane-timeline] article").length > 1);
  report.panes = await page.locator("[data-pane-timeline]").count();
  assert.ok(report.panes >= 2 && report.panes <= 6);
  assert.equal(await page.locator("iframe.conversation-pane-frame").count(), 0);
  assert.equal(await page.evaluate(() => window.streamCount), 1);
  if (output) {
    await mkdir(output, {recursive: true});
    for (const scheme of ["light", "dark"]) {
      await page.emulateMedia({colorScheme: scheme});
      await page.screenshot({path: join(output, "tiled-" + scheme + ".png")});
    }
  }
  await page.evaluate(() => {
    window.agentCoordPanes.leave();
  });
  await switchTo(ids[0]);
  // Four nearly 5 MiB files are already durable before the user presses Enter.
  // Delay the first response, then lose it after intake and recover on reload.
  await page.evaluate(async () => {
    const header =
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a8xkAAAAASUVORK5CYII=";
    const bytes = atob(header).padEnd(5 * 1024 * 1024 - 3, "\0"),
      url = "data:image/png;base64," + btoa(bytes);
    const images = Array.from({length: 4}, (_, i) => ({name: "sample-" + i + ".png", url}));
    state.attachments.drafts.set(state.selected, images);
    state.attachments.render();
    const input = document.getElementById("message");
    input.value = "Stored image attachments";
    input.dispatchEvent(new Event("input", {bubbles: true}));
    await state.durableDrafts.write(state.selected, {text: input.value, images, mentions: []});
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  report.attachment = await page.evaluate(async () => {
    const input = document.getElementById("message");
    const started = performance.now();
    input.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true}));
    const synchronous = performance.now() - started;
    assertVisible();
    await new Promise(requestAnimationFrame);
    assertVisible();
    return {
      synchronous,
      firstFrame: performance.now() - started,
      images: 4,
      bytesPerImage: 5 * 1024 * 1024 - 3,
    };
    function assertVisible() {
      if (
        input.value ||
        !Array.from(document.querySelectorAll("#timeline [data-outgoing]")).some((node) =>
          node.textContent.includes("Stored image attachments"),
        )
      )
        throw Error("Missing optimistic image submission");
    }
  });
  await page.waitForFunction(
    () => document.querySelector("#timeline [data-status='uncertain']"),
    {},
    {timeout: 45000},
  );
  const sentBeforeReload = submissions.at(-1);
  coldOpen = new Promise((resolve) => {
    releaseColdOpen = resolve;
  });
  await page.reload({waitUntil: "domcontentloaded"});
  try {
    await page.waitForFunction(
      () => state.detail?.thread?.turns?.length === 40 && document.querySelector("#timeline article"),
    );
    report.indexedSnapshotOnColdOpen = true;
  } finally {
    releaseColdOpen();
    coldOpen = null;
  }
  await page.waitForFunction(
    () =>
      Array.from(document.querySelectorAll("#timeline [data-status='sent']")).some((node) =>
        node.textContent.includes("Stored image attachments"),
      ),
    {},
    {timeout: 45000},
  );
  assert.equal(submissions.at(-1).requestId, sentBeforeReload.requestId);
  assert.equal(submissions.at(-1).images, 4);
  assert.equal(accepted.size, 3);
  await page.waitForFunction(async () => !(await state.durableDrafts.pending(state.selected)).length);
  report.recoveredAttachment = true;
  await page.waitForFunction(() => !state.activity?.queue.length && !state.activity?.pending);
  await page.waitForTimeout(2500);
  measureIdle = true;
  await page.waitForTimeout(Number(process.env.RIBBON_IDLE_SAMPLE_MS || 60000));
  measureIdle = false;
  report.idleSampleMs = Number(process.env.RIBBON_IDLE_SAMPLE_MS || 60000);
  const sample = snapshots.get(ids[0]);
  snapshots.set(newId, {
    ...sample,
    session: {...sample.session, thread_id: newId},
    recent_turns: {id: newId, turns: [], totalTurns: 0, beforeSeq: 0, earlierTurns: 0},
    work_thread: {...sample.work_thread, thread_id: newId, title: "Created conversation"},
    agent_tree: {root_thread_id: newId, data: []},
  });
  await page.locator("#new-session").evaluate((button) => button.click());
  await page.waitForFunction(
    () => state.selected === "new-session" && !document.getElementById("send").disabled,
  );
  report.firstMessage = await page.evaluate(() => {
    const input = document.getElementById("message"),
      begin = performance.now();
    input.value = "First input in new conversation";
    input.dispatchEvent(new Event("input", {bubbles: true}));
    input.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true}));
    if (input.value || !document.querySelector("#timeline [data-outgoing]"))
      throw Error("New-session input did not appear on Enter");
    const elapsed = performance.now() - begin;
    input.value = "Next unsent input";
    input.dispatchEvent(new Event("input", {bubbles: true}));
    return elapsed;
  });
  await page.waitForFunction(
    (id) =>
      state.selected === id &&
      document.querySelector("#timeline [data-status='sent']") &&
      document.getElementById("message").value === "Next unsent input",
    newId,
  );
  assert.equal(submissions.at(-1).text, "First input in new conversation");
  report.cacheLimits = await page.evaluate(async () => {
    const cache = new ConversationSnapshotCache("bounded-fixture"),
      detail = {thread: {turns: []}, snapshotBytes: 4096};
    for (let index = 0; index < 66; index++) await cache.write("thread-" + index, detail);
    const evicted = await cache.read("thread-0");
    const recent = await cache.read("thread-65");
    const db = await cache.ready;
    const count = await new Promise((resolve, reject) => {
      const request = db.transaction("sizes").objectStore("sizes").count();
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    for (let index = 0; index < 66; index++) conversationEntry("cache-probe-" + index);
    return {
      disk: count,
      memory: state.conversations.size,
      oldestEvicted: evicted === null,
      newestRetained: !!recent,
    };
  });
  assert.deepEqual(report.cacheLimits, {disk: 64, memory: 64, oldestEvicted: true, newestRetained: true});
  report.summary = {
    switchP50: quantile(
      report.switches.map((v) => v.beforePaint),
      0.5,
    ),
    switchP99: quantile(
      report.switches.map((v) => v.beforePaint),
      0.99,
    ),
    optimisticMax: Math.max(...report.optimistic),
    idleGets: report.polling.filter((r) => r.method === "GET").length,
  };
  if (process.env.RIBBON_SCREENSHOTS && output) {
    for (const width of [1440, 390])
      for (const colorScheme of ["light", "dark"]) {
        const mobile = width === 390;
        const capture = await browser.newContext({
          viewport: {width, height: mobile ? 844 : 900},
          isMobile: mobile,
          hasTouch: mobile,
          colorScheme,
        });
        try {
          const screen = await capture.newPage();
          await screen.goto(base + "/#" + ids[0], {waitUntil: "domcontentloaded"});
          await screen.waitForFunction(
            () =>
              performance.getEntriesByName("workspace-ready").length &&
              document.querySelector("#timeline article"),
          );
          await screen.screenshot({path: join(output, `conversation-${width}-${colorScheme}.png`)});
        } finally {
          await capture.close();
        }
      }
  }
  assert.deepEqual(report.errors, []);
  assert.deepEqual(report.polling, []);
} finally {
  if (output) {
    await mkdir(output, {recursive: true});
    await writeFile(join(output, "architecture.json"), JSON.stringify(report, null, 2) + "\n");
  }
  if (browser) await browser.close();
  if (server) {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
  if (fixture.exitCode === null && fixture.signalCode === null) {
    const exit = once(fixture, "exit");
    fixture.kill("SIGTERM");
    await exit;
  }
}
console.log(JSON.stringify(report.summary));
