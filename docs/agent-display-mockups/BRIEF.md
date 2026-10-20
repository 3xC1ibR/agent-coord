# Builder brief for the agent-display mockups

This is the contract every mockup page follows. The goal of the set: show the
user, at a glance, **what agents exist, what each is doing and on what, which
sub-agents it has, and what needs the user and what exactly**, when the user can
spin up any number of agents and agents can spin up their own.

## Read first

1. `shared/fixture.js`: the data model and simulation. `AgentField.create({scale})`
   returns a `field` with `agents` and helpers: `byId`, `children`, `parent`,
   `roots`, `depth`, `ancestors`, `descendants`, `treeRows`, `needsUser`,
   `progress`, `blocks`, `impact`, `externalWaits`, `agentWaits`, `asks`,
   `descendantsNeedingUser`, `counts`, `statusLabel`, `relativeTime`, `duration`,
   `answer(id, optionId|text)`, `tick()`, `spawn()`, `dismiss()`, `subscribe()`,
   `events`, `now`. Constants and helpers: `AgentField.PHASES`, `PHASE_LABELS`,
   `DOMAINS`, `DOMAIN_LABELS`, `ROLES`, `ROLE_LABELS`, `PROVIDER_NAMES`,
   `STATUS_LABELS`, `providerLogo(client, label)`, `el(tag, class, text)`,
   `phaseSteps(agent)`, `statusDot(agent)`.
2. `shared/mockup.js`: `Mockup.mount({number, title, spectrum, summary, signals,
   notes, fullBleed, showChrome, defaultScale, render(field, stage, api)})` and
   `Mockup.askBox(agent, field, onOpen)`. The shell owns the top strip, the faux
   app chrome, scale and theme, the Play/Step/Reset transport, the "Signals used"
   popover, and the thread drawer (`api.select(id)`, `api.close()`, `api.note(text)`).
3. `shared/tokens.css`: design tokens and shared components. Use the CSS
   variables; do not hard-code colors. Accent (`--accent`) means running work or
   a primary action. Warm (`--warm`) means attention. Domain hues
   (`--domain-web` …) exist only for territory-style views.
4. `mockups/01-card-refinements.html`: the reference page structure.

## Agent fields

Every agent has: `id`, `title`, `name` (nullable identity), `role`, `kind`
(`app` | `subagent` | `delegate`), `client`, `parentId`, `phase`, `status`
(`working` | `waiting` | `blocked` | `idle` | `failed` | `done`), `summary`,
`activity` (nullable live line), `next {actor, action}`, `ask` (nullable: `kind`,
`question`, `options [{id,label}]`, `freeText`, `blocking`, `askedAt`), `scope []`,
`domain`, `target`, `plan [{label, state, agentId?}]`, `waitingOn []` (agent ids or
`external:…`), `links []`, `cost {tokens, usd, minutes}`, `confidence`,
`createdAt`, `updatedAt`, `checkpointAt`, `stale`, `unread`, `pinned`, `history
[{at, phase, summary, actor}]`, `failure`. Times are ms epochs relative to
`field.now`.

"Needs the user" is `field.needsUser(agent)`: an `ask` is present or `next.actor`
is `user`. `field.asks()` returns them sorted by blocking, impact, then age.

## Page contract

- One file: `mockups/NN-<slug>.html`. Load `../shared/tokens.css`,
  `../shared/fixture.js`, `../shared/mockup.js`; put page CSS in a `<style>`
  block and page JS in one inline `<script>`. Vanilla JS and CSS only, no
  dependencies, no build step, no network.
- Call `Mockup.mount({...})` once. `render(field, stage, api)` must be
  idempotent: rebuild the stage's children each call (it runs on load and on
  every field change from Play/Step/answer/spawn). Keep it fast enough for 160
  agents (`?scale=large`). Preserve the user's own view state (scroll, zoom,
  expanded nodes, sort) across re-renders by keeping it in closure variables.
- Make agents clickable: `api.select(agent.id)` opens the shared drawer. Use
  `Mockup.askBox(agent, field, api.select)` wherever an ask is shown so it can be
  answered in place. Call `field.answer(id, optionId)` for custom answer UI.
- Fill `signals: {current: [...], proposed: [...]}` with the keys from
  `Mockup.CURRENT_SIGNALS` and `Mockup.PROPOSED_SIGNALS` the view actually uses,
  and a one-sentence `notes` on what the view assumes.
- Work in both themes and at all three scales. Degrade gracefully at
  `large`: group, collapse, aggregate, or virtualize rather than overflow.
- Use `showChrome: false` only if the concept replaces the app chrome;
  `fullBleed: true` for canvas-style views.
- Do not edit `shared/*`, `index.html`, `README.md`, or another mockup. If you
  need a helper the fixture lacks, write it in your page. If you find a real bug
  in a shared file, report it in your reply instead of editing it.

## Verify

```
cd docs/agent-display-mockups
./render.sh mockups/NN-<slug>.html            # light + dark PNGs in out/
SCALE=large ./render.sh mockups/NN-<slug>.html
```

Look at the PNGs. Also confirm the page has no script errors: run headless
Chrome with `--dump-dom` on `?scale=large` and check that `#stage` has content.

## Coordinate

Declare your exact file before editing:
`agent-coord begin-work --scope docs/agent-display-mockups/mockups/NN-<slug>.html --activity implementing`.
Save a checkpoint when done. Reply to the handoff message with
`agent-coord reply --message-id <id>` in three to six lines: what the view shows,
the interactions, the signals used, and any shared-file bug you found.

---

## 02 · Agent roster (conservative)

