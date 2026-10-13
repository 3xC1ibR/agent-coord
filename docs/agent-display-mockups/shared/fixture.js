/* Shared fixture for the agent-display mockups.
 *
 * One deterministic "field" of agents at three scales (small / medium / large),
 * with the signals Ribbon Field has today (title, phase, summary, next action,
 * links, scope, parent/child, provider, runtime status) plus the signals a
 * richer checkpoint could carry (identity name, role, kind, structured ask,
 * domain/target, plan steps, waiting-on edges, live activity, cost, confidence).
 * A small simulation advances working agents so the mockups can be exercised.
 *
 * Nothing here talks to the real coordination database. */
"use strict";
const AgentField = (() => {
  const BASE_NOW = Date.UTC(2026, 9, 12, 20, 40, 0);
  const MIN = 60_000, HOUR = 3_600_000;
  const PHASES = ["discussion", "investigation", "planning", "orchestrating", "implementation", "validation", "deployment", "finished"];
  const PHASE_LABELS = {discussion: "Discussing", investigation: "Investigating", planning: "Planning", orchestrating: "Orchestrating",
    implementation: "Implementing", validation: "Validating", deployment: "Deploying", finished: "Done"};
  const DOMAINS = ["web", "backend", "mobile", "docs", "infra", "release", "research"];
  const DOMAIN_LABELS = {web: "Web UI", backend: "Backend", mobile: "Mobile", docs: "Docs", infra: "Infra", release: "Release", research: "Research"};
  // Domain scenarios model how well the model reports domains: the clean seven, three coarse ones,
  // fourteen fine ones, a messy mix of synonyms, overlaps, singletons and gaps, or none at all
  // (the UI then derives a cluster key from the write scope, which costs the model nothing).
  const COARSE = {web: "product", mobile: "product", docs: "product", backend: "platform", infra: "platform", release: "platform", research: "research"};
  const FINE = {web: ["overview", "composer"], backend: ["threads", "dispatch"], mobile: ["push", "chat"], docs: ["guides", "reference"], infra: ["build", "ci"], release: ["desktop", "plugin"], research: ["evaluation", "launch"]};
  const SYNONYMS = {web: ["frontend", "UI", "web ui", "Web"], backend: ["api", "server", "services"], mobile: ["iOS", "phone", "Mobile app"], docs: ["documentation", "writing"], infra: ["ci", "devops", "tooling"], release: ["shipping", "deploy"], research: ["analysis", "investigation"]};
  const DOMAIN_SCENARIOS = {
    clean: "7 clean domains", coarse: "3 coarse domains", fine: "14 fine domains", messy: "Messy: synonyms, overlaps, gaps", none: "No domain signal (derive from scope)",
  };
  function scopeKey(a) {
    const first = a.scope[0]; if (!first) return null;
    const parts = first.split("/").filter(Boolean);
    const top = parts[0] === "plugins" && parts.length > 3 ? parts.slice(0, 2).join("/") : parts[0];
    return parts.length > 1 ? top : (first.includes(".") ? "repository root" : top);
  }
  function applyDomainScenario(agents, scenario, byId) {
    const groupLabel = new Map();
    for (const a of agents) {
      const r = mulberry32(hashSeed(a.id + ":" + scenario));
      a.domainReported = a.domain; a.domainSource = "reported";
      let domains;
      if (scenario === "coarse") domains = [COARSE[a.domain] || a.domain];
      else if (scenario === "fine") domains = [a.domain + "/" + (FINE[a.domain] ? FINE[a.domain][Math.floor(r() * FINE[a.domain].length)] : "general")];
      else if (scenario === "none") { const k = scopeKey(a); domains = k ? [k] : []; a.domainSource = k ? "derived" : "missing"; }
      else if (scenario === "messy") {
        const parent = a.parentId && byId.get(a.parentId);
        if (parent && groupLabel.has(parent.id) && r() < 0.7) domains = [...groupLabel.get(parent.id)];
        else {
          const d = r();
          if (d < 0.5) domains = [a.domain];
          else if (d < 0.72) domains = [pick(r, SYNONYMS[a.domain] || [a.domain])];
          else if (d < 0.84) domains = [a.domain, pick(r, DOMAINS.filter(x => x !== a.domain))];
          else if (d < 0.93) domains = [pick(r, [a.target || "misc", "misc", "other", "general"])];
          else { domains = []; a.domainSource = "missing"; }
        }
        groupLabel.set(a.id, domains);
      } else domains = [a.domain];
      a.domains = domains; a.domain = domains[0] || null;
    }
  }
  const ROLES = ["dispatcher", "orchestrator", "implementer", "reviewer", "researcher", "releaser", "delegate", "subagent"];
  const ROLE_LABELS = {dispatcher: "Dispatcher", orchestrator: "Orchestrator", implementer: "Implementer", reviewer: "Reviewer",
    researcher: "Researcher", releaser: "Release specialist", delegate: "Terminal delegate", subagent: "Subagent"};
  const CLIENTS = ["codex", "claude", "antigravity", "kimi"];
  const PROVIDER_NAMES = {codex: "Codex", claude: "Claude Code", antigravity: "Antigravity", kimi: "Kimi"};
  const STATUS_LABELS = {working: "Working", waiting: "Needs you", blocked: "Waiting", idle: "Idle", failed: "Failed", done: "Done"};

  /* Provider logo paths adapted from T3 Code, apps/web/src/components/Icons.tsx (MIT, Copyright (c) 2026 T3 Tools Inc.),
     as already used by the shipped model picker. Google artwork from the Google identity branding sample. */
  const MARKS = {
    codex: {viewBox: "100 100 411 411", path: "M252.794 108.802C289.191 99.0484 326.265 110.305 351.148 135.135C385.113 126.072 422.85 134.862 449.492 161.505C476.136 188.149 484.925 225.888 475.862 259.85V259.854C500.696 284.735 511.95 321.81 502.198 358.207C492.447 394.602 464.161 421.084 430.215 430.217C421.083 464.162 394.603 492.448 358.206 502.199C321.812 511.951 284.734 500.693 259.852 475.864C225.887 484.927 188.15 476.137 161.507 449.495C134.864 422.851 126.073 385.111 135.136 351.149C110.304 326.266 99.0496 289.192 108.801 252.795C118.552 216.4 146.84 189.918 180.784 180.785C189.917 146.841 216.396 118.553 252.794 108.802ZM374.292 407.145C374.292 411.271 372.092 415.086 368.517 417.148L283.723 466.102C302.487 480.585 327.555 486.459 352.217 479.852C386.997 470.532 410.068 439.312 410.555 405.006V317.717C410.555 315.08 409.125 312.621 406.843 311.303L374.292 292.509V407.145ZM251.868 415.897C248.296 417.959 243.893 417.959 240.317 415.897L155.526 366.942C152.366 390.436 159.811 415.08 177.866 433.136H177.863C203.325 458.594 241.896 462.962 271.85 446.232L347.449 402.586C349.735 401.268 351.148 398.8 351.148 396.163V358.579L251.868 415.897ZM368.602 220.628C366.319 219.309 363.474 219.318 361.191 220.637L328.641 239.431L427.921 296.749C431.496 298.811 433.697 302.627 433.697 306.752V404.661C455.622 395.654 473.244 376.881 479.851 352.218C489.169 317.442 473.668 281.85 444.201 264.274L368.602 220.628ZM177.303 206.34C155.377 215.348 137.756 234.122 131.148 258.783C121.832 293.561 137.331 329.153 166.799 346.727L242.398 390.373C244.68 391.692 247.525 391.684 249.807 390.366L282.357 371.572L183.078 314.253C179.504 312.189 177.303 308.375 177.303 304.251V206.34ZM259.849 279.145V331.858L305.5 358.213L351.15 331.858V279.145L305.5 252.789L259.849 279.145ZM327.276 144.9C308.512 130.418 283.445 124.543 258.782 131.15C224.002 140.471 200.931 171.691 200.445 205.995V293.286C200.445 295.923 201.875 298.381 204.158 299.7L236.707 318.493V203.856C236.707 199.731 238.909 195.916 242.483 193.853L327.276 144.9ZM433.137 177.867C407.675 152.407 369.103 148.038 339.149 164.769L263.55 208.415C261.265 209.734 259.852 212.202 259.852 214.838V252.423L359.132 195.105C362.703 193.041 367.108 193.041 370.682 195.105L455.473 244.06C458.635 220.567 451.189 195.922 433.135 177.867H433.137Z"},
    claude: {viewBox: "0 0 256 257", path: "m50.228 170.321 50.357-28.257.843-2.463-.843-1.361h-2.462l-8.426-.518-28.775-.778-24.952-1.037-24.175-1.296-6.092-1.297L0 125.796l.583-3.759 5.12-3.434 7.324.648 16.202 1.101 24.304 1.685 17.629 1.037 26.118 2.722h4.148l.583-1.685-1.426-1.037-1.101-1.037-25.147-17.045-27.22-18.017-14.258-10.37-7.713-5.25-3.888-4.925-1.685-10.758 7-7.713 9.397.649 2.398.648 9.527 7.323 20.35 15.75L94.817 91.9l3.889 3.24 1.555-1.102.195-.777-1.75-2.917-14.453-26.118-15.425-26.572-6.87-11.018-1.814-6.61c-.648-2.723-1.102-4.991-1.102-7.778l7.972-10.823L71.42 0 82.05 1.426l4.472 3.888 6.61 15.101 10.694 23.786 16.591 32.34 4.861 9.592 2.592 8.879.973 2.722h1.685v-1.556l1.36-18.211 2.528-22.36 2.463-28.776.843-8.1 4.018-9.722 7.971-5.25 6.222 2.981 5.12 7.324-.713 4.73-3.046 19.768-5.962 30.98-3.889 20.739h2.268l2.593-2.593 10.499-13.934 17.628-22.036 7.778-8.749 9.073-9.657 5.833-4.601h11.018l8.1 12.055-3.628 12.443-11.342 14.388-9.398 12.184-13.48 18.147-8.426 14.518.778 1.166 2.01-.194 30.46-6.481 16.462-2.982 19.637-3.37 8.88 4.148.971 4.213-3.5 8.62-20.998 5.184-24.628 4.926-36.682 8.685-.454.324.519.648 16.526 1.555 7.065.389h17.304l32.21 2.398 8.426 5.574 5.055 6.805-.843 5.184-12.962 6.611-17.498-4.148-40.83-9.721-14-3.5h-1.944v1.167l11.666 11.406 21.387 19.314 26.767 24.887 1.36 6.157-3.434 4.86-3.63-.518-23.526-17.693-9.073-7.972-20.545-17.304h-1.36v1.814l4.73 6.935 25.017 37.59 1.296 11.536-1.814 3.76-6.481 2.268-7.13-1.297-14.647-20.544-15.1-23.138-12.185-20.739-1.49.843-7.194 77.448-3.37 3.953-7.778 2.981-6.48-4.925-3.436-7.972 3.435-15.749 4.148-20.544 3.37-16.333 3.046-20.285 1.815-6.74-.13-.454-1.49.194-15.295 20.999-23.267 31.433-18.406 19.702-4.407 1.75-7.648-3.954.713-7.064 4.277-6.286 25.47-32.405 15.36-20.092 9.917-11.6-.065-1.686h-.583L44.07 198.125l-12.055 1.555-5.185-4.86.648-7.972 2.463-2.593 20.35-13.999-.064.065Z"},
    antigravity: {viewBox: "10 10 20 20", paths: [{path: "M29.6 20.2273C29.6 19.5182 29.5364 18.8364 29.4182 18.1818H20V22.05H25.3818C25.15 23.3 24.4455 24.3591 23.3864 25.0682V27.5773H26.6182C28.5091 25.8364 29.6 23.2727 29.6 20.2273Z", fill: "#4285F4"}, {path: "M20 30C22.7 30 24.9636 29.1045 26.6181 27.5773L23.3863 25.0682C22.4909 25.6682 21.3454 26.0227 20 26.0227C17.3954 26.0227 15.1909 24.2636 14.4045 21.9H11.0636V24.4909C12.7091 27.7591 16.0909 30 20 30Z", fill: "#34A853"}, {path: "M14.4045 21.9C14.2045 21.3 14.0909 20.6591 14.0909 20C14.0909 19.3409 14.2045 18.7 14.4045 18.1V15.5091H11.0636C10.3864 16.8591 10 18.3864 10 20C10 21.6136 10.3864 23.1409 11.0636 24.4909L14.4045 21.9Z", fill: "#FBBC04"}, {path: "M20 13.9773C21.4681 13.9773 22.7863 14.4818 23.8227 15.4727L26.6909 12.6045C24.9591 10.9909 22.6954 10 20 10C16.0909 10 12.7091 12.2409 11.0636 15.5091L14.4045 18.1C15.1909 15.7364 17.3954 13.9773 20 13.9773Z", fill: "#E94235"}]},
    kimi: {viewBox: "0 0 24 24", path: "M4 3h4v7l8-7h5l-10 9 10 9h-5l-8-7v7H4z"},
  };
  function providerLogo(client, label = null) {
    const mark = MARKS[client]; if (!mark) return null;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", mark.viewBox); svg.setAttribute("class", "provider-logo provider-logo-" + client);
    if (label) { svg.setAttribute("role", "img"); svg.setAttribute("aria-label", label); } else svg.setAttribute("aria-hidden", "true");
    for (const part of mark.paths || [{path: mark.path}]) {
      const p = document.createElementNS(svg.namespaceURI, "path");
      p.setAttribute("d", part.path); p.setAttribute("fill-rule", "evenodd"); p.setAttribute("fill", part.fill || "currentColor"); svg.append(p);
    }
    return svg;
  }

  function mulberry32(seed) {
    let a = seed >>> 0;
    return () => { a |= 0; a = a + 0x6D2B79F5 | 0; let t = Math.imul(a ^ a >>> 15, 1 | a); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; };
  }
  const pick = (rand, list) => list[Math.floor(rand() * list.length)];
  const weighted = (rand, pairs) => { const total = pairs.reduce((s, [, w]) => s + w, 0); let r = rand() * total; for (const [v, w] of pairs) { r -= w; if (r <= 0) return v; } return pairs[pairs.length - 1][0]; };

  const NAMES = ["Gravity", "Pager", "Quill", "Tandem", "Mica", "Juniper", "Ferry", "Lantern", "Ocelot", "Harbor", "Sable", "Comet", "Tallow", "Cedar", "Bramble", "Heron", "Marlow", "Pike", "Rook", "Sorrel", "Vesper", "Wren", "Yarrow", "Zephyr", "Alder", "Basil", "Cobalt", "Dune", "Ember", "Fennel", "Garnet", "Hollis", "Indigo", "Jasper", "Kestrel", "Linden", "Moss", "Nettle", "Orchid", "Pebble"];
  const OBJECTS = {
    web: ["thread hover preview", "saved view tabs", "composer attachments", "attention panel", "phase board scrolling", "keyboard navigation", "dispatcher rail", "thread search ranking", "pinned section", "notification toasts"],
    backend: ["checkpoint storage", "message delivery receipts", "reply classification", "write-scope leases", "SSE event stream", "thread preview cache", "database contention handling", "dispatch routing", "schedule runner", "session registry"],
    mobile: ["cold-launch push", "mobile composer", "home-screen install", "offline draft recovery", "push subscription renewal", "phone layout for tiles", "voice input", "haptic feedback", "mobile search", "badge counts"],
    docs: ["dispatcher reference", "orchestration guide", "remote access docs", "README quick start", "native push notes", "skill guidance", "release notes", "theory of attention", "thread panes guide", "competitor comparison"],
    infra: ["launchd deployment job", "macOS build pipeline", "cachebuster helper", "plugin marketplace refresh", "test runner flakiness", "code signing", "crash log capture", "log rotation", "remote tunnel health", "demo video pipeline"],
    release: ["combined desktop build", "mobile beta", "plugin version bump", "release audit", "rollback plan", "smoke test matrix", "release notes draft", "signing certificate renewal", "install verification", "restart recovery"],
    research: ["automation evaluation program", "owner context gap", "idle orchestrator decision", "launch positioning", "pricing experiment", "onboarding drop-off", "competitor install flows", "attention count semantics", "classifier confidence", "retention signals"],
  };
  const VERBS = {web: ["Fix", "Refine", "Add", "Speed up", "Restyle"], backend: ["Harden", "Fix", "Refactor", "Add", "Instrument"], mobile: ["Fix", "Add", "Recover", "Polish", "Verify"],
    docs: ["Write", "Update", "Rewrite", "Trim", "Review"], infra: ["Repair", "Automate", "Stabilize", "Audit", "Migrate"], release: ["Prepare", "Verify", "Ship", "Audit", "Stage"], research: ["Explain", "Find", "Compare", "Assess", "Measure"]};
  const TARGETS = {web: ["Overview", "Thread view", "Composer", "Sidebar", "Dispatcher workspace"], backend: ["threads.py", "attention.py", "store.py", "codex_app_server.py", "dispatch_runtime.py"],
    mobile: ["Mobile chat", "Push worker", "PWA shell", "Mobile overview", "Speech input"], docs: ["README", "docs/dispatch.md", "SKILL.md", "docs/native-push.md", "docs/remote-access.md"],
    infra: ["desktop/macos/build.py", "scripts/", "launchd", "tests/", "demo/"], release: ["combined-qd9", "mobile-beta-3", "plugin 0.9.4", "marketplace", "macOS app"], research: ["rig evaluation", "launch research", "classifier", "onboarding", "pricing"]};
  const SCOPES = {web: ["plugins/agent-coord/scripts/agent_coord/web/app.js", "plugins/agent-coord/scripts/agent_coord/web/styles.css", "plugins/agent-coord/scripts/agent_coord/web/views.js", "plugins/agent-coord/scripts/agent_coord/web/thread-hover.js", "tests/test_web_views.js"],
    backend: ["plugins/agent-coord/scripts/agent_coord/threads.py", "plugins/agent-coord/scripts/agent_coord/attention.py", "plugins/agent-coord/scripts/agent_coord/store.py", "plugins/agent-coord/scripts/agent_coord/codex_app_server.py", "tests/test_threads.py"],
    mobile: ["mobile/**", "plugins/agent-coord/scripts/agent_coord/web/mobile-chat.js", "plugins/agent-coord/scripts/agent_coord/web/push-worker.js", "plugins/agent-coord/scripts/agent_coord/web_push.py", "tests/test_web_push.py"],
    docs: ["README.md", "docs/dispatch.md", "plugins/agent-coord/skills/agent-coordination/SKILL.md", "docs/native-push.md", "docs/remote-access.md"],
    infra: ["desktop/macos/build.py", "scripts/**", "tests/test_macos_app.py", "demo/render.js", ".github/**"],
    release: ["build/macos/**", "plugins/agent-coord/.codex-plugin/plugin.json", "plugins/agent-coord/.claude-plugin/plugin.json", "desktop/macos/**", "docs/release-notes.md"],
    research: ["docs/launch-research-2026-10-07.md", "docs/dispatch-evaluation.md", "docs/agent-dispatch-findings/**", "docs/feature-ideas.md", "docs/theory-of-attention.md"]};
  // Runtime-derived activity: the provider stream already reports tool calls (read, edit, run, search).
  const TOOL_KINDS = ["read", "edit", "run", "search"];
  const hashSeed = text => { let h = 2166136261; for (const ch of text) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); } return h >>> 0; };
  const toolFor = (a, rand2) => {
    const file = a.scope[0] ? (a.scope[0].endsWith("/**") ? a.scope[0].slice(0, -2) : a.scope[0].split("/").filter(Boolean).pop()) : null;
    const kind = a.role === "researcher" || a.role === "reviewer" ? pick(rand2, ["read", "read", "search"]) : pick(rand2, TOOL_KINDS);
    const label = kind === "run" ? pick(rand2, ["python3 -m unittest " + (file ? file.replace(/\.py$/, "") : "tests"), "node tests/" + (file || "test_web_views.js"), "npm test", "./render.sh"])
      : kind === "search" ? "grep " + pick(rand2, ["checkpoint_stale", "next_actor", "response_state", "write_scope", "attention_reason"])
      : (file || pick(rand2, ["README.md", "app.js", "threads.py", "styles.css"]));
    return {kind, label};
  };
  const ACTIVITIES = ["Reading the current implementation", "Running the focused test suite", "Writing tests for the new path", "Reproducing the reported defect", "Editing the implementation", "Checking the diff against the plan", "Waiting for a command to finish", "Reading tests and fixtures", "Updating the checkpoint", "Comparing two approaches"];
  const ASKS = {
    decision: [{question: "Two approaches are viable for {target}: keep the current retry policy or move to chunked retries. Which do you prefer?", options: [["keep", "Keep current policy"], ["chunked", "Chunked retries"], ["either", "Your call"]]},
      {question: "The {target} work touches a shared helper. Extract it now, or leave it for a follow-up?", options: [["extract", "Extract now"], ["later", "Follow-up"]]},
      {question: "Should {target} keep backward compatibility with saved views from 0.9.2?", options: [["keep", "Keep compatible"], ["drop", "Drop old format"]]}],
    approval: [{question: "Ready to deploy {target}. Deploy now?", options: [["deploy", "Deploy now"], ["wait", "Wait for handoff"], ["diff", "Show me the diff first"]]},
      {question: "Push the {target} branch to the shared remote?", options: [["push", "Push"], ["local", "Keep local"]]}],
    information: [{question: "Which workspace should the migrated {target} files land in?", freeText: true, options: []},
      {question: "What is the expected cold-launch delay budget for {target} on an iPhone 12?", freeText: true, options: []}],
    access: [{question: "Pushing {target} needs write access to the rig remote. Grant it, or push from your machine?", options: [["grant", "Grant for this task"], ["manual", "I’ll push"], ["skip", "Skip pushing"]]}],
    review: [{question: "Review of {target} found three defects, fixed in place. Approve closing the review?", options: [["approve", "Approve"], ["diffs", "Show the diffs"], ["open", "Keep it open"]]},
      {question: "The {target} change is ready. Review the integrated diff before I merge?", options: [["review", "I’ll review"], ["merge", "Merge without me"]]}],
    failure: [{question: "Provider returned rate-limit errors for six minutes on {target}. Retry, or hand it off?", options: [["retry", "Retry"], ["handoff", "Hand off to Claude"], ["close", "Close"]]},
      {question: "The {target} test run crashed the simulator twice. Retry, or stop here?", options: [["retry", "Retry"], ["stop", "Stop"]]}]};
  const EXTERNALS = ["GitHub Actions run 4411", "the App Store build processing", "Jev classifier rollout", "the staging database restore", "TestFlight review", "CI run 4419"];
  const PLAN_STEPS = {
    implementer: ["Read the current code and tests", "Write a failing test", "Implement the change", "Run the focused suite", "Update docs and checkpoint"],
    reviewer: ["Read the diff and surrounding code", "Reproduce reported issues", "Write findings with file references", "Confirm fixes", "Close the review"],
    researcher: ["Collect the relevant threads and documents", "Trace the decision history", "Write up findings", "Confirm with evidence"],
    releaser: ["Verify the build artifacts", "Run the install smoke test", "Deploy and restart", "Verify runtime health", "Record the audit"],
    orchestrator: ["Break the objective into assignments", "Create or reuse specialists", "Integrate contributions", "Validate the integrated result", "Deliver"],
    delegate: ["Start the watch", "Poll until the run completes", "Report the result"],
    subagent: ["Search the relevant files", "Summarize findings"],
    dispatcher: ["Route incoming requests"]};

  const PHASE_SUMMARY = {discussion: "Discussed {obj} with the user and agreed on the goal.", investigation: "Read the current code and history for {obj}.",
    planning: "Planned the approach for {obj} and listed the steps.", orchestrating: "Coordinating specialists for {obj}.", implementation: "Implementing {obj}; focused tests are passing so far.",
    validation: "Validating {obj} against the plan and the reported defects.", deployment: "Deploying {obj} and watching runtime health.", finished: "Completed {obj}."};

  function fill(text, agent) { return text.replaceAll("{target}", agent.target || agent.title).replaceAll("{obj}", lower(agent.title)); }
  function lower(title) { return title.charAt(0).toLowerCase() + title.slice(1); }

  function makeAgent(spec, rand, now = BASE_NOW) {
    const a = Object.assign({
      name: null, role: "implementer", kind: "app", client: "codex", parentId: null, phase: "implementation", status: "working",
      summary: "", activity: null, next: {actor: "agent", action: ""}, ask: null, scope: [], domain: "web", target: null,
      plan: [], waitingOn: [], links: [], cost: {tokens: 0, usd: 0, minutes: 0}, confidence: 0.8,
      createdAt: BASE_NOW - 90 * MIN, updatedAt: BASE_NOW - 5 * MIN, checkpointAt: null, stale: false, unread: false, pinned: false, history: [], failure: null,
    }, spec);
    a.createdAt = Math.min(a.createdAt, now - (now === BASE_NOW ? 2 * MIN : 0));
    if (a.updatedAt < a.createdAt) a.updatedAt = a.createdAt;
    a.checkpointAt ??= a.updatedAt;
    if (!a.target) a.target = pick(rand, TARGETS[a.domain]);
    if (!a.plan.length) {
      const steps = PLAN_STEPS[a.role] || PLAN_STEPS.implementer;
      const fraction = a.status === "done" ? 1 : a.phase === "validation" ? 0.8 : a.phase === "deployment" ? 0.9 : a.phase === "implementation" ? 0.4 + rand() * 0.3 : a.phase === "planning" ? 0.1 : a.phase === "orchestrating" ? 0.4 : 0.3;
      const done = a.status === "done" ? steps.length : Math.min(steps.length - 1, Math.round(steps.length * fraction));
      a.plan = steps.map((label, i) => ({label, state: i < done ? "done" : i === done && a.status !== "done" ? "active" : "todo"}));
      if (a.status === "waiting" && a.plan[done]) a.plan[done].state = "blocked";
    }
    if (!a.summary) a.summary = fill(PHASE_SUMMARY[a.phase], a);
    if (a.status === "working" && !a.activity) a.activity = pick(rand, ACTIVITIES);
    if (a.status === "done") { a.phase = "finished"; a.next = {actor: "nobody", action: ""}; }
    if (!a.cost.minutes) { a.cost.minutes = Math.max(2, Math.round((a.updatedAt - a.createdAt) / MIN)); a.cost.tokens = a.cost.minutes * (1800 + Math.floor(rand() * 2600)); a.cost.usd = Math.round(a.cost.tokens / 1000 * 0.9) / 100; }
    // Messages: how much the user has been in this conversation (a free runtime signal).
    const rand2 = mulberry32(hashSeed(a.id));
    const baseTurns = a.role === "dispatcher" ? 8 : a.role === "orchestrator" ? 3 : a.kind !== "app" ? 0 : 1;
    a.userTurns = spec.userTurns ?? Math.max(a.kind === "app" ? 1 : 0, Math.round(baseTurns + Math.min(5, a.cost.minutes / 70) + rand2() * 2.4));
    a.turns = spec.turns ?? a.userTurns + Math.min(48, Math.round(a.cost.minutes / 6)) + 1;
    if (!a.history.length) {
      const idx = PHASES.indexOf(a.phase);
      const passed = PHASES.slice(1, idx).filter(p => p !== "orchestrating" || a.role === "orchestrator").filter(p => !(p === "deployment" && a.role !== "releaser")).filter(p => a.role === "orchestrator" ? !["implementation", "planning"].includes(p) : true);
      const span = a.checkpointAt - a.createdAt;
      a.history = passed.map((phase, i) => ({at: a.createdAt + span * (i + 1) / (passed.length + 1), phase, summary: fill(PHASE_SUMMARY[phase], a), actor: "agent"}));
      a.history.push({at: a.checkpointAt, phase: a.phase, summary: a.summary, actor: "agent"});
    }
    if (a.ask) { a.ask.askedAt ??= a.updatedAt; a.ask.blocking ??= true; a.next = {actor: "user", action: a.ask.question}; }
    // Tool-call pulses over the last 90 minutes, from the same per-agent generator so the main sequence is unchanged.
    const until = a.status === "working" ? now : a.status === "waiting" ? (a.ask?.askedAt || a.updatedAt) : a.updatedAt;
    const from = Math.max(a.createdAt, now - 90 * MIN);
    a.pulses = [];
    for (let t = from + rand2() * 2 * MIN; t < until; t += (a.status === "blocked" ? 6 + rand2() * 10 : 1 + rand2() * 3) * MIN) a.pulses.push(Math.round(t));
    if (a.status === "working" && (!a.pulses.length || until - a.pulses[a.pulses.length - 1] > 3 * MIN)) a.pulses.push(Math.max(a.createdAt, now - Math.round(rand2() * 2 * MIN)));
    const tool = toolFor(a, rand2);
    a.lastTool = {...tool, at: a.pulses.length ? a.pulses[a.pulses.length - 1] : a.updatedAt};
    a._rand2 = rand2;
    return a;
  }

  function smallSet(rand) {
    const T = BASE_NOW;
    const specs = [
      {id: "s00", userTurns: 14, title: "Dispatcher", name: "Dispatcher", role: "dispatcher", client: "codex", phase: "orchestrating", status: "idle", domain: "research", target: "agent-coord",
        summary: "Routed the last three requests to existing specialists. Nothing is queued.", next: {actor: "nobody", action: ""}, createdAt: T - 30 * HOUR, updatedAt: T - 41 * MIN, pinned: true, plan: [{label: "Route incoming requests", state: "active"}]},
      {id: "s01", userTurns: 9, title: "Procurement Evaluation Readiness", role: "orchestrator", client: "claude", phase: "orchestrating", status: "blocked", domain: "release", target: "run-002", pinned: true,
        summary: "Run-002 is held until the workspace upload release is deployed and verified, following the owner-approved wait. The intake repair is deployed and Rig is healthy.",
        next: {actor: "agent", action: "The upload owner completes rig-p4me and sends the deployment, health, and API handoff; this agent then verifies delivery and coordinates run-002."},
        waitingOn: ["s02"], scope: ["rig/evaluation/**"], createdAt: T - 6 * HOUR, updatedAt: T - 62 * MIN, links: [{kind: "bead", label: "run-002", target: "rig-vdid"}]},
      {id: "s02", userTurns: 6, title: "Rig workspace file uploads", name: "Harbor", role: "orchestrator", client: "codex", phase: "orchestrating", status: "waiting", domain: "backend", target: "Upload API", parentId: "s01",
        summary: "Resolved the scope overlap with the concurrent Program task-request work by repairing Gravity’s narrowed declaration to separate upload file paths. Gravity is active again; the UI half is done.",
        ask: {kind: "approval", question: "The backend and UI halves are integrated. Deploy the upload release to Rig now, or wait for the API handoff from Procurement?", options: [{id: "deploy", label: "Deploy now"}, {id: "wait", label: "Wait for handoff"}, {id: "look", label: "Let me look first"}], blocking: true, askedAt: T - 5 * MIN},
        waitingOn: ["s03"], scope: ["rig/api/uploads/**", "rig/web/upload/**"], createdAt: T - 5 * HOUR, updatedAt: T - 5 * MIN, unread: true, links: [{kind: "bead", label: "Uploads", target: "rig-p4me"}, {kind: "branch", label: "feature/uploads", target: "feature/uploads"}]},
      {id: "s03", title: "Workspace upload implementation", name: "Gravity", role: "implementer", client: "antigravity", phase: "implementation", status: "working", domain: "backend", target: "Upload API", parentId: "s02",
        summary: "Implementing chunked upload with resumable sessions. The storage adapter and three of five integration tests are done.", activity: "Running the upload integration tests",
        plan: [{label: "Read the current upload path", state: "done"}, {label: "Add resumable session table", state: "done"}, {label: "Implement chunked writes", state: "done"}, {label: "Integration tests", state: "active"}, {label: "Update API docs", state: "todo"}],
        scope: ["rig/api/uploads/**", "rig/tests/uploads/**"], createdAt: T - 4 * HOUR, updatedAt: T - 2 * MIN, links: [{kind: "pull_request", label: "#412", target: "https://example.invalid/rig/pull/412"}]},
      {id: "s04", title: "Workspace upload UI", role: "implementer", client: "codex", phase: "finished", status: "done", domain: "web", target: "Upload page", parentId: "s02",
        summary: "Completed the upload page: drag-and-drop, progress, and failure recovery. 14 tests pass.", createdAt: T - 4 * HOUR, updatedAt: T - 48 * MIN, unread: true, scope: ["rig/web/upload/**"],
        links: [{kind: "pull_request", label: "#409", target: "https://example.invalid/rig/pull/409"}]},
      {id: "s05", userTurns: 2, title: "Correctness review rig-xef1.2", name: "Rook", role: "reviewer", client: "codex", phase: "validation", status: "waiting", domain: "backend", target: "program_updates.py", parentId: "s01",
        summary: "Completed the integrated correctness review. Three reproduced defects were fixed in place; all 123 focused tests and eight reviewer reproductions pass.",
        ask: {kind: "review", question: "No blocking findings remain. Approve closing rig-xef1.2?", options: [{id: "approve", label: "Approve"}, {id: "diffs", label: "Show the diffs"}, {id: "open", label: "Keep it open"}], blocking: false, askedAt: T - 22 * MIN},
        scope: [], createdAt: T - 3 * HOUR, updatedAt: T - 22 * MIN, links: [{kind: "bead", label: "Correctness review", target: "rig-xef1.2"}]},
      {id: "s06", title: "Find automation evaluation program", role: "researcher", client: "claude", phase: "investigation", status: "idle", domain: "research", target: "rig evaluation",
        summary: "Found the UTC-clock automation lifecycle prompts from September 19–20 and benchmark 007, the autonomous email workflow used for the Automation MVP evaluation.", next: {actor: "nobody", action: ""},
        createdAt: T - 2 * HOUR, updatedAt: T - 64 * MIN, unread: true, plan: [{label: "Collect the relevant threads and documents", state: "done"}, {label: "Trace the decision history", state: "done"}, {label: "Write up findings", state: "done"}]},
      {id: "s07", title: "Explain missing owner context", role: "researcher", client: "claude", phase: "investigation", status: "idle", domain: "research", target: "context builder",
        summary: "Confirmed that the experiment preference was durably saved on September 19 but absent from the October 12 model request. The context builder includes formal owner decisions only.", next: {actor: "nobody", action: ""},
        createdAt: T - 2 * HOUR, updatedAt: T - 66 * MIN, plan: [{label: "Collect the relevant threads and documents", state: "done"}, {label: "Trace the decision history", state: "done"}, {label: "Write up findings", state: "done"}]},
      {id: "s08", title: "Explain idle orchestrator decision", role: "researcher", client: "claude", phase: "investigation", status: "idle", domain: "research", target: "orchestrator",
        summary: "Traced the exact October 12 status turn: four successful model calls inspected empty work and degraded pursuit, then acknowledged the old hold without requesting strategy.", next: {actor: "nobody", action: ""},
        createdAt: T - 2 * HOUR, updatedAt: T - 70 * MIN, plan: [{label: "Collect the relevant threads and documents", state: "done"}, {label: "Trace the decision history", state: "done"}, {label: "Write up findings", state: "done"}]},
      {id: "s09", userTurns: 5, title: "Mobile push cold launch", name: "Pager", role: "implementer", client: "claude", phase: "implementation", status: "working", domain: "mobile", target: "Push worker",
        summary: "Recovering notification destinations after a cold mobile launch. The subscription is re-registered before the first poll; writing the regression test now.", activity: "Writing the cold-launch regression test",
        plan: [{label: "Reproduce the lost destination", state: "done"}, {label: "Re-register subscription on launch", state: "done"}, {label: "Regression test", state: "active"}, {label: "Verify on a device", state: "todo"}],
        scope: ["plugins/agent-coord/scripts/agent_coord/web/push-worker.js", "plugins/agent-coord/scripts/agent_coord/web_push.py", "tests/test_web_push.py"], createdAt: T - 100 * MIN, updatedAt: T - 3 * MIN},
      {id: "s10", title: "Explore push worker code", role: "subagent", kind: "subagent", client: "claude", phase: "finished", status: "done", domain: "mobile", target: "Push worker", parentId: "s09",
        summary: "Completed: mapped the subscription lifecycle across push-worker.js and web_push.py.", createdAt: T - 80 * MIN, updatedAt: T - 60 * MIN, plan: [{label: "Search the relevant files", state: "done"}, {label: "Summarize findings", state: "done"}]},
      {id: "s11", title: "Write cold-launch tests", role: "subagent", kind: "subagent", client: "claude", phase: "implementation", status: "working", domain: "mobile", target: "tests/test_web_push.py", parentId: "s09",
        summary: "Writing two regression tests for destination recovery.", activity: "Running tests/test_web_push.py", createdAt: T - 12 * MIN, updatedAt: T - 1 * MIN, plan: [{label: "Write the regression tests", state: "active"}, {label: "Run the suite", state: "todo"}]},
      {id: "s12", userTurns: 4, title: "Ribbon Field release combined-qd9", name: "Release", role: "releaser", client: "codex", phase: "deployment", status: "waiting", domain: "release", target: "combined-qd9",
        summary: "Build combined-qd9 is verified: install smoke test passed and 172 conversations were preserved in the dry run.",
        ask: {kind: "approval", question: "Install build combined-qd9 and restart Ribbon Field? Open conversations will reconnect after the restart.", options: [{id: "now", label: "Install now"}, {id: "tonight", label: "Tonight at 11pm"}, {id: "cancel", label: "Cancel"}], blocking: true, askedAt: T - 14 * MIN},
        scope: ["build/macos/**"], createdAt: T - 3 * HOUR, updatedAt: T - 14 * MIN, links: [{kind: "document", label: "Audit", target: "build/macos/combined-qd9/final-audit.json"}]},
      {id: "s13", userTurns: 3, title: "Dispatcher reference rewrite", role: "implementer", client: "kimi", phase: "implementation", status: "failed", domain: "docs", target: "docs/dispatch.md",
        summary: "Failed: the provider returned rate-limit errors for six minutes while rewriting the controls section. Two of four sections are drafted.", failure: "Provider rate limit",
        ask: {kind: "failure", question: "The provider rate-limited this agent for six minutes. Retry, or hand the rewrite off?", options: [{id: "retry", label: "Retry"}, {id: "handoff", label: "Hand off to Claude"}, {id: "close", label: "Close"}], blocking: false, askedAt: T - 9 * MIN},
        scope: ["docs/dispatch.md"], createdAt: T - 50 * MIN, updatedAt: T - 9 * MIN, plan: [{label: "Outline the sections", state: "done"}, {label: "Draft quick start", state: "done"}, {label: "Draft controls", state: "blocked"}, {label: "Draft routing", state: "todo"}]},
      {id: "s14", title: "CI watch for mobile build", role: "delegate", kind: "delegate", client: "claude", phase: "validation", status: "blocked", domain: "infra", target: "GitHub Actions", parentId: "s09",
        summary: "Watching the mobile build run; 2 of 3 jobs are green.", next: {actor: "external", action: "Waiting for GitHub Actions run 4411 to finish."}, waitingOn: ["external:GitHub Actions run 4411"],
        createdAt: T - 20 * MIN, updatedAt: T - 4 * MIN, plan: [{label: "Start the watch", state: "done"}, {label: "Poll until the run completes", state: "active"}, {label: "Report the result", state: "todo"}]},
      {id: "s15", userTurns: 2, title: "Thread hover preview latency", role: "implementer", client: "codex", phase: "planning", status: "working", domain: "web", target: "thread-hover.js",
        summary: "Measured a 300 ms open delay plus a preview fetch on every hover. Planning a cache warm-up on card render.", activity: "Reading thread-hover.js and the preview endpoint",
        scope: ["plugins/agent-coord/scripts/agent_coord/web/thread-hover.js", "plugins/agent-coord/scripts/agent_coord/thread_preview.py"], createdAt: T - 25 * MIN, updatedAt: T - 6 * MIN, stale: true,
        plan: [{label: "Measure the current delay", state: "done"}, {label: "Plan cache warm-up", state: "active"}, {label: "Implement", state: "todo"}, {label: "Verify on a slow profile", state: "todo"}]},
    ];
    return specs.map(spec => makeAgent(spec, rand));
  }

  function generated(rand, count, startIndex) {
    const agents = []; let n = startIndex;
    // Identity names are unique across the field; hand-written agents reserve theirs.
    const usedNames = new Set(["Gravity", "Harbor", "Rook", "Pager", "Release", "Dispatcher"]);
    const uniqueName = () => { const free = NAMES.filter(x => !usedNames.has(x)); if (!free.length) return null; const x = pick(rand, free); usedNames.add(x); return x; };
    const nextId = () => "g" + String(n++).padStart(3, "0");
    const used = new Set();
    const title = domain => { for (let i = 0; i < 20; i++) { const t = pick(rand, VERBS[domain]) + " " + pick(rand, OBJECTS[domain]); if (!used.has(t)) { used.add(t); return t; } } return pick(rand, VERBS[domain]) + " " + pick(rand, OBJECTS[domain]) + " " + n; };
    const ask = (kind, agent) => { const t = pick(rand, ASKS[kind]); return {kind, question: fill(t.question, agent), options: t.options.map(([id, label]) => ({id, label})), freeText: !!t.freeText, blocking: kind !== "review" && kind !== "failure" ? true : rand() < 0.4, askedAt: BASE_NOW - Math.floor(rand() * 40) * MIN}; };
    const statusFor = (phase) => weighted(rand, [["working", 45], ["idle", 18], ["waiting", 12], ["blocked", 8], ["done", 14], ["failed", 3]]);
    while (agents.length < count) {
      const domain = pick(rand, DOMAINS);
      const groupSize = rand() < 0.25 ? 0 : 2 + Math.floor(rand() * 4);
      const createdAt = BASE_NOW - (30 + Math.floor(rand() * 600)) * MIN;
      if (!groupSize) {
        const status = weighted(rand, [["working", 40], ["idle", 35], ["waiting", 10], ["done", 15]]);
        const role = domain === "research" ? "researcher" : "implementer";
        const phase = status === "done" ? "finished" : role === "researcher" ? "investigation" : pick(rand, ["planning", "implementation", "implementation", "validation"]);
        const a = {id: nextId(), title: title(domain), name: rand() < 0.35 ? uniqueName() : null, role, client: pick(rand, CLIENTS), phase, status, domain, scope: [pick(rand, SCOPES[domain])],
          createdAt, updatedAt: BASE_NOW - Math.floor(rand() * 180) * MIN, unread: status === "done" && rand() < 0.6, next: status === "idle" ? {actor: "nobody", action: ""} : undefined};
        if (status === "waiting") a.ask = ask(pick(rand, ["decision", "information", "approval"]), a);
        agents.push(makeAgent(a, rand)); continue;
      }
      const orchestrator = {id: nextId(), title: title(domain), name: rand() < 0.5 ? uniqueName() : null, role: "orchestrator", client: pick(rand, ["codex", "claude"]), phase: "orchestrating",
        status: "working", domain, createdAt, updatedAt: BASE_NOW - Math.floor(rand() * 30) * MIN, scope: [pick(rand, SCOPES[domain])], waitingOn: [], pinned: rand() < 0.15};
      const children = [];
      for (let i = 0; i < groupSize; i++) {
        const role = weighted(rand, [["implementer", 60], ["reviewer", 20], ["researcher", 15], ["releaser", 5]]);
        const cDomain = rand() < 0.75 ? domain : pick(rand, DOMAINS);
        const status = statusFor();
        const phase = status === "done" ? "finished" : role === "researcher" ? "investigation" : role === "reviewer" ? "validation" : role === "releaser" ? "deployment" : pick(rand, ["planning", "implementation", "implementation", "implementation", "validation"]);
        const child = {id: nextId(), title: title(cDomain), name: rand() < 0.3 ? uniqueName() : null, role, client: pick(rand, CLIENTS), phase, status, domain: cDomain, parentId: orchestrator.id,
          createdAt: createdAt + (5 + Math.floor(rand() * 40)) * MIN, updatedAt: BASE_NOW - Math.floor(rand() * 90) * MIN, scope: [SCOPES[cDomain][(i + n) % SCOPES[cDomain].length]], unread: status === "done" && rand() < 0.5,
          next: status === "idle" ? {actor: "nobody", action: ""} : undefined};
        if (status === "waiting") child.ask = ask(pick(rand, ["decision", "approval", "information", "access", "review"]), child);
        if (status === "failed") { child.ask = ask("failure", child); child.failure = "Provider error"; }
        if (status === "blocked") { const ext = pick(rand, EXTERNALS); child.waitingOn = ["external:" + ext]; child.next = {actor: "external", action: "Waiting for " + ext + "."}; }
        if (status !== "done") orchestrator.waitingOn.push(child.id);
        children.push(child);
        const subs = status === "working" && rand() < 0.5 ? 1 + Math.floor(rand() * 3) : 0;
        for (let s = 0; s < subs; s++) {
          const subStatus = rand() < 0.5 ? "working" : "done";
          children.push({id: nextId(), title: pick(rand, ["Explore", "Test", "Search", "Summarize", "Verify"]) + " " + pick(rand, OBJECTS[cDomain]), role: "subagent", kind: "subagent", client: child.client,
            phase: subStatus === "done" ? "finished" : "implementation", status: subStatus, domain: cDomain, parentId: child.id, createdAt: child.createdAt + (10 + Math.floor(rand() * 30)) * MIN,
            updatedAt: BASE_NOW - Math.floor(rand() * 25) * MIN});
        }
      }
      if (!orchestrator.waitingOn.length) { orchestrator.status = "idle"; orchestrator.phase = "validation"; orchestrator.next = {actor: "nobody", action: ""}; }
      else orchestrator.status = rand() < 0.7 ? "blocked" : "working";
      if (orchestrator.status === "blocked") orchestrator.next = {actor: "agent", action: "Waiting for " + orchestrator.waitingOn.length + " specialists to finish, then integrating."};
      agents.push(makeAgent(orchestrator, rand));
      for (const child of children) agents.push(makeAgent(child, rand));
    }
    return agents;
  }

  function create({scale = "small", seed = 7, domains = "clean"} = {}) {
    const rand = mulberry32(seed);
    const agents = smallSet(rand);
    const extra = scale === "large" ? 140 : scale === "medium" ? 26 : 0;
    if (extra) agents.push(...generated(rand, extra, 0));
    const scenario = Object.hasOwn(DOMAIN_SCENARIOS, domains) ? domains : "clean";
    applyDomainScenario(agents, scenario, new Map(agents.map(a => [a.id, a])));
    const listeners = new Set();
    const field = {agents, now: BASE_NOW, scale, seed, scenario, events: [], rand, tickCount: 0};
    const index = () => { field._byId = new Map(agents.map(a => [a.id, a])); field._kids = new Map(); for (const a of agents) { if (!a.parentId) continue; if (!field._kids.has(a.parentId)) field._kids.set(a.parentId, []); field._kids.get(a.parentId).push(a); } for (const list of field._kids.values()) list.sort((x, y) => x.createdAt - y.createdAt); };
    index();
    const emit = () => { index(); for (const fn of listeners) fn(field); };
    Object.assign(field, {
      byId: id => field._byId.get(id) || null,
      children: id => field._kids.get(id) || [],
      parent: id => { const a = field.byId(id); return a?.parentId ? field.byId(a.parentId) : null; },
      roots: () => agents.filter(a => !a.parentId || !field.byId(a.parentId)),
      depth: id => { let d = 0, a = field.byId(id); while (a?.parentId && field.byId(a.parentId)) { d++; a = field.byId(a.parentId); } return d; },
      ancestors: id => { const out = []; let a = field.parent(id); while (a) { out.push(a); a = field.parent(a.id); } return out; },
      descendants: id => { const out = []; const walk = pid => { for (const c of field.children(pid)) { out.push(c); walk(c.id); } }; walk(id); return out; },
      treeRows: (ids = null) => { const rows = []; const walk = (a, depth) => { rows.push({agent: a, depth}); for (const c of field.children(a.id)) walk(c, depth + 1); }; for (const r of (ids ? ids.map(field.byId).filter(Boolean) : field.roots())) walk(r, 0); return rows; },
      needsUser: a => !!a.ask || (a.next?.actor === "user" && !!a.next.action),
      progress: a => ({done: a.plan.filter(s => s.state === "done" || (s.state === "delegated" && field.byId(s.agentId)?.status === "done")).length, total: a.plan.length}),
      blocks: id => agents.filter(a => a.waitingOn.includes(id)).map(a => a.id),
      impact: id => { const seen = new Set(); const walk = x => { for (const b of field.blocks(x)) if (!seen.has(b)) { seen.add(b); walk(b); } }; walk(id); return [...seen]; },
      externalWaits: a => a.waitingOn.filter(w => w.startsWith("external:")).map(w => w.slice(9)),
      agentWaits: a => a.waitingOn.filter(w => !w.startsWith("external:")).map(field.byId).filter(Boolean),
      asks: () => agents.filter(field.needsUser).sort((x, y) => Number(!!y.ask?.blocking) - Number(!!x.ask?.blocking) || field.impact(y.id).length - field.impact(x.id).length || (x.ask?.askedAt ?? x.updatedAt) - (y.ask?.askedAt ?? y.updatedAt)),
      descendantsNeedingUser: id => field.descendants(id).filter(field.needsUser),
      counts: (list = agents) => { const c = {total: list.length, working: 0, waiting: 0, blocked: 0, idle: 0, failed: 0, done: 0, needsUser: 0, unread: 0}; for (const a of list) { c[a.status]++; if (field.needsUser(a)) c.needsUser++; if (a.unread) c.unread++; } return c; },
      domainOf: a => a.domain, domainLabel: d => DOMAIN_LABELS[d] || d,
      domainsOf: a => a.domains || (a.domain ? [a.domain] : []),
      domainKeys: () => [...new Set(agents.flatMap(a => a.domains || (a.domain ? [a.domain] : [])))].sort(),
      statusLabel: a => a.status === "idle" ? PHASE_LABELS[a.phase] : a.status === "blocked" ? "Waiting on " + (field.externalWaits(a)[0] || (field.agentWaits(a).length === 1 ? (field.agentWaits(a)[0].name || field.agentWaits(a)[0].title) : field.agentWaits(a).length + " agents")) : STATUS_LABELS[a.status],
      relativeTime: ms => { const d = Math.max(0, field.now - ms); if (d < MIN) return "just now"; if (d < HOUR) return Math.round(d / MIN) + "m ago"; if (d < 24 * HOUR) return Math.round(d / HOUR) + "h ago"; return Math.round(d / (24 * HOUR)) + "d ago"; },
      duration: ms => { const d = Math.max(0, ms); if (d < HOUR) return Math.round(d / MIN) + "m"; if (d < 24 * HOUR) return (d / HOUR).toFixed(d < 10 * HOUR ? 1 : 0) + "h"; return Math.round(d / (24 * HOUR)) + "d"; },
      subscribe: fn => { listeners.add(fn); return () => listeners.delete(fn); },
      emit,
      log: (agent, type, text) => field.events.push({at: field.now, agentId: agent.id, type, text}),
      // Recency from the runtime, not from the checkpoint: the last tool call or the last update.
      lastActivity: a => Math.max(a.updatedAt, a.pulses?.length ? a.pulses[a.pulses.length - 1] : 0),
      age: a => Math.max(0, field.now - field.lastActivity(a)),
      toolLabel: a => a.lastTool ? ({read: "Reading ", edit: "Editing ", run: "Running ", search: "Searching "}[a.lastTool.kind] + a.lastTool.label) : null,
      pulsesSince: (a, ms) => (a.pulses || []).filter(t => t >= field.now - ms),
      // What a lens shows without the thread: the last finished step, the current one, and the next.
      recentSteps: a => {
        if (a.plan.length) {
          const i = a.plan.findIndex(s => ["active", "blocked"].includes(s.state));
          const done = a.plan.filter(s => s.state === "done");
          const out = [];
          if (done.length) out.push({state: "done", label: done[done.length - 1].label});
          if (i >= 0) out.push({state: a.plan[i].state, label: a.plan[i].label});
          const next = a.plan.find((s, j) => j > i && s.state === "todo");
          if (next) out.push({state: "todo", label: next.label});
          if (!out.length && a.status === "done") out.push({state: "done", label: done[done.length - 1]?.label || "Finished"});
          return out;
        }
        return a.history.slice(-2).map(h => ({state: "done", label: h.summary}));
      },
    });
    function finish(a) {
      a.phase = "finished"; a.status = "done"; a.activity = null; a.next = {actor: "nobody", action: ""}; a.unread = true;
      a.summary = "Completed " + lower(a.title) + ". " + (a.role === "reviewer" ? "No blocking findings remain." : a.role === "researcher" ? "Findings are written up with evidence." : "Focused tests pass.");
      for (const s of a.plan) if (s.state !== "delegated") s.state = "done";
      a.updatedAt = a.checkpointAt = field.now; a.history.push({at: field.now, phase: "finished", summary: a.summary, actor: "agent"}); field.log(a, "finished", a.title + " finished");
      for (const w of agents) { const i = w.waitingOn.indexOf(a.id); if (i >= 0) { w.waitingOn.splice(i, 1); if (!w.waitingOn.length && w.status === "blocked") { w.status = "working"; w.activity = "Integrating contributions"; w.next = {actor: "agent", action: "Integrate and validate the contributions."}; w.updatedAt = field.now; field.log(w, "resumed", w.title + " resumed"); } } }
    }
    function phaseFor(a) {
      if (a.role === "orchestrator") return a.waitingOn.length ? "orchestrating" : "validation";
      if (a.role === "researcher" || a.role === "subagent") return a.phase === "finished" ? "finished" : a.role === "researcher" ? "investigation" : "implementation";
      if (a.role === "reviewer") return "validation";
      if (a.role === "releaser") return "deployment";
      const {done, total} = field.progress(a); if (!total) return a.phase;
      return done === 0 ? (a.phase === "planning" || a.phase === "investigation" ? a.phase : "planning") : done >= total - 1 ? "validation" : "implementation";
    }
    field.tick = () => {
      field.now += MIN; field.tickCount++;
      const working = Math.max(1, agents.filter(x => x.status === "working").length);
      // Field-wide budgets per simulated minute: about one ask per four minutes, one spawn per five, one external wait per eight.
      const askP = Math.min(0.015, 0.25 / working), spawnP = Math.min(0.01, 0.2 / working), extP = Math.min(0.015, 0.12 / working);
      for (const a of [...agents]) {
        const r = rand();
        if (a.status === "working") {
          if (r < 0.12) {
            const active = a.plan.findIndex(s => s.state === "active");
            if (active >= 0) a.plan[active].state = "done";
            const next = a.plan.findIndex(s => s.state === "todo");
            if (next >= 0) { a.plan[next].state = "active"; a.activity = a.plan[next].label; a.summary = fill(PHASE_SUMMARY[phaseFor(a)], a) + " Step " + (next + 1) + " of " + a.plan.length + ": " + lower(a.plan[next].label) + "."; const p = phaseFor(a); if (p !== a.phase) { a.phase = p; a.history.push({at: field.now, phase: p, summary: a.summary, actor: "agent"}); field.log(a, "phase", a.title + " → " + PHASE_LABELS[p]); } a.updatedAt = a.checkpointAt = field.now; a.stale = false; }
            else if (a.role === "dispatcher") { a.updatedAt = field.now; }
            else finish(a);
          } else if (r < 0.12 + askP && !a.ask && a.role !== "dispatcher") {
            const kind = a.role === "releaser" ? "approval" : a.role === "reviewer" ? "review" : pick(rand, ["decision", "information", "approval", "access"]);
            const t = pick(rand, ASKS[kind]);
            a.ask = {kind, question: fill(t.question, a), options: t.options.map(([id, label]) => ({id, label})), freeText: !!t.freeText, blocking: kind !== "review", askedAt: field.now};
            a.status = "waiting"; a.next = {actor: "user", action: a.ask.question}; a.activity = null; const act = a.plan.find(s => s.state === "active"); if (act) act.state = "blocked";
            a.updatedAt = a.checkpointAt = field.now; field.log(a, "ask", a.title + " asks: " + a.ask.question);
          } else if (r < 0.12 + askP + spawnP && a.kind === "app" && field.depth(a.id) < 2 && field.children(a.id).length < 4 && a.role !== "dispatcher") {
            field.spawn(a.id, {title: pick(rand, ["Explore", "Test", "Search", "Summarize", "Verify"]) + " " + pick(rand, OBJECTS[a.domain])}, false);
          } else if (r < 0.12 + askP + spawnP + extP && a.kind === "app" && !a.waitingOn.length) {
            const ext = pick(rand, EXTERNALS); a.waitingOn = ["external:" + ext]; a.status = "blocked"; a.activity = null; a.next = {actor: "external", action: "Waiting for " + ext + "."}; a.updatedAt = field.now; field.log(a, "blocked", a.title + " waits for " + ext);
          } else if (r < 0.35) { a.activity = pick(rand, ACTIVITIES); a.updatedAt = field.now; }
          if (a._rand2 && a._rand2() < 0.65) { a.pulses.push(field.now); if (a._rand2() < 0.5) a.lastTool = {...toolFor(a, a._rand2), at: field.now}; else a.lastTool.at = field.now; if (a._rand2() < 0.18) a.turns++; }
        } else if (a.status === "blocked") {
          if (field.externalWaits(a).length && r < 0.18) { a.waitingOn = a.waitingOn.filter(w => !w.startsWith("external:")); if (!a.waitingOn.length) { a.status = "working"; a.activity = pick(rand, ACTIVITIES); a.next = {actor: "agent", action: "Continue with the next step."}; a.updatedAt = field.now; field.log(a, "resumed", a.title + " resumed"); } }
        } else if (a.status === "idle" && r < 0.02 && a.role === "dispatcher") { a.updatedAt = field.now; }
      }
      emit();
    };
    field.answer = (id, choice) => {
      const a = field.byId(id); if (!a) return;
      const label = typeof choice === "string" ? (a.ask?.options.find(o => o.id === choice)?.label || choice) : String(choice);
      const was = a.ask; a.ask = null; a.unread = false; a.userTurns = (a.userTurns || 0) + 1; a.turns = (a.turns || 0) + 1;
      a.history.push({at: field.now, phase: a.phase, summary: "You answered: " + label + ".", actor: "user"}); field.log(a, "answered", a.title + ": " + label);
      if (was?.kind === "failure" && choice === "close") { a.status = "done"; a.phase = "finished"; a.next = {actor: "nobody", action: ""}; a.summary = "Closed after a provider failure. " + a.summary; }
      else if (was?.kind === "approval" && (choice === "cancel" || choice === "wait" || choice === "local")) { a.status = "idle"; a.next = {actor: "nobody", action: ""}; a.summary = "Holding after your answer (" + label + "). " + a.summary; }
      else { if (was?.kind === "failure" && choice === "handoff") a.client = "claude"; a.status = "working"; a.failure = null; a.activity = "Continuing after your answer"; a.next = {actor: "agent", action: "Continue with the chosen option: " + label + "."}; const blocked = a.plan.find(s => s.state === "blocked"); if (blocked) blocked.state = "active"; }
      a.updatedAt = a.checkpointAt = field.now; emit();
    };
    field.spawn = (parentId, {title, role = "subagent", kind = "subagent", client} = {}, announce = true) => {
      const parent = field.byId(parentId); if (!parent) return null;
      const id = "n" + String(agents.length).padStart(3, "0");
      const a = makeAgent({id, title: title || "Explore " + pick(rand, OBJECTS[parent.domainReported || parent.domain] || OBJECTS.web), role, kind, client: client || parent.client, phase: "implementation", status: "working", domain: parent.domain, target: parent.target, parentId, createdAt: field.now, updatedAt: field.now}, rand, field.now);
      a.domains = [...(parent.domains || [parent.domain])]; a.domainReported = parent.domainReported || parent.domain; a.domainSource = parent.domainSource || "reported";
      agents.push(a); parent.plan.push({label: a.title, state: "delegated", agentId: id}); if (kind === "app" && !parent.waitingOn.includes(id)) parent.waitingOn.push(id);
      field.log(parent, "spawned", parent.title + " started " + a.title);
      if (announce) emit(); else index();
      return a;
    };
    field.dismiss = id => { const a = field.byId(id); if (a) { a.unread = false; emit(); } };
    return field;
  }

  const el = (tag, className, text) => { const n = document.createElement(tag); if (className) n.className = className; if (text != null) n.textContent = text; return n; };
  function phaseSteps(agent) {
    const wrap = el("span", "phase-steps" + (agent.phase === "finished" ? " done" : ""));
    wrap.title = PHASE_LABELS[agent.phase]; wrap.setAttribute("aria-label", "Phase: " + PHASE_LABELS[agent.phase]);
    const idx = PHASES.indexOf(agent.phase);
    PHASES.forEach((p, i) => { const i2 = el("i"); if (i < idx) i2.className = "past"; else if (i === idx) i2.className = "current"; wrap.append(i2); });
    return wrap;
  }
  function statusDot(agent) { const d = el("span", "dot " + agent.status); d.title = STATUS_LABELS[agent.status]; return d; }

  return {create, PHASES, PHASE_LABELS, DOMAINS, DOMAIN_LABELS, DOMAIN_SCENARIOS, ROLES, ROLE_LABELS, CLIENTS, PROVIDER_NAMES, STATUS_LABELS, providerLogo, el, phaseSteps, statusDot, BASE_NOW};
})();
if (typeof module !== "undefined" && module.exports) module.exports = AgentField;
