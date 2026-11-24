// Exercise persistent application documents across every coordinator switch.
import {existsSync, readFileSync, writeFileSync} from "node:fs";
import {createRequire} from "node:module";
import {join} from "node:path";
import {performance} from "node:perf_hooks";

const [directory, moduleName] = process.argv.slice(2);
const {chromium} = createRequire(new URL("../../demo/package.json", import.meta.url))(moduleName);
const {address, threads} = JSON.parse(readFileSync(join(directory, "edge-browser-input.json"), "utf8"));
const origin = new URL(address).origin;
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const now = () => performance.timeOrigin + performance.now();
const stopped = () => existsSync(join(directory, "edge-browser-stop"));
const documentCounts = {};
const activity = [];
const errors = [];
const browser = await chromium.launch({headless: true});

try {
  const context = await browser.newContext();
  await context.route("**/*", route => new URL(route.request().url()).origin === origin
    ? route.continue() : route.abort());

  async function pageFor(name, thread) {
    const page = await context.newPage();
    page.setDefaultTimeout(5000);
    documentCounts[name] = 0;
    page.on("request", request => {
      if (request.isNavigationRequest()) documentCounts[name]++;
    });
    await navigate(page, thread);
    return page;
  }

  async function navigate(page, thread) {
    await page.goto(address + "#" + encodeURIComponent(thread), {waitUntil: "domcontentloaded"});
    const execution = await page.waitForFunction(id => typeof state !== "undefined"
      && state.timelineThread === id && state.detail?.activeTurn || null, thread);
    const activeTurn = await execution.jsonValue();
    await execution.dispose();
    return activeTurn;
  }

  async function send(page, thread, sequence) {
    const marker = `steer-browser-${sequence}`;
    const input = page.locator("#message");
    await input.fill(marker);
    const began = now();
    const [request] = await Promise.all([
      page.waitForRequest(value => value.method() === "POST"
        && new URL(value.url()).pathname === `/api/browser/sessions/${thread}/messages`
        && value.postDataJSON()?.message === marker),
      input.press("Enter"),
    ]);
    const response = await request.response();
    if (!response) throw new Error("Composer response unavailable");
    const value = await response.json();
    const serverTiming = response.headers()["server-timing"]?.match(/^submission;dur=([\d.]+)$/);
    activity.push({kind: "send", began, elapsed: now() - began, thread,
      body: request.postDataJSON(), status: response.status(),
      activeTurn: value.acceptedMessage?.turnId, coordinatorMs: serverTiming ? Number(serverTiming[1]) : null});
  }

  const composers = await Promise.all(threads.map((thread, index) => pageFor(`composer-${index}`, thread)));
  const openers = await Promise.all([0, 1].map(index => pageFor(`opening-${index}`, threads.at(-1))));
  writeFileSync(join(directory, "edge-browser-ready"), "ready\n");

  const pending = new Map();
  function submit(page, operation, sequence) {
    if (pending.has(page)) {
      errors.push({sequence, error: "The previous browser action exceeded its next slot"});
      return;
    }
    pending.set(page, operation().catch(error => {
      errors.push({sequence, error: error.name});
    }).finally(() => pending.delete(page)));
  }

  const start = now();
  for (let sequence = 0; sequence < 400; sequence++) {
    await delay(Math.max(0, start + sequence * 100 - now()));
    if (stopped()) break;
    const index = sequence % threads.length;
    const thread = threads[index];
    submit(composers[index], () => send(composers[index], thread, sequence), sequence);
    if (sequence % 2 === 0) {
      const opener = openers[(sequence / 2) % openers.length];
      submit(opener, async () => {
        const began = now();
        const activeTurn = await navigate(opener, thread);
        activity.push({kind: "open", began, elapsed: now() - began, thread, status: 200, activeTurn});
      }, sequence);
    }
  }
  await Promise.all(pending.values());
  if (!stopped()) errors.push({error: "Coordinator switches exceeded the browser workload time limit"});
  writeFileSync(join(directory, "edge-browser-result.json"), JSON.stringify({activity, errors, documentCounts}));
} finally {
  await browser.close();
}
