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