A dense table, a process monitor for agents. One row per agent, indented under
its creator (use `field.treeRows()`), with a disclosure toggle to collapse a
subtree; collapsed rows show roll-up counts (working, needs you, done). Columns:
Agent (provider logo, name or title, role, kind), Doing (activity when working,
otherwise the first sentence of the summary), Phase (stepper + label), Needs
(ask kind chip, or "Waiting on …" for blocked), Agents (child count with dots),
Scope (first scope path, truncated, full on hover), Updated. Sortable column
headers (needs-me first by default, then working, then updated); a filter row
with text search and status toggles. Clicking a row opens the drawer; an ask cell
expands into `Mockup.askBox` inline. At `large`, the table should still be
scannable: sticky header, zebra rows, and the collapse state remembered per
parent across re-renders.

## 03 · Needs-you inbox (moderate)

Split the overview into **Your queue** on top and **Working** below. The queue
lists `field.asks()` as inbox items: ask kind, the asking agent (identity, role,
creator), the question, the options or a free-text answer, "Blocks N agents"
with names, and how long it has waited. Answering removes the item with a short
animation and the agent appears in the working strip. Group the queue by
blocking first, then by kind. Below, **Working** is a compact strip: one line per
working or blocked root agent (dot, title, activity or waiting-on, phase
stepper) with its sub-agents folded into a count that expands on click.
Finished-since-you-left results sit in a third, quieter section with a Dismiss.
A header line gives the three counts: need you, working, finished. At `large`,
the queue should paginate or group by kind with counts, and the working strip
should group by domain.

## 04 · Delegation tree (moderate)

An org chart. Roots across the top, children below, laid out as a tree (HTML/CSS
with connectors, or SVG). Each node: provider logo, name or title, role chip,
phase pill, status dot, activity when working, and an ask summary when waiting.
Needs-you counts bubble up: every ancestor of an agent that needs the user shows
a warm badge with the count of descendants needing input, so collapsed branches
still tell you. Collapse/expand per node (remember state across re-renders).
A toolbar: "Only branches that need me", "Collapse all", "Expand all". Clicking
a node opens the drawer; the ask on a waiting node is answerable inline. At
`large`, start with depth 1 collapsed and let the user open branches; show
per-branch counts on the collapsed edge.

## 05 · Territory map (moderate)

Answer "on what" by *where*. Build a region layout from the agents' `scope`
paths (group by top-level directory, then by the next segment) with a toggle to
lay out by `domain` + `target` instead. Each region is a labeled block sized by
how many agents touch it (treemap-like, CSS grid or flex is fine). Agents are
markers inside their regions: status dot color, name or short title, nested
markers for sub-agents inside the parent's marker. Regions touched by two or
more active agents show an overlap/conflict stripe and a count. Untouched
regions appear faintly so empty territory is visible. Hovering a marker shows
title, phase, activity; clicking opens the drawer. A side list shows asks
(`Mockup.askBox`) and highlights the asking agent's region on hover. At `large`,
regions aggregate: show counts per region and expand a region on click.

## 06 · Swimlane timeline (moderate)

A horizontal time axis (default window: the last 6 hours to now, zoomable with
buttons or wheel). One lane per root agent, with child lanes forking out below
the parent at the child's `createdAt` (indented, connected). Along each lane
draw phase segments colored by phase (use neutral ink tints, not status colors),
checkpoint ticks from `history`, asks as warm stop markers that run from
`askedAt` to the now line (the lane is frozen), external waits as hollow
segments, and finish marks. A vertical **now** line; lanes still working extend a
pulsing accent edge to it. Hover a tick for the checkpoint summary; click a lane
label to open the drawer; click a stop marker to answer in place. A left column
gives identity and status. At `large`, collapse child lanes by default and show
an aggregate strip per root (counts of working / waiting / done).

## 07 · Plan boards (radical)

Assume every agent reports a short `plan`. Each root agent becomes a vertical
board column: header with identity, title, phase stepper, and "k of n"; then
the plan steps as rows (done, active with the live activity line, todo,
blocked with the ask inline, delegated with the sub-agent's own mini-progress
and dot). Sub-agents that are not already a delegated step appear as a trailing
"Agents" row. The top of the page rolls the field up: steps done across all
agents, asks open, agents working, with a stacked progress bar. Columns sort
needs-you first, then working. Step rows are clickable (open the drawer of the
owner or the delegated agent). At `large`, columns become compact cards with
only the active step and a progress bar, expanding on click.

## 08 · Written briefing (radical)

