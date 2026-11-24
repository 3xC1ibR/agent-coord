import assert from "node:assert/strict";
import {createServer} from "node:http";
import {createRequire} from "node:module";
import {fileURLToPath} from "node:url";
import test from "node:test";

const require = createRequire(import.meta.url);
const browsers = require(process.env.RIBBON_PLAYWRIGHT || "../demo/node_modules/playwright");
const library = fileURLToPath(new URL("../plugins/agent-coord/scripts/agent_coord/web/composer-drafts.js", import.meta.url));

for (const engine of process.env.RIBBON_BROWSER ? [process.env.RIBBON_BROWSER] : ["chromium", "webkit"]) {
  test(`${engine} retains draft edits and idempotent staged sends across documents`, async () => {
    const server = createServer((_, response) => response.writeHead(200, {"Content-Type": "text/html"}).end("<!doctype html>"));
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    let browser;
    try {
      browser = await browsers[engine].launch();
      const context = await browser.newContext();
      const reopen = async () => {
        const page = await context.newPage();
        await page.goto(`http://127.0.0.1:${server.address().port}`);
        await page.addScriptTag({path: library});
        return page;
      };
      let page = await reopen();
      const details = await page.evaluate(async () => {
        const store = ComposerDrafts.create({scope:"workspace"});
        const image = suffix => ({name:suffix, url:"data:image/png;base64," + "A".repeat(6_990_500)});
        const attachment = image("submitted");
        await store.write("thread", {text:"submitted", images:[attachment]});
        await Promise.all([
          store.write("thread", {images:[attachment, image("next image")]}),
          store.write("thread", {text:"next text"}),
        ]);
        const body = {requestId:"stable-id", message:"submitted", images:[attachment]};
        await store.stage("thread", "stable-id", {text:"submitted", images:[attachment], body, path:"sessions/thread/messages"});
        await store.stage("thread", "stable-id", {text:"wrong retry", body, path:"wrong"});
        await store.stage("thread", "next-id", {text:"second submission", body:{message:"second submission"}, path:"sessions/thread/messages"});
        return (await store.read("thread")).images.map(item => item.name);
      });
      assert.deepEqual(details, ["next image"]);
      await page.close();
      page = await reopen();
      const recovered = await page.evaluate(async () => {
        const store = ComposerDrafts.create({scope:"workspace"});
        const other = ComposerDrafts.create({scope:"other window"});
        const pending = await store.pending("thread"), draft = await store.read("thread");
        const retained = await store.recover("thread", "stable-id");
        await store.acknowledge("thread", "stable-id");
        await store.acknowledge("thread", "stable-id");
        await store.write("thread", {text:"", images:[]});
        const restored = await store.seed("thread", {text:"old native snapshot", images:pending[0].images});
        const rejected = await store.recover("thread", "next-id");
        return {
          draft: [draft.text, draft.images.map(item => item.name)],
          pending: pending.map(item => [item.requestId, item.text, item.path, item.body.images?.[0]?.url.length || 0]),
          other: await other.read("thread"), retained, restored, rejected,
          final: await store.read("thread"), remaining: (await store.pending("thread")).length,
        };
      });
      assert.deepEqual(recovered.draft, ["next text", ["next image"]]);
      assert.deepEqual(recovered.pending, [
        ["stable-id", "submitted", "sessions/thread/messages", 6_990_522],
        ["next-id", "second submission", "sessions/thread/messages", 0],
      ]);
      assert.equal(recovered.other, null);
      assert.equal(recovered.retained, false);
      assert.deepEqual(recovered.restored, {text:"", mentions:[], images:[]});
      assert.equal(recovered.rejected, true);
      assert.deepEqual(recovered.final, {text:"second submission", mentions:[], images:[]});
      assert.equal(recovered.remaining, 0);
      const moved = await page.evaluate(async () => {
        const store = ComposerDrafts.create({scope:"workspace"});
        const other = ComposerDrafts.create({scope:"other window"});
        const attachment = {name:"next.png", url:"data:image/png;base64,YQ=="};
        await store.write("temporary", {text:"newer input", images:[attachment]});
        await other.write("temporary", {text:"independent window"});
        await store.write("created", {images:[]});
        await store.move("temporary", "created");
        await store.move("temporary", "created");
        const draft = await store.read("created");
        const source = await store.read("temporary");
        await store.stage("created", "in-flight", {text:"old request", body:{message:"old request"}, path:"sessions/created/messages"});
        await store.remove("created");
        return {draft, source, retired:await store.read("created"), pending:(await store.pending("created")).map(row => row.requestId),
          independent:(await other.read("temporary")).text};
      });
      assert.deepEqual(moved, {draft:{text:"newer input", mentions:[], images:[]}, source:null, retired:null,
        pending:["in-flight"], independent:"independent window"});
      const promoted = await page.evaluate(async () => {
        const store = ComposerDrafts.create({scope:"window creation"});
        const project = "new-session:/project";
        const attachment = {name:"submitted.png", url:"data:image/png;base64,YQ=="};
        await store.write(project, {text:"first prompt", images:[attachment]});
        await store.stage(project, "creation", {text:"first prompt", images:[attachment],
          body:{creationIntent:{firstMessageId:"first-message"}}, path:"sessions"});
        await store.write(project, {text:"subsequent edit", images:[]});
        const wire = {requestId:"first-message", body:{message:"first prompt", images:[attachment]}, path:"sessions/created/messages"};
        const first = await store.promoteCreation(project, "creation", "created", wire);
        await store.write("created", {text:"edit after promotion"});
        const repeated = await store.promoteCreation(project, "creation", "created", wire, {text:"stale replacement"});
        return {first, repeated, creation:await store.pending(project), result:await store.pending("created"),
          composerIds:await store.pendingThreadIds(),
          draft:await store.read("created"), previous:await store.read(project)};
      });
      assert.equal(promoted.first.text, "first prompt");
      assert.equal(promoted.first.images[0].name, "submitted.png");
      assert.deepEqual(promoted.repeated, promoted.first);
      assert.deepEqual(promoted.result, [promoted.first]);
      assert.deepEqual(promoted.creation, []);
      assert.deepEqual(promoted.composerIds, ["created"]);
      assert.equal(promoted.previous, null);
      assert.deepEqual(promoted.draft, {text:"edit after promotion", mentions:[], images:[]});
    } finally {
      await browser?.close();
      await new Promise(resolve => server.close(resolve));
    }
  });
}
