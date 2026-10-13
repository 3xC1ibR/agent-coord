#!/usr/bin/env node
/* Email the 100 games film through the AgentMail inbox that agent-daemon owns.
 * Uses the daemon's installed SDK and its Keychain credential; the key is never printed.
 *   node demo/hundred-games/email-film.mjs [--to address] [--small]  */
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import { readFileSync, statSync } from "node:fs";
import { createHash } from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire("/opt/projects/agent-daemon/package.json");
const { AgentMailClient } = require("agentmail");
const HERE = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const to = args.includes("--to") ? args[args.indexOf("--to") + 1] : "hi@rquinonez.com";
const INBOX = "daemon-coder@agentmail.to";
const apiKey = execFileSync("security", ["find-generic-password", "-s", "agent-daemon-agentmail-api-key", "-w"], {encoding: "utf8"}).trim();
if (!apiKey) throw new Error("AgentMail API key not found in the login Keychain");
const client = new AgentMailClient({apiKey});

const candidates = args.includes("--file") ? [args[args.indexOf("--file") + 1]] : args.includes("--small") ? ["ribbon-field-hundred-games-720.mp4"] : ["ribbon-field-hundred-games.mp4", "ribbon-field-hundred-games-720.mp4"];
const sha = p => createHash("sha256").update(readFileSync(p)).digest("hex");
let sent = null, lastError = null;
for (const name of candidates) {
  const file = path.join(HERE, "out", name), bytes = statSync(file).size;
  const variant = name.includes("720") ? "720 × 720 compact encode" : name.includes("lean") ? "1080 × 1080 lean encode" : "1080 × 1080 original";
  const text = [
    "Ribbon Field · 100 games (59 seconds, with music)",
    "",
    `Attached: ${name} (${(bytes / 1048576).toFixed(1)} MB, ${variant}).`,
    "One request launches one hundred game agents across OpenAI, Anthropic, Gemini, and Kimi; every chat is closed; one later request wakes the same three agents with their context. Activity condensed; the games are make-believe.",
    "",
    `SHA-256: ${sha(file)}`,
    "Source and validation: /opt/projects/agent-coord/demo/hundred-games/",
    "",
    "Sent because iCloud Drive on the Mac is refusing new uploads. This is a one-way delivery from the agent-daemon inbox; replying to it would start that daemon's Codex email bridge.",
  ].join("\n");
  try {
    sent = await client.inboxes.messages.send(INBOX, {
      to: [to],
      subject: `Ribbon Field · 100 games film (${variant})`,
      text,
      attachments: [{filename: name, contentType: "video/mp4", contentDisposition: "attachment", content: readFileSync(file).toString("base64")}],
    });
    console.log(JSON.stringify({sent: name, bytes, to, messageId: sent.messageId, threadId: sent.threadId}));
    break;
  } catch (error) {
    lastError = error;
    console.error(`send of ${name} failed: ${error?.statusCode ?? ""} ${error?.message ?? error}`.slice(0, 400));
  }
}
if (!sent) { console.error("all attempts failed"); process.exit(1); }