The product writes the status report. Compose prose from the fixture (no model
calls): a first paragraph for what needs the user (each ask as a sentence with
inline answer buttons), then a paragraph per orchestration group for what is
moving (one sentence per agent with its activity and step), then "finished
since you left" with a time-window selector (30 min, 2 h, today) driven by
`history` and `events`. Every agent name or title is a link that opens the
drawer; hovering a name highlights the matching pip in a compact strip of all
agents (status dots, grouped by root) shown at the top or side. Make the prose
honest and specific: counts, names, durations, what is blocked by whom. Provide
a "Read aloud" button that uses `speechSynthesis` if available. At `large`, the
briefing must summarize ("14 agents are implementing across four domains; the
three that need you are…") rather than list everything; include a "show all"
disclosure.

## 09 · Mission control (radical)

A zoomable, pannable canvas (SVG or canvas element) using `fullBleed: true`.
Position each agent by domain (x, columns with labels) and phase (y, rows from
discussion at the top to finished at the bottom); jitter deterministically to
avoid overlap. Nodes: status dot style (working pulses in accent, waiting warm,
blocked hollow, done muted), size by sub-agent count, provider logo on hover
or at high zoom. Edges: creator to child (thin), `waitingOn` (dashed, arrow to
the thing waited on; external waits point to a labeled edge marker). A **dock**
along the bottom lists agents that need the user as answerable cards; selecting
a dock item pans to the node and highlights its edges and impact. Hover a node
for identity, title, activity; click to open the drawer. At `large` and zoomed
out, cluster nodes per cell into a count bubble whose fill reads as a heat map;
zoom in to split. Keep zoom/pan in closure state across re-renders.

## 10 · Focus mode (radical)

One thing at a time. The stage shows only the top item from `field.asks()`:
large title, identity and creator, the ask with options or free text
(`Mockup.askBox` or custom), what it blocks (names, with their status),
the agent's summary and plan, links and scope, and how long it has waited.
A transport: **Answer** (the options), **Skip** (move to the next ask, remember
skipped ones until Reset), **Defer** (hide for 30 simulated minutes). When no
asks remain, show a calm "Nothing needs you" state with the field summary and
the next expected completion. Along one edge, a peripheral strip of pips for
every agent (grouped by root, status colors, warm for needs-you, the current
one highlighted); clicking a pip jumps focus to that agent even if it has no
ask, showing its summary instead. Keep counts visible: "2 of 5 need you".
Use `showChrome: false` so the mode feels like a different place.

---

# Round two: agents as marks, not cards

The user reviewed round one. What landed: the delegation tree's visible
relationships; the mission-control canvas as "a completely different way to
represent an agent" (tiny dots, hover to see what it is doing, zoom in to
split clusters); approving a permission request straight from the home page;
cards that show what the model is actually doing; plan steps as "what is it
working on and what is next" without reading the thread; collapsible sub-agents
under a parent; the territory idea that scopes already give us "where" for free.
What did not land: summary stat lines ("16 agents · 9 trees · 4 need you…"),
the wall-of-text briefing, the plan-boards and territory views being
overwhelming at a glance, and too many waiting-on edges. The user is also wary
of asking the model to report much more than it already does.

Round two riffs on that. Every round-two page follows these principles:

1. **An agent is a mark, not a card.** A dot, star, cell, line, or particle.
   Identity appears on hover (a native `title` is fine). Detail appears in the
   shared lens on click: `api.peek(agent.id, anchorElement)` (or
   `api.peek(id, null, {x, y})` for canvas coordinates). The lens shows now,
   the tool it is using, next, the last/current/next step, its sub-agents
   (collapsible), and the ask with inline answers. `api.select(id)` still opens
   the full drawer; use it only from the lens's "Open thread".
2. **Needs-you is the most prominent thing on screen and answerable in
   place.** A permission or decision can be accepted without opening anything.
   Put it at the top, the center, or the surface, never in a bottom dock.
3. **Sub-agents are relationships, not cards.** Satellites, star-lines,
   orbits, nested cells, forks. The parent collapses them; expanding shows what
   each did (`field.recentSteps`, `field.toolLabel`). A sub-agent that needs
   the user still surfaces on its own, labeled with its creator.
4. **Age sinks.** Fresh activity floats, brightens, or moves; old results
   sink, dim, or drift to the edge. Use `field.age(a)` and
   `field.lastActivity(a)`, which come from tool pulses (`a.pulses`) and the
   last update, not from the checkpoint. Nothing disappears unless the user
   dismisses it.
5. **Direct manipulation.** Where there is a canvas, the user can drag marks
   (`Mockup.drag(el, {onMove, onEnd})`). A dragged mark is pinned: it keeps
   its place across re-renders and no longer sinks. Offer "Reset layout".
6. **No stat lines.** No "N agents · N working · N done" headers. Counts
   belong on the marks themselves (a cluster bubble, a satellite count).
7. **Edges are sparse.** Lineage is faint or implied by proximity. Waiting-on
   edges appear only on hover or peek, or when they block something that
   needs the user.
8. **Prefer free signals.** Runtime status, `tool_activity` (`a.lastTool`,
   `field.toolLabel`), `pending_approval`, `age`, write scope, parent/child,
   and the checkpoint fields the agent already reports (phase, summary,
   next_action). Treat `plan`, ask options, `name`, `role`, `domain`, and
   `target` as optional enrichments: the view must read well without them and
   get better with them. Say which is which in `signals`.
9. **Works at 160.** Aggregate into counted clusters; zoom, click, or hover to
   split. Keep user view state (zoom, pan, pins, expansions) in closure
   variables across re-renders.
10. **Keep motion meaningful.** Working marks pulse or move; everything else
    holds still. Respect `prefers-reduced-motion`.

Reference page: `mockups/11-settling-field.html`.

## 12 · Constellations

The delegation tree as a night sky. Each orchestration group is a
constellation: the root is the brightest star, children are stars joined to it
by faint lines, grandchildren by fainter ones. Standalone agents are lone stars.
Brightness and size follow recency (`field.age`): a star that just made a tool
call is bright with a soft halo; one that finished hours ago is a dim point.
Needs-you stars pulse warm and get a short label with the ask kind and a
primary answer button right there (other options in the lens). Constellations
are placed on a free canvas (deterministic seed), sorted so the ones with
needs-you are nearest the top; whole constellations drift downward as their
newest activity ages, and done constellations settle near the bottom edge.
Drag any star to move its constellation (pins it). Hover shows name/title;
click peeks. At 160, distant constellations render as a single labeled glow
with a star count; zoom (wheel or buttons) resolves them. No stat lines.

## 13 · Orbits

