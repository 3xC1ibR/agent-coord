/* Orchestration layer for the round-three mockups (19–28).
 *
 * Loaded after fixture.js and before mockup.js. It wraps AgentField.create so every
 * field also carries the agent-to-agent message log and the orchestration signals
 * the round-three views need:
 *
 *   Today (agent-coord already records these):
 *     messages      sender, recipient, body, classification (creation, action_required,
 *                   info, reply, closure, failure), reply_required, in_reply_to, exchange
 *                   thread, delivered/acknowledged times, and whether it woke a turn.
 *     created_by    who created whom, creation mode (orchestration or dispatch), and the
 *                   reply contract of the initial request.
 *     conflicts     overlapping begin-work write scopes between active agents.
 *
 *   Proposed (one or two more fields on the same checkpoint or send call):
 *     intent        why a message was sent: assign, question, answer, handoff, finding,
 *                   review, blocker, coordinate, status.
 *     surface       what the agent works on, as a product noun: page, service, module,
 *                   doc, pipeline, build, question.
 *     contract      what the creator expects back: deliverable, acceptance checks, due.
 *     deliverable   what the agent will hand over and how ready it is.
 *     escalation    a child's ask relayed up the chain (ask.via lists the agents it passed).
 *     health        on-track, slipping, stuck (self-reported or derived from age and waits).
 *
 * Nothing here talks to the real coordination database. */
