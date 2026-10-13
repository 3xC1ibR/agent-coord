#!/usr/bin/env node
/* Drive a mockup in headless Chrome over the DevTools protocol, run a script,
 * and save a screenshot. No dependencies (Node 22 has WebSocket built in).
 *
 *   node probe.js mockups/11-settling-field.html?scale=small "document.querySelector('.mark').click()" out/probe.png
 *
 * The script runs after the page settles; a second optional delay (ms) waits
 * before the screenshot. Prints console errors and the script's result.
 * Inside the script: tap(selector) and drag(selector, dx, dy) send real pointer
 * events; wait(ms) pauses. Return a value to print it. */
"use strict";
const {spawn} = require("node:child_process");
const path = require("node:path");
const fs = require("node:fs");
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const [,, page, script = "", out = "out/probe.png", settle = "900"] = process.argv;
if (!page) { console.error("usage: probe.js <mockup.html?query> [script] [out.png] [delayMs]"); process.exit(2); }
const url = "file://" + path.resolve(__dirname, page);
const port = 9300 + Math.floor(Math.random() * 500);
const chrome = spawn(CHROME, ["--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1", "--window-size=1440,1000", "--remote-debugging-port=" + port, "--user-data-dir=" + fs.mkdtempSync("/tmp/probe-"), "about:blank"], {stdio: "ignore"});
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  let target = null;
  for (let i = 0; i < 40 && !target; i++) { await sleep(150); try { const r = await fetch("http://127.0.0.1:" + port + "/json/new?about:blank", {method: "PUT"}); target = await r.json(); } catch { /* not up yet */ } }
  if (!target) throw new Error("Chrome did not start");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise(r => ws.onopen = r);
  let id = 0; const pending = new Map(); const errors = [];
  ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    if (m.method === "Runtime.exceptionThrown") errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
    if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") errors.push(m.params.args.map(a => a.value || a.description).join(" ")); };
  const send = (method, params = {}) => new Promise(r => { const n = ++id; pending.set(n, r); ws.send(JSON.stringify({id: n, method, params})); });
  await send("Runtime.enable"); await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", {width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false});
  await send("Page.navigate", {url}); await sleep(Number(settle));
  let result = null;
  const helpers = `const _pt = (el, type, x, y) => el.dispatchEvent(new PointerEvent(type, {bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0, buttons: type === "pointerup" ? 0 : 1, pointerId: 1, pointerType: "mouse", isPrimary: true}));
    const _el = sel => typeof sel === "string" ? document.querySelector(sel) : sel;
    const tap = sel => { const el = _el(sel); if (!el) return "no element: " + sel; const r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2; _pt(el, "pointerdown", x, y); _pt(el, "pointerup", x, y); el.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true, clientX: x, clientY: y})); return "tapped"; };
    const drag = (sel, dx, dy, steps = 6) => { const el = _el(sel); if (!el) return "no element: " + sel; const r = el.getBoundingClientRect(); let x = r.left + r.width / 2, y = r.top + r.height / 2; _pt(el, "pointerdown", x, y); for (let i = 1; i <= steps; i++) { _pt(el, "pointermove", x + dx * i / steps, y + dy * i / steps); } _pt(el, "pointerup", x + dx, y + dy); return "dragged"; };
    const wait = ms => new Promise(r => setTimeout(r, ms));`;
  if (script) { const r = await send("Runtime.evaluate", {expression: "(async () => { " + helpers + "\n" + script + " })()", awaitPromise: true, returnByValue: true}); result = r.result?.result?.value ?? r.result?.exceptionDetails?.text ?? null; await sleep(Number(settle)); }
  const shot = await send("Page.captureScreenshot", {format: "png"});
  fs.mkdirSync(path.dirname(path.resolve(__dirname, out)), {recursive: true});
  fs.writeFileSync(path.resolve(__dirname, out), Buffer.from(shot.result.data, "base64"));
  console.log(JSON.stringify({page, result, errors, screenshot: out}));
  ws.close(); chrome.kill();
})().catch(e => { console.error(e); chrome.kill(); process.exit(1); });