Each root agent is a sun; its sub-agents orbit it, depth as orbit radius,
grandchildren as moons. Orbital motion is activity: a working planet moves
along its orbit, an idle one holds still, a done one is a small dim body at
the outer edge, a waiting one glows warm and stops. The sun's own state shows
in its corona (accent while working, warm while needing you, hollow while
blocked). Systems sit on a free canvas sized by total activity in the last
hour (`field.pulsesSince`); old systems sink and shrink, recent ones float up.
Hover any body for name/title and what it is doing; click peeks. Clicking a
sun zooms into that system so planets get labels and their last step; Escape
or a back button zooms out. Needs-you suns and planets are also listed in a
compact surface strip at the top with a primary answer button each. Drag a
system to pin it. At 160, systems collapse to discs with a planet count and
zoom resolves them.

## 14 · Vital signs

Every agent is a sparkline, not a card: one thin row per agent showing tool
pulses over the last hour (`a.pulses`, `field.pulsesSince`) as ticks or a
pulse trace, colored by status, with the activity label at the right edge
(`field.toolLabel`). A waiting or failed agent flatlines from `askedAt` with a
warm marker and a primary answer button inline; a blocked agent shows a hollow
flat segment labeled with what it waits on. Sub-agents are thinner traces
nested under their parent, collapsed by default into a bracket with tiny
status dots; expanding shows each one's trace and last step. Rows order by
needs-you first, then by last activity, so stale rows sink to the bottom and
dim. Hover a row for title and identity; click peeks. At 160, rows are 14 px
and still legible; a sticky time axis and a hover crosshair give the time.
This page should make the case that "what is it doing" can come from the tool
stream with no extra reporting from the model.

## 15 · River

Phases as a flow from left (discussion) to right (finished). Each agent is a
particle moving downstream; its x position is its phase plus progress within
the phase, its drift speed is recent activity. Working particles move, idle
ones sit in an eddy, blocked ones hover in place with a hollow outline.
Needs-you particles are caught on a rock at the top of the stream: each rock
shows the particle, a short ask label, and a primary answer button; answering
releases it back into the flow. Done particles flow into a delta at the right
edge and settle downward as they age (the delta is the sediment). Sub-agents
travel as smaller particles alongside their parent, joined by a short thread;
a parent collapses its sub-agents into a count when more than three. Hover
shows name/title and activity; click peeks; drag a particle to pin it where it
is. At 160, particles in the same phase band merge into counted schools that
split on hover or zoom. No stat lines.

## 16 · Radar

What needs you is at the center. Concentric rings: the bull's-eye holds
needs-you blips with a label and a primary answer button each; the next ring
holds working blips; then idle; then blocked and waiting-on-others; done
blips drift past the outer edge and fade. Angle is domain (seven sectors,
labeled on the rim). A slow sweep highlights blips that made a tool call in
the last two minutes so you can see where the activity is. Sub-agents are
small blips tethered to their parent by a short line; the parent shows a
count when collapsed, and hovering or clicking the parent expands them.
Hover shows name/title and activity; click peeks; drag pins a blip (it stays
at its radius and angle). At 160, blips in the same sector and ring merge into
counted arcs that split on zoom (wheel or buttons). Edges to what a blip waits
on appear only while hovering or peeking it.

## 17 · Breathing grid

A dense grid of small cells, one per root agent, like a contribution graph
that is alive. Each cell breathes at the agent's activity rate: a working cell
pulses at its pulse cadence, an idle cell is still, a done cell is dim, a
needs-you cell glows warm and shows a small primary answer button on hover.
Cells are sorted by recency so fresh ones sit in the top rows and old ones
sink to the bottom rows; the grid never scrolls at 160 (cells shrink to fit).
Sub-agents are sub-cells inside their parent (a 2×2 or 3×3 inset), collapsed
to a count when more than four; clicking the parent's corner expands it in
place to show each sub-agent's last step. Hover shows name/title and the tool
it is using; click peeks the lens. Drag reorders a cell and pins it (pinned
cells stay in place as the rest re-sort). A "needs you" filter dims every
other cell. No stat lines.


---

# Synthesis: 18 · Atlas

Built in the orchestrating thread from the user's direction after round two:
settling field was the favorite; keep a quiet needs-you panel like mission
control's, with "show on map"; the direction is spatial (physical-book memory);
constellations' blinking needs-you dots and hub-and-spoke groups; orbits' zoom-in
on click but no continuous motion; domains as amorphous clusters on one canvas
rather than rigid columns, zooming into a cluster to a view with needs-you on
top and older work at the bottom. For illustration, the fixture gained domain
scenarios (`AgentField.create({domains})`: clean, coarse, fine, messy, none) and
the shell a selector (`domainScenarios: true`), so the map can be judged when
the model reports few, many, overlapping, or no domains.

Revision after review: three zoom levels (all domains, one domain, one
orchestrator with its sub-agents), a scatter instead of an age-ordered list,
dot size from how many messages the user exchanged with the agent (`userTurns`,
a runtime signal added to the fixture), and dot color from age (full accent for
recent activity, fading over a day). State moved to a ring so color can carry
age. Positions are shared by all levels so the far view is a true overview.

Real data: `export-threads.py` snapshots the live threads (now carrying
`domain`, `domain_source`, and `scope_cluster`) into `shared/real-threads.js`;
`AgentField.create({scale: "real", workspace})` maps them into the same agent
shape, and the shell offers the snapshot and a workspace selector when the
script is loaded. The count line above cluster titles was removed on request.

---

# Round three: orchestration (mockups 19–28)

Rounds one and two asked "what agents exist and what are they doing". Round
three asks how they **work together**: when the user can spin up any number of
agents and each can spin up its own, show

1. which agents are messaging each other, and why;
2. how they collaborate (delegation, handoffs, peer coordination, scope
   negotiation, review);
