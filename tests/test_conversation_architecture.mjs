// Browser-level conversation regression with a disposable server, real app assets,
// and deterministic large histories. RIBBON_WEB_DIST may identify a packaged build.
import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {readFile, mkdir, writeFile} from "node:fs/promises";
import {createRequire} from "node:module";
import {dirname, resolve, join} from "node:path";
import {fileURLToPath} from "node:url";
import {once} from "node:events";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dist = process.env.RIBBON_WEB_DIST || join(root, "plugins/agent-coord/scripts/agent_coord/web/dist");
const {chromium} = createRequire(join(root, "demo/package.json"))("playwright");
const index = await readFile(join(dist, "index.html"), "utf8");
const output = process.env.RIBBON_FRONTEND_EVIDENCE;
const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(AGENT_COORD_|CODEX_|CLAUDE_CODE_|PYTHONPATH$)/.test(key)));
const fixture = spawn(process.env.PYTHON || "python3", ["-Bu", "-c", `
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
`, "--port", "0"], {cwd: root, env, stdio: ["ignore", "pipe", "pipe"]});
let browser;
const report = {switches: [], optimistic: [], errors: [], requests: {}, polling: []};
const quantile = (values, percentile) => [...values].sort((a, b) => a-b)[Math.min(values.length-1, Math.ceil(values.length*percentile)-1)];
try {
  const base = await new Promise((resolve, reject) => {
    let logs = "";
    const timer = setTimeout(() => reject(Error("Fixture startup timed out: " + logs)), 20000);
    fixture.once("error", reject);
    fixture.once("exit", code => { clearTimeout(timer); reject(Error("Fixture exited " + code + ": " + logs)); });
    for (const stream of [fixture.stdout, fixture.stderr]) stream.on("data", data => {
      logs += data;
      const match = logs.match(/Asks fixture: (http:\/\/127\.0\.0\.1:\d+)/);
      if (match) { clearTimeout(timer); resolve(match[1]); }
    });
  });
  browser = await chromium.launch({headless:true, ...(process.env.RIBBON_BROWSER_CHANNEL ? {channel:process.env.RIBBON_BROWSER_CHANNEL} : {})});
  const context = await browser.newContext({viewport:{width:1440,height:900}});
  const page = await context.newPage();
  page.on("pageerror", error => report.errors.push(String(error)));
  page.setDefaultTimeout(15000);
  await page.addInitScript(() => {
    window.streamCount = 0;
    const Original = window.EventSource;
    window.EventSource = class extends Original {
      constructor(...args) { super(...args); window.streamCount++; window.testStream = this; }
    };
  });
  await page.route(base + "/", route => route.fulfill({body:index,contentType:"text/html"}));
  await page.route(base + "/dist/*", async route => {
    const name = new URL(route.request().url()).pathname.split("/").pop();
    await route.fulfill({body:await readFile(join(dist,name)),contentType:name.endsWith(".js")?"text/javascript":"text/css"});
  });
  const config = await (await context.request.get(base + "/api/browser/config")).json();
  const inventory = await (await context.request.get(base + "/api/browser/threads?archived=false")).json();
  const ids = inventory.data.map(t => t.thread_id);
  assert.equal(ids.length, 6);
  const snapshots = new Map(), histories = new Map();
  const message = "Compilation completed with all diagnostics recorded. ".repeat(55);
  for (const id of ids) {
    const template = await (await context.request.get(base + "/api/browser/sessions/" + id, {
      headers:{"X-Agent-Coord-Thread-Controls":"1","X-Agent-Coord-History-View":"recent"}})).json();
    const turns = Array.from({length:331}, (_, i) => ({id:id+"-turn-"+i,status:"completed",startedAt:1700000000+i*30,
      items:[{id:"u"+i,type:"userMessage",content:[{type:"text",text:"Verify change "+i}]},
        {id:"tool"+i,type:"commandExecution",status:"completed",aggregatedOutput:("output record\n").repeat(1200),command:"verify"},
        {id:"a"+i,type:"agentMessage",text:message + (i === 330 ? "\n\n\x60\x60\x60openui\nroot = TextContent(\"Retained response\")\n\x60\x60\x60" : "")}]}));
    histories.set(id,turns);
    const recent = {...template.thread,id,turns:turns.slice(-40),beforeSeq:291,earlierTurns:291,totalTurns:331,view:"recent"};
    snapshots.set(id,{...template,thread:undefined,recent_turns:recent,work_thread:await (await context.request.get(base+"/api/browser/threads/"+id)).json(),
      history_revision:1,metadata_revision:1,stream_seq:0,asks:[],agent_tree:{root_thread_id:id,data:[]},sidecars:{watches:[],requests:[]},schedules:[],models:[],scratchpad:{text:"",updated_at:null}});
  }
  report.recentBytes = Buffer.byteLength(JSON.stringify(snapshots.get(ids[0])));
  const settings = {...config,conversationOpen:1,conversationHistoryPages:1,conversationCacheScope:"integration-test",
    conversationRevisionEpoch:"fixture",ordinarySubmissionReceipts:1,conversationMetadataEvents:1,conversationMetadataCursor:0};
  await page.route(base+"/api/browser/config", route => route.fulfill({json:settings}));
  await page.route(/\/api\/browser\/conversations\/[^/]+\/open$/, async route => {
    const id = new URL(route.request().url()).pathname.split("/").at(-2);
    await route.fulfill({json:snapshots.get(id)});
  });
  await page.route(/\/api\/browser\/sessions\/[^/]+\?before_seq=/, async route => {
    const url = new URL(route.request().url()), id = url.pathname.split("/").at(-1), end = Number(url.searchParams.get("before_seq"));
    const start = Math.max(0,end-40);
    await route.fulfill({json:{thread:{id,turns:histories.get(id).slice(start,end),beforeSeq:start,earlierTurns:start,totalTurns:331}}});
  });
  let submitting = false, maximum = 0, active = 0;
  const submissions = [];
  await page.route(/\/api\/browser\/sessions\/[^/]+\/messages$/, async route => {
    const id = new URL(route.request().url()).pathname.split("/").at(-2), body = route.request().postDataJSON();
    submissions.push({id, requestId:body.requestId, text:body.message});
    maximum = Math.max(maximum, ++active);
    if (submitting) await new Promise(done => setTimeout(done,150));
    active--;
    await route.fulfill({json:{acceptedMessage:{threadId:id,requestId:body.requestId,turnId:id+"-turn-330",message:body.message,imageCount:0,afterItemId:"a330"}}});
  });
  let measureIdle = false;
  page.on("request", request => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith("/api/browser/")) {
      report.requests[path]=(report.requests[path]||0)+1;
      if (measureIdle) report.polling.push({method:request.method(),path});
    }
  });
  await page.goto(base+"/#"+ids[0], {waitUntil:"domcontentloaded"});
  await page.waitForFunction(() => document.querySelector("#timeline article") && !document.querySelector("#send").disabled && performance.getEntriesByName("workspace-ready").length);
  report.boot = await page.evaluate(() => {
    const script = performance.getEntriesByType("resource").find(e => /\/dist\/app-.*\.js/.test(e.name));
    return {bundleEnd:script.responseEnd, domContentLoaded:performance.getEntriesByType("navigation")[0].domContentLoadedEventEnd,
      synchronousEvaluation:performance.getEntriesByName("workspace-evaluated")[0]?.startTime-script.responseEnd,
      ready:performance.getEntriesByName("workspace-ready")[0]?.startTime};
  });
  const iframe = page.locator("#timeline iframe[title='Interactive agent response']");
  await iframe.waitFor();
  await page.evaluate(() => window.originalOpenUI=document.querySelector("#timeline iframe"));
  const switchTo = id => page.evaluate(id => select(id,{focus:false,refreshInventory:false}),id);
  await switchTo(ids[1]); await switchTo(ids[0]);
  assert.ok(await page.evaluate(() => window.originalOpenUI === document.querySelector("#timeline iframe")));
  await page.waitForTimeout(150);
  for(let iteration=0; iteration<40; iteration++) {
    const id=ids[1-iteration%2];
    const sample=await page.evaluate(async id=>{
      const tab=[...document.querySelectorAll("#conversation-tabs [data-thread-tab]")].find(el=>el.dataset.threadTab===id);
      const selectTab=tab?.querySelector("button") || tab;
      if (!selectTab) throw Error("Missing conversation tab for "+id);
      const started=performance.now();selectTab.click();
      await Promise.resolve();
      const elapsed=performance.now()-started;
      await new Promise(requestAnimationFrame);
      if(!document.querySelector("#timeline article") || document.querySelector("#message").disabled || state.selected!==id)
        throw Error("Cached tab did not display its conversation");
      return {synchronous:elapsed,beforePaint:performance.now()-started};
    },id);
    report.switches.push(sample);
  }
  submitting=true;
  for (const text of ["First ordered input","Second ordered input"]) {
    const sample=await page.evaluate(text=>{
      const input=document.getElementById("message");input.focus();input.value=text;
      input.dispatchEvent(new Event("input",{bubbles:true}));
      const begin=performance.now();input.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true,cancelable:true}));
      if(input.value || ![...document.querySelectorAll("#timeline [data-outgoing]")].some(n=>n.textContent.includes(text)))
        throw Error("Optimistic input was not visible before returning from Enter");
      return performance.now()-begin;
    },text);
    report.optimistic.push(sample);
  }
  await page.waitForFunction(()=>document.querySelectorAll("#timeline [data-status='sent']").length>=2);
  assert.deepEqual(submissions.map(s=>s.text),["First ordered input","Second ordered input"]);
  assert.equal(new Set(submissions.map(s=>s.requestId)).size,2);
  assert.equal(maximum,1);
  await page.locator("#timeline .timeline-earlier").evaluate(button=>button.click());
  await page.waitForFunction(()=>state.detail.thread.turns.length===80);
  report.renderedRows=await page.locator("#timeline article").count();
  assert.ok(report.renderedRows>0 && report.renderedRows<60);
  await page.evaluate(()=>{goHome({restoreDispatcher:false});window.agentCoordPanes.tileCurrent();});
  await page.waitForFunction(()=>document.querySelectorAll("[data-pane-timeline] article").length>1);
  report.panes=await page.locator("[data-pane-timeline]").count();
  assert.ok(report.panes>=2 && report.panes<=6);
  assert.equal(await page.locator("iframe.conversation-pane-frame").count(),0);
  assert.equal(await page.evaluate(()=>window.streamCount),1);
  await page.evaluate(()=>{window.agentCoordPanes.leave();});await switchTo(ids[0]);
  await page.waitForFunction(() => !state.activity?.queue.length && !state.activity?.pending);
  await page.waitForTimeout(2500);
  measureIdle=true;
  await page.waitForTimeout(Number(process.env.RIBBON_IDLE_SAMPLE_MS || 60000));
  measureIdle=false;
  report.idleSampleMs=Number(process.env.RIBBON_IDLE_SAMPLE_MS || 60000);
  report.summary={switchP50:quantile(report.switches.map(v=>v.beforePaint),.5),switchP99:quantile(report.switches.map(v=>v.beforePaint),.99),
    optimisticMax:Math.max(...report.optimistic),idleGets:report.polling.filter(r=>r.method==="GET").length};
  assert.deepEqual(report.errors,[]);
  assert.deepEqual(report.polling,[]);
} finally {
  if (output) {await mkdir(output,{recursive:true}); await writeFile(join(output,"architecture.json"), JSON.stringify(report,null,2)+"\n");}
  if (browser) await browser.close();
  if (fixture.exitCode===null && fixture.signalCode===null) {const exit=once(fixture,"exit");fixture.kill("SIGTERM");await exit;}
}
console.log(JSON.stringify(report.summary));