"use strict";
const Orchestration = (() => {
  const MIN = 60_000, HOUR = 3_600_000;
  const KINDS = ["creation", "action_required", "info", "reply", "closure", "failure"];
  const KIND_LABELS = {creation: "Created", action_required: "Request", info: "FYI", reply: "Reply", closure: "Closed", failure: "Failure report"};
  const INTENTS = ["assign", "question", "answer", "handoff", "finding", "review", "blocker", "coordinate", "status"];
  const INTENT_LABELS = {assign: "Assigns", question: "Asks", answer: "Answers", handoff: "Hands off", finding: "Reports a finding", review: "Requests review",
    blocker: "Escalates a blocker", coordinate: "Coordinates scope", status: "Reports status"};
  const SURFACE_KINDS = {web: "page", backend: "service", mobile: "screen", docs: "doc", infra: "pipeline", release: "build", research: "question"};
  const DELIVERABLES = {implementer: "pull request", reviewer: "review verdict", researcher: "findings", releaser: "verified build", orchestrator: "integrated outcome",
    subagent: "summary to parent", delegate: "watch report", dispatcher: "routing"};
  const ACCEPTANCE = {implementer: ["Focused tests pass", "Scope stays inside the declaration", "Checkpoint links the PR"],
    reviewer: ["Every finding reproduced", "No blocking defects", "Verdict sent to the creator"],
    researcher: ["Sources cited", "Answer in two sentences", "Open questions listed"],
    releaser: ["Install smoke test passes", "Conversations preserved", "Rollback ready"],
    subagent: ["Summary returned to the parent"], delegate: ["Result reported when the run finishes"], orchestrator: ["Every contribution integrated", "End-to-end validation passes"]};

  const ANSWERS = ["Yes; keep the current interface.", "Use the new field; the old one is going away.", "Confirmed; proceed with the plan you described.",
    "Narrowed my scope to the tests directory; you're clear.", "Not yet; I'll send it after the focused tests pass.", "The fixture is in tests/fixtures; reuse it.",
    "Good catch; I'll fold that into my next step.", "That path is owned by another team; ask their lead first."];
  const pick = (rand, list) => list[Math.floor(rand() * list.length)];
  const nameOf = a => a ? (a.name || a.title) : "You";
  const lower = t => t ? t.charAt(0).toLowerCase() + t.slice(1) : t;

  /* Bodies are short, like the real ones agents send each other. */
  const BODY = {
    assign: a => `Please take ${lower(a.title)}. Work only in ${a.scope[0] || "your declared scope"}; reply with the ${DELIVERABLES[a.role] || "result"} when done.`,
    question: (a, b) => `Quick check before I continue on ${lower(a.title)}: is ${b?.target || "the current contract"} still the interface you expect?`,
    answer: (a, b, rand) => pick(rand, ANSWERS),
    handoff: a => `${a.title} is ready for you: ${(a.links[0]?.label) || "see my checkpoint"}. Tests pass on my side.`,
    finding: a => `Finding from ${lower(a.title)}: the slow path is in ${a.target}; details in my checkpoint.`,
    review: a => `Please review ${lower(a.title)} before I hand it up.`,
    blocker: a => `Blocked on ${lower(a.title)}: ${a.ask?.question || "I need a decision before continuing."}`,
    coordinate: (a, b) => `We both touch ${sharedPrefix(a, b) || "the same files"}. I'll keep to ${a.scope[0] || "my half"}; can you narrow yours?`,
    status: a => `${a.title}: ${a.plan.filter(s => s.state === "done").length} of ${a.plan.length} steps done.`,
  };

  function sharedPrefix(a, b) {
    for (const x of a.scope || []) for (const y of b.scope || []) { const p = overlap(x, y); if (p) return p; }
    return null;
  }
  function overlap(x, y) {
    const sx = x.replace(/\*\*.*$/, ""), sy = y.replace(/\*\*.*$/, "");
    if (sx.startsWith(sy)) return sy || y; if (sy.startsWith(sx)) return sx || x; return null;
  }

  function attach(field) {
    if (field.messages) return field;
    const agents = field.agents, rand = field.rand;
    let nextId = 1;
    field.messages = [];

    /* Arbitrary depth: in the generated fields, promote a few working specialists to
     * orchestrate their own app agents so trees go three or four levels deep. */
    if (field.scale !== "small" && field.scenario !== "real") {
      const candidates = agents.filter(a => a.kind === "app" && a.parentId && a.status === "working" && a.role === "implementer");
      const count = Math.round(candidates.length * 0.22);
      for (const a of candidates.slice(0, count)) {
        a.role = "orchestrator"; a.phase = "orchestrating";
        for (let i = 0; i < 2; i++) {
          const k = field.spawn(a.id, {role: pick(rand, ["implementer", "implementer", "reviewer", "researcher"]), kind: "app"}, false);
          if (k && rand() < 0.35) field.spawn(k.id, {}, false);
        }
      }
    } else if (field.scale === "small") {
      const hover = field.byId("s15"); if (hover && !hover.scope.includes("plugins/agent-coord/scripts/agent_coord/web/**")) hover.scope.push("plugins/agent-coord/scripts/agent_coord/web/**");
      const g = field.byId("s03");
      if (g && !field.children("s03").length) { const k = field.spawn("s03", {title: "Run upload integration tests"}, false); if (k) { k.createdAt = field.now - 9 * MIN; k.activity = "Running tests/uploads/test_resume.py"; } }
    }

    /* Proposed signals, derived deterministically so every view shows the same values. */
    for (const a of agents) decorate(a);
    function decorate(a) {
      a.surface ??= {kind: SURFACE_KINDS[a.domain] || "module", label: a.target || a.title};
      a.deliverable ??= {kind: DELIVERABLES[a.role] || "result", label: a.links[0]?.label || null,
        state: a.status === "done" ? "delivered" : field.progress(a).total && field.progress(a).done >= field.progress(a).total - 1 ? "ready" : field.progress(a).done ? "draft" : "none"};
      const parent = a.parentId && field.byId(a.parentId);
      if (parent && !a.contract) a.contract = {from: parent.id, deliverable: a.deliverable.kind, acceptance: (ACCEPTANCE[a.role] || ACCEPTANCE.implementer).map(label => ({label, met: a.status === "done"})),
        replyRequired: a.kind === "app", due: a.createdAt + (60 + Math.floor(rand() * 120)) * MIN};
      a.health ??= healthOf(a);
    }
    function healthOf(a) {
      if (a.status === "failed") return "stuck";
      if (a.status === "done" || a.status === "idle") return "on-track";
      const age = field.now - Math.max(a.updatedAt, a.pulses?.at(-1) || 0);
      if (a.status === "blocked" && field.now - a.updatedAt > 40 * MIN) return "stuck";
      if ((a.status === "working" && age > 25 * MIN) || (a.contract && field.now > a.contract.due + 90 * MIN && field.progress(a).done < field.progress(a).total / 2)) return "slipping";
      return "on-track";
    }

    function send(from, to, {kind = "action_required", intent = "status", body, at = field.now, replyRequired, inReplyTo = null, exchange = null, about = null, silent = false} = {}) {
      const sender = from === "user" ? null : field.byId(from), recipient = to === "user" ? null : field.byId(to);
      if ((from !== "user" && !sender) || (to !== "user" && !recipient)) return null;
      const m = {id: nextId++, from, to, at, kind, intent, body: body || (BODY[intent] ? BODY[intent](sender || recipient, recipient, rand) : ""),
        replyRequired: replyRequired ?? (kind === "action_required" || kind === "creation"), inReplyTo, exchange: exchange || "x" + (inReplyTo ? field.messages.find(x => x.id === inReplyTo)?.exchange?.slice(1) : nextId - 1),
        about, deliveredAt: at + 2000, ackedAt: null, wokeTurn: to !== "user" && recipient?.status !== "working"};
      if (inReplyTo) { const orig = field.messages.find(x => x.id === inReplyTo); if (orig) { orig.ackedAt = at; orig.repliedBy = m.id; m.exchange = orig.exchange; } }
      field.messages.push(m);
      if (!silent) field.log(sender || recipient, "message", nameOf(sender) + " → " + nameOf(recipient) + ": " + (INTENT_LABELS[intent] || KIND_LABELS[kind]));
      return m;
    }
    field.send = send;

    /* Seed history: the small field gets hand-written exchanges based on real threads. */
    const T = field.now;
    const S = (from, to, minsAgo, opts) => send(from, to, {...opts, at: T - minsAgo * MIN, silent: true});
    if (field.scale === "small" && field.byId("s01")) {
      const c1 = S("s01", "s02", 300, {kind: "creation", intent: "assign", body: "Own the Rig workspace upload release end to end: backend and UI halves, then deploy and send me the deployment, health, and API handoff. Reply when deployed."});
      const c2 = S("s02", "s03", 240, {kind: "creation", intent: "assign", body: "Implement resumable chunked uploads in rig/api/uploads/**. Keep out of rig/web/upload/**. Reply with the PR and test counts."});
      const c3 = S("s02", "s04", 238, {kind: "creation", intent: "assign", body: "Build the upload page: drag-and-drop, progress, failure recovery. Only rig/web/upload/**. Reply with the PR."});
      const neg = S("s03", "s02", 150, {kind: "action_required", intent: "coordinate", body: "Program task-request work also declared rig/api/**. My begin-work conflicts. Can you get them to narrow it?", about: "rig/api/**"});
      const q = S("s02", "s03", 120, {kind: "action_required", intent: "question", body: "Is the resumable session table keyed by workspace or by upload? The UI needs to know for retry."});
      S("s03", "s02", 117, {kind: "reply", intent: "answer", inReplyTo: q.id, body: "By upload id, with the workspace as a foreign key. Retry can reuse the upload id."});
      S("s02", "s03", 95, {kind: "reply", intent: "coordinate", inReplyTo: neg.id, replyRequired: false, body: "Repaired your narrowed declaration: rig/api/uploads/** and rig/tests/uploads/** only. You are clear to continue."});
      S("s04", "s02", 48, {kind: "reply", intent: "handoff", inReplyTo: c3.id, body: "Upload page is done: #409, 14 tests pass. Failure recovery uses the upload id from Gravity's table."});
      S("s03", "s02", 20, {kind: "info", intent: "status", replyRequired: false, body: "Three of five integration tests pass. Resume-after-disconnect is next."});
      S("s01", "s05", 180, {kind: "creation", intent: "assign", body: "Review rig-xef1.2 for correctness: reproduce every defect before reporting. Reply with a verdict."});
      S("s05", "s01", 22, {kind: "reply", intent: "review", inReplyTo: field.messages.find(m => m.to === "s05")?.id, body: "Verdict: no blocking findings. Three reproduced defects were fixed in place; 123 focused tests and 8 reproductions pass. Needs the owner's approval to close."});
      S("s01", "s02", 62, {kind: "action_required", intent: "question", body: "Run-002 is held for your deployment. What is your ETA for the API handoff?"});
      S("s00", "s06", 125, {kind: "creation", intent: "assign", replyRequired: false, body: "Find the automation evaluation program the user ran in September."});
      S("s00", "s07", 124, {kind: "creation", intent: "assign", replyRequired: false, body: "Explain why the owner's experiment preference was missing from the October 12 request."});
      S("s00", "s08", 123, {kind: "creation", intent: "assign", replyRequired: false, body: "Explain why the orchestrator stayed idle on October 12."});
      S("s07", "s08", 80, {kind: "info", intent: "finding", replyRequired: false, body: "Related: the context builder drops informal owner decisions. That may explain your idle hold too."});
      S("s09", "s10", 80, {kind: "creation", intent: "assign", body: "Map the push subscription lifecycle across push-worker.js and web_push.py."});
      S("s10", "s09", 60, {kind: "reply", intent: "finding", inReplyTo: field.messages.at(-1).id, body: "The subscription is re-created only after the first poll. Cold launch drops the destination before that."});
      S("s09", "s11", 12, {kind: "creation", intent: "assign", body: "Write two regression tests for destination recovery in tests/test_web_push.py."});
      S("s09", "s14", 20, {kind: "creation", intent: "assign", body: "Watch GitHub Actions run 4411 and report when the mobile build finishes."});
      S("s14", "s09", 4, {kind: "info", intent: "status", replyRequired: false, body: "2 of 3 jobs green; the iOS job is still running."});
      S("s15", "s09", 7, {kind: "action_required", intent: "coordinate", body: "I may touch thread_preview.py while you're in web_push.py; no overlap expected, just checking you're not editing the preview cache.", about: "plugins/agent-coord/scripts/agent_coord/"});
      S("s13", "user", 9, {kind: "failure", intent: "blocker", replyRequired: false, body: "Provider rate limit for six minutes while drafting controls. Two of four sections drafted."});
      void c1; void c2;
      const g = field.byId("s03"), sub = field.children("s03")[0];
      if (g && sub) S("s03", sub.id, 9, {kind: "creation", intent: "assign", body: "Run tests/uploads/test_resume.py and summarize failures."});
      // Harbor asks the user directly; Rook's review ask was relayed through Procurement.
      const harbor = field.byId("s02"); if (harbor?.ask) harbor.ask.via = [];
      const rook = field.byId("s05"); if (rook?.ask) rook.ask.via = ["s01"];
    } else {
      for (const a of [...agents].sort((x, y) => x.createdAt - y.createdAt)) {
        const p = a.parentId && field.byId(a.parentId); if (!p) continue;
        const created = S(p.id, a.id, (T - a.createdAt) / MIN, {kind: "creation", intent: "assign", replyRequired: a.kind === "app"});
        const span = Math.max(1, (Math.min(a.updatedAt, T) - a.createdAt) / MIN);
        const chatter = Math.floor(rand() * (a.kind === "app" ? 4 : 2));
        for (let i = 0; i < chatter; i++) {
          const at = (T - a.createdAt) / MIN - span * (i + 1) / (chatter + 1);
          const intent = pick(rand, ["status", "status", "question", "finding", "coordinate"]);
          if (intent === "question") { const q = S(p.id, a.id, at, {intent: "question"}); if (rand() < 0.75) S(a.id, p.id, at - 3, {kind: "reply", intent: "answer", inReplyTo: q.id}); }
          else S(a.id, p.id, at, {kind: "info", intent, replyRequired: false});
        }
        if (a.status === "done") S(a.id, p.id, (T - a.updatedAt) / MIN, {kind: "reply", intent: "handoff", inReplyTo: created?.id});
        if (a.ask && rand() < 0.6) { S(a.id, p.id, (T - (a.ask.askedAt || a.updatedAt)) / MIN, {kind: "action_required", intent: "blocker"}); a.ask.via = [p.id]; }
      }
      // Peers in the same team coordinate when their scopes overlap or share a domain.
      for (const p of agents) {
        const kids = field.children(p.id).filter(k => k.kind === "app");
        for (let i = 0; i < kids.length; i++) for (let j = i + 1; j < kids.length; j++) {
          if (rand() < (sharedPrefix(kids[i], kids[j]) ? 0.8 : 0.18)) S(kids[i].id, kids[j].id, Math.max(1, (T - Math.max(kids[i].createdAt, kids[j].createdAt)) / MIN * rand()), {intent: sharedPrefix(kids[i], kids[j]) ? "coordinate" : "question", kind: "action_required"});
        }
      }
      // Cross-team: a few agents in the same domain under different roots compare notes.
      const active = agents.filter(a => a.kind === "app" && !["done"].includes(a.status));
      for (let i = 0; i < Math.round(active.length / 9); i++) {
        const a = pick(rand, active), b = pick(rand, active.filter(x => x.domain === a.domain && x.id !== a.id && field.ancestors(x.id).at(-1)?.id !== field.ancestors(a.id).at(-1)?.id));
        if (b) S(a.id, b.id, 5 + rand() * 120, {intent: pick(rand, ["finding", "coordinate", "question"]), kind: "info", replyRequired: false});
      }
    }
    field.messages.sort((x, y) => x.at - y.at || x.id - y.id);

    /* Queries */
    Object.assign(field, {
      KIND_LABELS, INTENT_LABELS,
      nameOf: id => id === "user" ? "You" : nameOf(field.byId(id)),
      messagesOf: (id, {since = 0} = {}) => field.messages.filter(m => (m.from === id || m.to === id) && m.at >= since),
      between: (a, b) => field.messages.filter(m => (m.from === a && m.to === b) || (m.from === b && m.to === a)),
      // Messages that still owe a reply, with who owes it and for how long.
      // Pass {creations: false} to leave out the result an assignment owes back to its creator.
      openRequests: ({creations = true} = {}) => field.messages.filter(m => m.replyRequired && !m.repliedBy && m.kind !== "closure" && (creations || m.kind !== "creation") && !(m.to !== "user" && field.byId(m.to)?.status === "done")),
      owes: id => field.openRequests().filter(m => m.to === id),
      awaiting: id => field.openRequests().filter(m => m.from === id),
      // Aggregated edges for drawing: one per unordered pair, with counts each way.
      edges: ({since = 0, includeUser = true} = {}) => {
        const map = new Map();
        for (const m of field.messages) {
          if (m.at < since || (!includeUser && (m.from === "user" || m.to === "user"))) continue;
          const [a, b] = [m.from, m.to].sort(); const key = a + "|" + b;
          if (!map.has(key)) map.set(key, {a, b, count: 0, ab: 0, ba: 0, last: null, open: 0, intents: {}, kinds: {}});
          const e = map.get(key); e.count++; if (m.from === a) e.ab++; else e.ba++; e.last = m;
          e.intents[m.intent] = (e.intents[m.intent] || 0) + 1; e.kinds[m.kind] = (e.kinds[m.kind] || 0) + 1;
          if (m.replyRequired && !m.repliedBy) e.open++;
        }
        return [...map.values()];
      },
      exchanges: () => {
        const map = new Map();
        for (const m of field.messages) { if (!map.has(m.exchange)) map.set(m.exchange, []); map.get(m.exchange).push(m); }
        return [...map.entries()].map(([id, list]) => ({id, messages: list, first: list[0], last: list.at(-1), parties: [...new Set(list.flatMap(m => [m.from, m.to]))],
          open: list.some(m => m.replyRequired && !m.repliedBy)})).sort((x, y) => y.last.at - x.last.at);
      },
      // Peers: agents this one has talked to recently that are not its parent or children.
      collaborators: (id, since = field.now - 2 * HOUR) => {
        const a = field.byId(id), family = new Set([a?.parentId, ...field.children(id).map(k => k.id)]);
        const out = new Map();
        for (const m of field.messagesOf(id, {since})) { const other = m.from === id ? m.to : m.from; if (other === "user" || family.has(other)) continue; out.set(other, (out.get(other) || 0) + 1); }
        return [...out.entries()].map(([oid, count]) => ({agent: field.byId(oid), count})).filter(x => x.agent);
      },
      // Overlapping write scopes between unfinished agents.
      conflicts: () => {
        // begin-work refuses overlapping declarations, so a live conflict is transient: both agents are mid-turn.
        // At most one conflict per agent keeps the generated fields realistic.
        const live = agents.filter(a => (a.status === "working" || a.status === "blocked") && a.kind === "app" && a.scope?.length), out = [], seen = new Set();
        for (let i = 0; i < live.length; i++) for (let j = i + 1; j < live.length; j++) {
          const p = sharedPrefix(live[i], live[j]); if (!p) continue;
          if (live[i].parentId === live[j].id || live[j].parentId === live[i].id || seen.has(live[i].id) || seen.has(live[j].id)) continue;
          seen.add(live[i].id); seen.add(live[j].id);
          out.push({a: live[i], b: live[j], path: p, negotiating: field.between(live[i].id, live[j].id).some(m => m.intent === "coordinate")});
        }
        return out;
      },
      // A team is a root and everyone below it.
      teams: () => field.roots().map(r => { const members = [r, ...field.descendants(r.id)]; return {root: r, members, counts: field.counts(members), needs: members.filter(field.needsUser), depth: Math.max(...members.map(m => field.depth(m.id)))}; }),
      chain: id => [...field.ancestors(id).reverse(), field.byId(id)].filter(Boolean),
      health: a => (a.health = healthOf(a)),
      // Who an ask is really for and what it holds up.
      askPath: id => { const a = field.byId(id); return a?.ask ? {via: (a.ask.via || []).map(field.byId).filter(Boolean), blocks: field.impact(id).map(field.byId).filter(Boolean)} : null; },
      reply: (messageId, body, from) => { const m = field.messages.find(x => x.id === messageId); if (!m) return null; const r = send(from || m.to, m.from, {kind: "reply", intent: "answer", inReplyTo: m.id, body}); field.emit(); return r; },
      message: (to, body, {intent = "question"} = {}) => { const m = send("user", to, {kind: "action_required", intent, body}); const a = field.byId(to); if (a && a.status === "idle") { a.status = "working"; a.activity = "Reading your message"; a.updatedAt = field.now; } field.emit(); return m; },
    });

    /* Simulation: wrap tick, answer and spawn so live changes produce messages. */
    const baseTick = field.tick, baseAnswer = field.answer, baseSpawn = field.spawn;
    field.spawn = (parentId, opts = {}, announce = true) => {
      const k = baseSpawn(parentId, opts, false); if (!k) return k;
      decorate(k); send(parentId, k.id, {kind: "creation", intent: "assign", replyRequired: k.kind === "app", silent: true});
      if (announce) field.emit(); return k;
    };
    field.tick = () => {
      const before = new Map(agents.map(a => [a.id, {status: a.status, ask: a.ask, phase: a.phase}]));
      baseTick();
      for (const a of agents) {
        const was = before.get(a.id); const p = a.parentId && field.byId(a.parentId);
        if (!was) continue;
        if (p && a.status === "done" && was.status !== "done") { const created = field.messages.find(m => m.from === p.id && m.to === a.id && m.kind === "creation"); send(a.id, p.id, {kind: "reply", intent: "handoff", inReplyTo: created && !created.repliedBy ? created.id : null}); if (a.contract) for (const c of a.contract.acceptance) c.met = true; a.deliverable.state = "delivered"; }
        else if (a.ask && !was.ask) { if (p && rand() < 0.7) { send(a.id, p.id, {kind: "action_required", intent: "blocker"}); a.ask.via = [p.id]; } }
        else if (p && a.status === "working" && rand() < 0.04) send(a.id, p.id, {kind: "info", intent: pick(rand, ["status", "finding"]), replyRequired: false});
        else if (a.status === "working" && rand() < 0.012) { const sib = field.children(a.parentId || "").filter(x => x.id !== a.id && x.status !== "done"); if (sib.length) send(a.id, pick(rand, sib).id, {kind: "action_required", intent: pick(rand, ["coordinate", "question"])}); }
        if (a.phase !== was.phase || a.status !== was.status) { const prog = field.progress(a); if (a.deliverable && a.status !== "done") a.deliverable.state = prog.total && prog.done >= prog.total - 1 ? "ready" : prog.done ? "draft" : "none"; }
        a.health = healthOf(a);
      }
      // Replies arrive for open agent-to-agent requests after a few minutes.
      for (const m of field.openRequests()) {
        if (m.to === "user" || m.from === "user" || m.kind === "creation") continue;
        const r = field.byId(m.to); if (r && r.status === "working" && field.now - m.at > 3 * MIN && rand() < 0.25) send(m.to, m.from, {kind: "reply", intent: "answer", inReplyTo: m.id});
      }
      field.emit();
    };
    field.answer = (id, choice) => {
      const a = field.byId(id); const ask = a?.ask; const label = typeof choice === "string" ? (ask?.options.find(o => o.id === choice)?.label || choice) : String(choice);
      baseAnswer(id, choice);
      if (a) { const m = send("user", id, {kind: "reply", intent: "answer", body: label, replyRequired: false, silent: true}); for (const v of ask?.via || []) send(v, id, {kind: "info", intent: "answer", replyRequired: false, body: "The user answered: " + label + ".", silent: true}); void m; field.emit(); }
    };
    return field;
  }

  const baseCreate = AgentField.create, baseFromSnapshot = AgentField.fromSnapshot;
  AgentField.create = opts => attach(baseCreate(opts));
  AgentField.fromSnapshot = (...args) => attach(baseFromSnapshot(...args));
  return {attach, KINDS, KIND_LABELS, INTENTS, INTENT_LABELS, SURFACE_KINDS, DELIVERABLES};
})();