3. what agents are there (is the title the right identity? see below);
4. what each is doing and **on what** (phase is not enough: add the surface it
   works on: a page, service, screen, doc, pipeline, build, or question);
5. what sub-agents each has, at any depth;
6. whether each is working, waiting on another agent, or waiting on you;
7. what needs the user and **exactly what**, including asks that were relayed
   up a chain of agents before reaching you.

## Data: `shared/orchestration.js`

Load it **after** `fixture.js` and **before** `mockup.js`:

```html
<script src="../shared/fixture.js"></script>
<script src="../shared/orchestration.js"></script>
<script src="../shared/mockup.js"></script>
```

It wraps `AgentField.create`, so the `field` passed to `render` also has:

- `field.messages`: `{id, from, to, at, kind, intent, body, replyRequired,
  inReplyTo, exchange, about, deliveredAt, ackedAt, repliedBy, wokeTurn}`.
  `from`/`to` are agent ids or `"user"`. `kind` is today's classification
  (`creation`, `action_required`, `info`, `reply`, `closure`, `failure`);
  `intent` is proposed (`assign`, `question`, `answer`, `handoff`, `finding`,
  `review`, `blocker`, `coordinate`, `status`). Labels: `field.KIND_LABELS`,
  `field.INTENT_LABELS`.
- Queries: `field.messagesOf(id, {since})`, `field.between(a, b)`,
  `field.openRequests()` (reply owed), `field.owes(id)`, `field.awaiting(id)`,
  `field.edges({since, includeUser})` (one per pair: `a, b, count, ab, ba, last,
  open, intents, kinds`), `field.exchanges()`, `field.collaborators(id)` (peers
  outside the parent/child line), `field.conflicts()` (overlapping write scopes:
  `a, b, path, negotiating`), `field.teams()` (root + members + counts + needs +
  depth), `field.chain(id)` (root → agent), `field.askPath(id)` (`via` agents
  the ask passed through, `blocks` agents it holds up), `field.nameOf(id)`.
- Actions: `field.message(agentId, text)` (user → agent), `field.reply(messageId,
  text)`, plus the existing `field.answer`, `field.spawn`, `field.tick`. Play and
  Step generate messages: creations on spawn, handoffs on finish, blockers
  escalated to the parent when an ask appears, status/finding notes, peer
  coordination, and replies to open requests.
- Proposed per-agent signals: `a.surface {kind, label}`, `a.contract {from,
  deliverable, acceptance [{label, met}], replyRequired, due}` (children only),
  `a.deliverable {kind, label, state}` (`none|draft|ready|delivered`),
  `a.health` (`on-track|slipping|stuck`), `a.ask.via` (relay chain ids).
- Trees are up to four levels deep at `medium`/`large`. `small` has the
  hand-written Rig upload team (Procurement s01 → Harbor s02 → Gravity s03 →
  test subagent; UI s04; reviewer Rook s05), the dispatcher s00 routing three
  researchers who also talk to each other, the mobile push team (Pager s09 with a
  subagent, a test writer, and a CI-watch delegate), and a scope negotiation
  between s15 and s09.

The drawer (`api.select`) now shows the agent's recent messages and its
contract. New signal keys for `signals`: current `agent_messages`, `creation`,
`conflicts`; proposed `intent`, `surface`, `contract`, `deliverable`,
`escalation`, `health` (plus every earlier key).

## Principles carried from round two

The user kept: visible delegation, answering from the home page, plan steps as
"now and next", collapsible sub-agents, scopes as "where". The user rejected:
stat lines, walls of text, overwhelming plan/territory views, and **dense
edges**. So:

- **Edges are sparse by default.** Show message lines for the focused or hovered
  agent/team, recent traffic (last ~15 simulated minutes) fading with age, or
  aggregated per team. Never draw all 600 messages at once.
- **Needs-you is the most prominent thing** and answerable in place
  (`Mockup.askBox`). When an ask was relayed (`ask.via`), show the path
  ("Rook → Procurement → you") and what it blocks.
- **Identity**: show `name` when present, otherwise the title, then role and
  surface ("Gravity · implementer · Upload API"). The title is the assignment,
  which changes; the identity should not. Each mockup takes a position on this.
- **On what**: use `surface` (proposed) and scope (today) rather than phase alone.
- Works at 185 agents (`large`): group by team, aggregate edges, collapse.
- Meaningful motion only: a message in flight, a pulse on arrival.
- Clearly separate "today" from "proposed" via the Signals popover; each page's
  `notes` should say what the view would cost the model to report.

## Page contract

Same as round one (one file `mockups/NN-<slug>.html`, vanilla, no network,
idempotent `render`, `api.select`, both themes, three scales, do not edit shared
files or other mockups; report shared bugs in your reply). Use the spectrum
label given below. Verify with `./render.sh` (small and `SCALE=large`) and look
at the PNGs; use `probe.js` to exercise an interaction (answer an ask, click a
node) headlessly.

## 19 · Conversation cards (conservative)

Today's home page cards, grouped by team (root card with its members nested
and collapsible). Each card adds one **"talking with"** line: tiny avatars of
the counterparts it messaged in the last hour with the latest intent ("asked
Gravity about the session key · 3m"), and one **obligation** line when present:
"Owes Harbor a reply · 12m" or "Waiting on Gravity's reply · 6m". Relayed asks
show the path. Sub-agent chips show status dots. Only today's signals plus
`intent`; say so. Clicking a counterpart avatar opens that agent.

## 20 · Message ledger (conservative)

A process monitor for orchestration: a tree table (team → members, any depth)
with columns Agent (identity), On (surface + scope), State (phase + status),
**Owes** (replies it owes, oldest age), **Awaiting** (replies it is waiting
for), **Last message** (direction arrow, counterpart, intent, age), Needs you.
Expanding a row reveals its message exchanges inline as a compact threaded list
(grouped by `exchange`). Sortable; default sort puts needs-you, then oldest
owed reply, first. Sticky header; works at large.

## 21 · Live wires (moderate)

The delegation tree drawn as an org chart, with **messages as live wires**:
parent/child edges are always shown faintly; when a message is sent (Play), a
dot travels along the edge (peer messages arc between siblings or across
teams). Edges with an open request are dashed and labeled with the wait age.
Hover or focus an agent: its edges brighten, others fade, and a mini log of its
last messages appears. Needs-you badges bubble up to every ancestor; relayed
asks light the relay path toward a "You" node at the top. Collapse/expand
subtrees; at large start collapsed to teams with aggregated traffic counts.

## 22 · Sequence diagram (moderate)

A UML-style sequence diagram per team: lifelines across the top (You first,
then the root, then members in tree order, sub-agents nested under a bracket),
time flowing down. Messages are arrows labeled with intent and a short body;
replies are dashed returning arrows; open requests are arrows ending in a
pulsing "awaiting reply" stub. Phase is a colored activation bar on each
lifeline. Asks to the user arrive on the You lifeline and can be answered there,
creating the reply arrow. A team picker (or stacked teams), time-window control,
and hover for the full body. At large, pick one team at a time with a team list.

## 23 · Team channels (moderate)

Orchestration as group chat. Left rail: one channel per team (unread counts,
needs-you badge), plus a "Direct" section for cross-team pairs. The channel shows
agent-to-agent messages as chat lines with avatar, identity, intent tag, and
body; threaded replies collapse under the message they answer. Asks to the user
are pinned at the top of the channel with inline answers. A composer lets the
user post to the channel or @mention one agent (`field.message`). A members
panel on the right shows the team tree with status and surface. Play makes new
messages stream in.

## 24 · Surface map (moderate)

Answer "on what" with the product, not files: nodes are **surfaces** (pages,
services, screens, docs, pipelines, builds, questions) grouped by kind, laid out
as a map. Agents attach to the surface they work on as small marks with status.
Messages between agents become arcs between surfaces ("the Upload page team is
talking to the Upload API team"), weighted by recent count. Write-scope
conflicts glow on the surface where two teams overlap, with the negotiation
state. Click a surface: its agents, their asks, and its message traffic. Toggle
to lay out by scope directory instead of surface (what we have today).

## 25 · Contracts (radical)

Orchestration as **contracts between agents**. Every delegation is a card:
creator → assignee, deliverable, acceptance checks (ticking as met), due, and
state columns **Assigned → In progress → Handed off → Accepted** (plus
**Needs you** where the contract's acceptance or an ask waits on the user).
Sub-contracts nest inside their parent's card as a count you expand. A handoff
shows the deliverable (PR, findings, build) and an Accept / Send back action for
the creator (or for the user when they are the creator). Shows what the
checkpoint would need: `contract`, `deliverable`, `intent: handoff`.

## 26 · Comms matrix (radical)

Who talks to whom at 185 agents: an adjacency matrix with agents on both axes,
ordered by team and tree (team blocks along the diagonal, You as the first
row/column). Cells are colored by recent message count and marked when a reply
is owed; off-diagonal blocks reveal cross-team collaboration. Hover a cell for
the exchange; click to see it. Brushing a row highlights the agent's partners.
A toggle switches to a **chord/arc view** of the same data aggregated by team.
Small multiples of the last hour show how the pattern changed.

## 27 · Critical path (radical)

The user as a node in the dependency graph. Build a DAG from `waitingOn`,
open requests, and asks: agents wait on agents, on external things, and on
**You**. Lay it out left (blocked) to right (unblocking), and compute each root
outcome's **critical path**: the chain that finishes last. You see "You are
blocking 7 agents across 3 teams; answering Harbor's release ask unblocks the
longest chain." Answering an ask in place reflows the graph. Each node shows
identity, surface, health. Non-critical work fades.

## 28 · Office floor (radical)

A transformative, spatial metaphor: a floor plan. Each team is a room (nested
rooms for sub-teams); agents are people at desks (avatar, identity, a speech
bubble with the current activity). Messages are paper notes that fly between
desks; an open request leaves a note waiting on the recipient's desk. Agents
that need you walk to **your desk** in the corner and queue with their ask
(answerable there; the relayed path is a trail). Idle agents dim, done agents'
desks show their deliverable. Zoom from floor to room. At large, rooms shrink to
occupancy heatmaps until you zoom in.

---

# Real records and message dots (27 revision, 29)

The user asked for a new mockup in the vibe of Atlas (18) but focused on
orchestration, and for Critical path (27) to lay the agent tree out
vertically. Both must **use dots floating between agents to show message
communication**, and both must **render the user's actual records**.

## Real data

`python3 docs/agent-display-mockups/export-threads.py` writes
`shared/real-threads.js` (git-ignored) with threads **and now messages**:
`snapshot.messages` (agent-coord messages from the last 14 days touching an
exported thread; `--message-days N` changes it) and `snapshot.parties` (names
of counterparts outside the snapshot). Load it before `fixture.js`:

```html
<script src="../shared/real-threads.js"></script>
<script src="../shared/fixture.js"></script>
<script src="../shared/orchestration.js"></script>
<script src="../shared/mockup.js"></script>
```

With `?scale=real`, `AgentField.fromSnapshot` builds the field and
`orchestration.js` fills `field.messages` from the real rows: `kind` from the
database (creation from the create receipt, closure, reply via `in_reply_to`,
info, request), `replyRequired`, `repliedBy` (as `message_timeline` computes
it), `closed`, `wokeTurn`, `deliveredAt`, `ackedAt`, real `body`, `at`. `intent`
is guessed from the body and flagged `intentDerived: true`; say so in the UI.
`field.outsideMessages` counts messages to threads outside the current
workspace filter; `field.nameOf` resolves outside parties. In real mode Play
and Step invent nothing; asks come only from checkpoints with
`next_actor=user` (free-text, no options). Real fields have up to ~270 agents,
~1,200 messages (≈600 in the last 24 h), families up to 17 members, depth 2.

Default to the real snapshot when it is present:
`defaultScale: globalThis.AGENT_COORD_SNAPSHOT ? "real" : "small"`, and use
`projectTabs: true` like Atlas so the user can switch workspaces. Keep the
simulated scales working when the snapshot is missing (the file is not
committed), and show a short hint to run the exporter.

## Message dots

A message is a small dot that travels from sender to receiver along a soft
curve and is absorbed with a tiny pulse. Colors follow the existing tokens:
accent for ordinary traffic, warm for blockers and anything involving the user,
green for handoffs; dashed or hollow dots for requests still awaiting a reply.
Because the real snapshot is static, dots come from a **replay**: a time
cursor sweeps through the message log (default: the last 24 h compressed into
about 60 s, looping), launching each message's dot when the cursor passes its
timestamp. Provide a compact scrubber (play/pause, speed, window: 1 h / 24 h /
7 d) and a "live" end where open requests keep a slow, pulsing dot waiting at
the recipient. With the simulated scales, new messages from Play/Step launch
dots as they happen. Keep it sparse: never more than ~40 dots in flight, and
respect `prefers-reduced-motion` (show static ticks instead).

## 27 · Critical path, vertical

Keep everything that works (the "You are blocking N agents" headline, the
ranked asks rail answered in place, reflow on answer, hover chains, critical
path versus every wait). Change the layout so the **agent tree runs vertically**:
the root outcome on top, the agents it waits on below it, deeper waits further
down, and You at the bottom, as the thing the longest chains end on, or as a
docked rail. Edges are vertical curves. Message dots travel along those edges
(and between peers). With real data the dependency graph comes from creation
lineage, open `replyRequired` messages, unanswered creations, and
`next_actor=user` asks.

## 29 · Orchestration atlas (synthesis)

Atlas's vibe (calm spatial canvas, territories that keep their place, dots as
agents, blinking needs-you rings, a quiet needs-you panel that flies you to the
agent, zoom from overview to a family) with orchestration as the subject:

- **Families are the territories.** Each root and its descendants form an
  organic hull (reuse `shared/atlas-layout.js` ideas or your own deterministic
  layout); the root is a hub, children orbit it, grandchildren orbit their
  parent. Solo threads gather in a quiet commons by workspace or domain.
- **Messages are dots floating between agents** (above), within a family and
  across families, so you can see which families talk to each other. Edges are
  not drawn at rest; a faint trail shows the last few hops and fades.
- Agent dot size is message volume (sent + received); ring is state; color is
  recency like Atlas. Labels show identity (name, else title), and the surface
  or domain on hover.
- Zoom: overview → a family (its members bloom with labels, a mini log of its
  recent messages, open requests as dots waiting at the recipient) → an agent
  (lens via `api.peek` or the drawer via `api.select`).
- The needs-you panel lists checkpoint asks (answer in place for simulated
  fields; for real ones "Open thread") and open requests older than ~15 min
  ("Harbor waits on Gravity · 40m"), each flying the camera to the agent.
- Pan, zoom, and pinned positions persist in localStorage.

## 30 · Working set (moderate)

Revision agreed October 20, 2026: one horizontal axis for groups and one
vertical axis for state. Preserve the card view and mini-map; remove the
recency cutoff that made whole groups disappear.

- **Left/right changes group at every level.** All groups remain reachable,
  including groups with no running agents. The selected group stays the same
  when moving vertically. Group order starts by recency and stays stable while
  replies arrive, are read, or change phase.
- **All groups is fixed at the far left.** It aggregates the selected project's
  threads, including Ungrouped, through the same three states. Each card shows
  its group with a link that opens that group at the current level. Individual
  groups follow to the right. Section caps apply to the aggregate independently
  of individual groups.
- **Three levels, top to bottom:** Running + Needs my reply; Replies to read;
  Idle. Fresh visits start at All groups on the middle level; returning restores
  the group, grouping mode, and level per dataset/workspace using browser
  storage. Invalid or removed group keys fall back to All groups; unavailable
  storage leaves normal navigation usable. The top level has separate Needs my reply
  and Running sections. Idle has one column per populated checkpoint phase,
  including Finished, with cards stacked beneath each heading. Columns wrap
  on smaller screens without introducing a competing horizontal scroll area.
  A finished turn does not imply a finished investigation.
- **Thread membership is exclusive.** Running agents and required user replies
  occupy the top level. Other unread responses occupy the middle. All remaining
  threads occupy Idle. Nonrunning threads parked in Later remain in Idle and
  retain their Later label; a running turn stays visible at the top.
- **Real response receipts.** `export-threads.py` supplies `unread_result` from
  the current turn completion and `seen_completion_id`, excluding interrupted
  turns. A checkpoint update alone does not count as a response. Old snapshots
  without this field report that read status is unavailable, rather than
  guessing from phase or recency. `Mark read` changes only the preview and moves
  a response to Idle without changing its phase. Reload restores the snapshot.
- **Cards are selected per thread.** Show every qualifying member independently,
  including descendants whose parents occupy other levels. Parent links open
  context without bringing older siblings into the current view. For real data,
  cap each populated section at 12 cards with Show all / Show recent controls.
- **Groups.** Real data uses the named group (or the root's group for children
  without one), otherwise Ungrouped per workspace. `g` cycles group, domain,
  workspace. Simulated data uses root target, domain, or root family.
- **Minimal labels.** Show the group name and state without repeating the
  selected project, page position, general thread totals, or state descriptions.
  Only required replies and unread responses show counts, in their headings
  and mini-map cells. Running and Idle use presence dots; empty cells are blank.
  The project and data selectors omit counts.
- **Navigation.** Arrow keys, horizontal wheel/swipe, the group picker, and
  mini-map cells change group. Up/down keys and vertical wheel/swipe at a page
  edge change state. The mini-map has aligned group columns across all three
  levels and keeps empty cells navigable. Empty states link to populated levels of
  the same group. Preserve scroll position per group and level. Text inputs and
  the thread drawer retain their own keyboard behavior.
- Keep short transitions, respect reduced motion, and do not add eyebrow labels.

---

## Round five: the third dimension

Three parallel explorations of a 3D card view, each a different camera
relationship to the same field. Shared rules on top of the page contract:

- Build the scene with CSS 3D transforms (`perspective`, `preserve-3d`,
  `translate3d`, `rotate3d`) so every card stays a DOM node: text is crisp,
  `Mockup.askBox` answers in place, `api.select` opens the drawer. Canvas only
  for non-interactive backdrop (floor grid, fog, connectors, far dots). No
  WebGL, no raycaster, no textures or 3D models.
- Groups: derive them as 30 does (root family keyed by the root's `target`;
  `g` cycles target, domain, root family).
- Nothing that needs the user may be hidden by geometry or sit outside the view
  without a visible indicator. A mini-map ("where am I") is required.
- Chrome beyond the cards: one title line and the mini-map. No search, filters,
  or toolbar.
- `prefers-reduced-motion`: no autonomous camera motion, instant transitions.
- At `large`, bound the live card DOM: aggregate the old, idle tail into
  summary slabs, plaques, or spheres.

Derived signals, computed in the page and named in `notes` (use at most four as
primary channels): **heat** (decayed activity from `pulses` and `lastTool.at`,
half-life ≈ 20 simulated min), **mass** (`turns + history.length +
cost.minutes / 10`), **momentum** (phase transitions in the last simulated
hour), **fan-out** (`descendants(id).length`), **neglect** (age of an
unanswered ask), **group gravity** (sum of heat over a group), **stuck** (long
in one phase with summary churn and no phase change).

## 31 · Hangar (3D)

Fixed viewpoint at the front of a long hall; nothing is ever behind the camera.
Depth is recency (perspective shrinks old things for free, so apparent size reads
as mass × recency). Left to right is group, as labeled floor lanes ordered by
group gravity, hottest at the center. Height is attention: asks rise to eye
level and face the camera, working stands mid-height, idle and done sit on the
floor. Card size at z=0 is mass; fog and desaturation carry age independently
of depth. Keys: `←`/`→` strafe a lane, `↑`/`↓` (or `W`/`S`) walk into the past
and back along a floor time ruler (now, 10m, 1h, 6h, 1d, 1w), `Space`/`Home`
snap to the front, `Shift+↑` looks up at the needs-you row; wheel and drag
mirror these. Near cards are the full 01 card, mid-distance collapse to name
and dot, far cards are slabs, and a lane's tail aggregates into one "N older ·
last touched 3d" slab. Mini-map bottom-right: a top-down plan, lanes as
columns, time bands as rows, camera marker, warm dots; click to fly.

## 32 · Rooms (3D)

The literal version: first person, one room per group on a simple floor plan
(corridor or hub; say which in `notes`). Room size scales with group mass,
brightness with group gravity; dead groups are dim and dusty. Inside: root on
the far wall, sub-agents on the side walls by status, idle low near the floor,
working at eye level with the activity line, needs-you stepping into the middle
and glowing warm. When you are elsewhere, asks stand in their doorway or come
down the corridor, and every door shows a warm badge when its room has an ask.
Movement: `WASD`/arrows, `Q`/`E` or drag to turn (yaw only), `Tab` cycles
rooms, `1`–`9` jump, `Enter` selects the nearest card, `Esc` returns to the
corridor; jumps are 300 ms glides. Rooms you are not in collapse to a door
plaque at `large`. Mini-map top-right: floor plan, position, facing cone, warm
dots; click a room to glide. `notes` must say honestly what walking costs
against the 2D card view and whether door badges plus the map compensate.

## 33 · Cloud (3D)

No architecture: a rotatable 3D scatter of agents. Groups spread around a ring
so the top view reads as clusters; within a group, sub-agents orbit their root
with radius by tree depth. Up is attention (neglect pushes higher and pulses),
toward the default camera is heat, size is mass, opacity and saturation are
age. Thin lines join root to children; `waitingOn` edges are dashed. Clusters
with near-zero group gravity collapse to one labeled sphere until clicked.
Orbit camera: drag rotates, wheel zooms, `Shift`+drag pans; presets `1` front,
`2` top, `3` side, `0` reset; `←`/`→` orbit 15°, `↑`/`↓` tilt. Cards ease
toward new positions on each tick with slight repulsion. At `large`, positions
live in typed arrays, only the nearest ~80 cards materialize as DOM, the rest
are canvas dots. Chrome: title, a gnomon mini-map with an axis legend and warm
count, and an asks chip strip that flies the camera to a card. `notes` should
say which channels won and whether the shape told you anything a grid would not.
