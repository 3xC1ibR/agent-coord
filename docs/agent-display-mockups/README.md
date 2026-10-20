# Agent display mockups

Interactive mockups exploring how Ribbon Field could show the user's agents:
what agents exist, what each is doing and on what, which sub-agents it has, and
what needs the user. They range from a conservative restyle of today's card to
views that assume a richer checkpoint. Open `index.html` for the gallery.

The mockups are static HTML with no dependencies and no build step. They do not
touch the coordination database; every page renders a simulated field of agents
from `shared/fixture.js`.

## Open and drive a mockup

```
open docs/agent-display-mockups/index.html
open "docs/agent-display-mockups/mockups/03-needs-you-inbox.html?scale=large&theme=dark"
```

- **Scale**: `?scale=small` (16 hand-written agents based on real threads),
  `medium` (≈45), `large` (≈160). The selector in the top strip switches.
- **Theme**: `?theme=light|dark`; defaults to the system preference.
- **Simulation**: **Play** advances one simulated minute every 1.2 s; **Step**
  advances once; **Reset** restores the fixture. Working agents advance plan
  steps, change phase, raise asks, spawn sub-agents, wait on external things,
  and finish. Asks stay until you answer them in the page.
- **Thread drawer**: clicking an agent in any mockup opens the same drawer:
  identity, status, ask, summary, plan, waiting-on, scope, links, children,
  cost, and checkpoint history.
- **Signals used**: the popover in the top strip lists which signals the mockup
  relies on, split into what the checkpoint carries today and what it would
  need to add.
- Keys: `[` previous mockup, `]` next, `.` step, `Esc` close the drawer.

## Real data

Threads now carry `domain`, `domain_source`, and `scope_cluster`. The Atlas
mockup can run on a read-only snapshot of your actual threads:

```
python3 docs/agent-display-mockups/export-threads.py            # Now and Later threads
python3 docs/agent-display-mockups/export-threads.py --archived # include closed threads
```

This writes `shared/real-threads.js` from the coordination database (the same
SQLite file the app and CLI use), joining each thread with its latest
checkpoint and history, lineage (app-agent creations and terminal delegations),
live session state (write scope, last tool call, whether a turn is active),
completed-turn counts, and links. Open Atlas and choose **Real snapshot** in the
top strip, then pick a workspace. The snapshot is generated output and is not
committed; regenerate it whenever you want the map to reflect the present.

In the snapshot, dot size comes from completed turns (the closest free proxy
for messages with you), color from the last update or tool call, and asks from
checkpoints whose next actor is the user. Threads with no domain fall back to
their scope cluster, and clusters built only from scope clusters say so.

## Render screenshots

```
docs/agent-display-mockups/render.sh                 # every mockup, light and dark, small scale
SCALE=large docs/agent-display-mockups/render.sh mockups/09-mission-control.html
```

Output goes to `out/` (ignored by git) with a contact sheet per scale. The index
uses `out/*-small.png` as thumbnails when present.

## The signal question

A checkpoint today carries `title`, `phase`, `summary`, `next_action`,
`next_actor`, and `links`. The app adds runtime state, provider, unread and
stale flags, the begin-work write scope, orchestration parent/child links, and a
reply classification. That is enough for "what is it doing" and a coarse "does
it need me", but a glance cannot tell:

- **who** an agent is apart from its current task (the title changes with the
  assignment; there is no stable identity, role, or kind);
- **what exactly** it needs (the next action is free text, so it cannot be
  answered in place, prioritized, or measured for what it blocks);
- **on what** it works beyond file globs (no domain, page, or service);
- **how far along** it is (no steps, no live activity line, no expected time);
- **which provider-native subagents and terminal delegates** exist (only app
  agents created in orchestration mode are linked).

The proposed additions used by the later mockups are deliberately small and
agent-reportable in the same `checkpoint` call: `name`, `role`, `kind`, `ask`
(kind, question, options, blocking), `domain`, `target`, `plan` (steps with
states, including delegated steps that name the sub-agent), `activity`,
`waiting_on`, and `cost`. Everything else in the mockups (impact, roll-up
counts, timelines, briefings) is derived from those plus history.

## The ten mockups

| # | Mockup | Spectrum | Idea |
| --- | --- | --- | --- |
| 01 | Card refinements | conservative | Today's layout; the card gets an identity row, phase stepper, activity line, answerable ask, sub-agent chips. |
| 02 | Agent roster | conservative | A sortable, filterable tree table; a process monitor for forty-plus agents. |
| 03 | Needs-you inbox | moderate | Structured asks answered in place at the top; working agents become a quiet strip. |
| 04 | Delegation tree | moderate | An org chart with needs-you counts bubbling up to every ancestor. |
| 05 | Territory map | moderate | Agents placed over the files, services, or domains they touch; overlaps are conflicts. |
| 06 | Swimlane timeline | moderate | Lanes over time with phase segments, checkpoint ticks, asks as stop markers, child lanes forking. |
| 07 | Plan boards | radical | Each agent reports a plan; cards become progress columns; delegated steps are sub-agents. |
| 08 | Written briefing | radical | A written status report composed from checkpoints with every noun linked. |
| 09 | Mission control | radical | A zoomable canvas by domain and phase with delegation and waiting-on edges; a dock for blocked agents. |
| 10 | Focus mode | radical | One ask at a time with full context; the rest of the field is a peripheral strip. |

## Round two: agents as marks

After reviewing round one, the user kept: visible delegation relationships,
the canvas as a different way to represent an agent (dots, hover, zoom to
split), approving a request from the home page, cards that show what the
model is doing, plan steps as "what now and what next", collapsible
sub-agents, and the territory idea that scopes already tell us "where". Not
kept: stat lines, the wall-of-text briefing, overwhelming plan and territory
views, and dense waiting-on edges. The user is wary of asking the model to
report much more than it already does.

Round two (mockups 11–17) follows ten principles, written out in `BRIEF.md`:
an agent is a mark, not a card; needs-you is the most prominent thing and
answerable in place; sub-agents are relationships; age sinks; direct
manipulation pins; no stat lines; sparse edges; prefer free signals; works at
160; meaningful motion. Two shared pieces carry the round:

- **The lens** (`api.peek`): a compact popover on any mark with now, the tool
  in use, next, the last/current/next step, sub-agents (collapsible), the ask
  with inline answers, and "Open thread". It is what "click a thing and see
  what it is working on without reading the thread" means in practice.
- **Runtime pulses** (`a.pulses`, `a.lastTool`, `field.age`): tool calls the
  provider stream already reports. Round two uses them for "what is it doing"
  and "how fresh is it" so the model is not asked for more. `plan`, ask
  options, names, and domains stay optional enrichments.

| # | Mockup | Idea |
| --- | --- | --- |
| 11 | Settling field | Free canvas; fresh floats, old sinks; needs-you on the surface with a one-click answer; drag to pin; satellites for sub-agents. |
| 12 | Constellations | Orchestration groups as constellations; brightness is recency; needs-you stars pulse with a button. |
| 13 | Orbits | Roots are suns, sub-agents orbit; motion is activity; click a sun to zoom into its system. |
| 14 | Vital signs | One sparkline per agent from tool pulses; flatlines need you; nested traces for sub-agents. |
| 15 | River | Phases as a flow; particles drift; needs-you caught on rocks; done settles in the delta. |
| 16 | Radar | Needs-you at the center; rings outward by state; sectors by domain; a sweep shows recent calls. |
| 17 | Breathing grid | A grid of cells breathing at activity rate; fresh rows on top; inset sub-cells. |

`probe.js` drives a mockup over the DevTools protocol so interactions can be
verified headlessly: `node probe.js "mockups/11-settling-field.html?scale=small"
"tap('.mark.working'); await wait(300); return document.querySelector('.lens')?.innerText"
out/probe.png`. Scripts get `tap`, `drag`, and `wait` helpers.

## Synthesis: Atlas (mockup 18)

After round two the direction became spatial: the user remembers where things
are, the way a physical book differs from an e-book. Atlas combines the parts
that landed: the settling field's free canvas, the constellations' blinking
needs-you dots and hub-and-spoke groups, the orbits' zoom-in, and mission
control's quiet needs-you panel.

- **Organic checkpoint contours.** Each territory has a permanent domain seed,
  low-frequency outline noise, and broad cosine lobes shaped by family sizes.
  Lobe amplitude is capped at 30% of base radius; only unfinished threads
  contribute to lobe size, so completed volume cannot enlarge the outline. Noise and lobes taper smoothly to zero inside 30% of the radius, reaching full
  strength only at the rim. Phase boundaries remain nested, with calm circular
  centers and organic outer contours. From the center:
  Needs you, Getting started (no checkpoint), Discussing, Investigating, Planning,
  Orchestrating, Implementing, Validating, Deploying and Finished. Needs you
  overrides phase. Populated active phases get at least 52 units (32 for Needs you), growing with
  `24 × √count` up to 96. Finished stays an 18-unit crust regardless of count.
  Empty phases are 6-unit gutters (10 for an empty center), and trailing empty
  phases disappear.
  The rim adds 20 units of padding. Pinned threads retain a home in their phase.
  Fit includes the organic rim, pins, external domain captions and the focused
  domain’s phase ruler. The ruler sits to the right with ticks at each boundary
  and spaced phase names; node labels turn away from it.
- **Abstract family symbols and focused lineage.** Every parent gets a stable,
  procedurally generated form inherited by its children. Uneven scoops, broad
  swells, varied proportions and shear create smooth, asymmetric silhouettes
  without repeating petals or a finite icon palette. Parents use a larger mark with an open center;
  children use smaller solid silhouettes and standalone threads remain circles.
  There are no double outlines. A child that delegates gets its own branch
  symbol plus a small copy of its parent's symbol. Assignments persist across
  reloads and workspace tabs independently of status and position. There are no
  child counts on the map. The overview uses symbols alone, without links or
  arrows. Directional parent-to-child links appear in a focused domain or family
  and hide again when zoomed out; hover and keyboard focus emphasize a family. Selecting any member
  highlights the family, reveals its finished members and opens an indented tree
  with full names and status in the side panel. Selecting a tree row opens that
  agent's details. Color continues to mean attention/activity, not family.
- **Lineage initializes; users own angles.** Families initially divide the
  available angle in proportion to membership, sorted by a stable hash. Each
  node saves its angle, including through phase changes, new arrivals and reloads.
  Parent-child links track their actual positions even after manual placement.
  No force simulation runs on thread nodes.
- **Meaningful motion.** Phase and population changes animate node positions.
  Reduced-motion preferences disable transitions. Geometry lives in
  `shared/atlas-layout.js`. Domain centers get a deterministic initial force
  layout once. Clear saved centers stay fixed; overlapping hull bounds are
  separated with a small gutter after growth, loading an older layout or a hull
  drop. The corrected centers persist, so zooming never rearranges them. The
  dropped hull stays where the user put it and neighbors yield. Free pins keep
  their canvas positions during collision repair.
- **Free placement with a legible exception.** Drag around a contour to save an
  angle and depth within the current phase. Drag outside the territory to pin
  at that canvas position; a diamond, faint tether and phase-home ghost identify
  the exception. Drag back inside to release it. **Pin freely** also preserves
  arbitrary positions inside the hull through phase changes; **Return to phase layout** releases
  it explicitly. Pins stay fixed when their territory moves. Existing saved
  territory positions and older pin coordinates migrate.
- **Sparse labels and real age fading.** Overview captions sit outside territories,
  with up to three needs-you titles stacked below each domain name and an overflow
  count. Focused domains label needs-you nodes, the selection and up to three
  prominent unfinished threads. Hover and keyboard focus reveal other titles
  and details. Finished nodes fade from their completion timestamp to about 24%
  opacity at seven days, with a 22% floor. Heartbeats do not rejuvenate finished
  work. After 48 hours, unpinned finished threads fold into a quiet density band
  at the rim. Click a density segment or use **Show older finished** in the
  focused domain to reveal them; **Fold older finished** restores aggregation.
  Selection, location and explicit pins keep individual threads accessible.
  Finished ancestors remain visible while any descendant is visible, so family
  links retain their parent endpoint.
  Density counts preserve every folded thread; age never closes one. In the
  overview, needs-you marks have a 6.5-pixel radius, ordinary marks 3.5 pixels,
  and recent finished marks 2.3 pixels. Family symbols use 8.5-pixel parent radii
  and 5-pixel child radii at overview scale, constrained by nearby marks.
  Needs-you family parents have a 9.5-pixel radius.
  Domains without asks have quieter hulls
  and contours. Message volume no longer makes gray overview dots larger than asks.
- **Encodings.** Warm means needs you, blue means running, gray means idle or
  finished. Size reflects user messages, constrained by nearby marks. Whole
  hulls remain draggable. Preferences are saved per dataset, scenario and workspace.
- **Needs-you panel.** Right or bottom, plain: kind, who, from whom, the
  question, a one-click first option, More… for the lens, and "show on map",
  which flies to the node (or to the hub that contains it) and rings it.
- **Domain scenarios.** The Domains selector in the top strip switches the
  fixture between 7 clean domains, 3 coarse, 14 fine, a messy mix (synonyms
  such as "frontend" and "UI", agents in two domains, one-off labels, missing
  labels), and no domain signal at all. The map copes visibly: synonyms merge
  with "also …" shown, two-domain agents retain a short dashed tie to the other
  cluster while their position follows phase, one-off labels gather into "Other", missing
  labels form "No domain reported", and with no signal the clusters come from
  write scopes, which cost the model nothing.

## Round three: orchestration (mockups 19–28)

Rounds one and two asked what agents exist and what they are doing. Round three
asks how they work together when anyone can spin up agents that spin up agents:
who messages whom and why, how they delegate, hand off, negotiate scope and
review, what each one is (identity beyond the title), what it works on (a
surface, not only a phase), and what needs you, including asks that climbed a
chain of agents before reaching you.

`shared/orchestration.js` wraps `AgentField.create` and adds the agent-coord
message log to every field: sender, recipient, classification (creation,
request, FYI, reply, closure, failure report), reply obligation, `in_reply_to`,
exchange thread, delivery and acknowledgement, and whether the message woke a
turn. Everything there is recorded today. On top, it models a few proposed
fields, each one short enough for the agent to report in the same `send` or
`checkpoint` call:

| Proposed field | Where it is reported | What it unlocks |
| --- | --- | --- |
| `intent` | on `send` (assign, question, answer, handoff, finding, review, blocker, coordinate, status) | edge labels, filtering chatter from blockers, "why are they talking" |
| `surface` | on `checkpoint` (page, service, screen, doc, pipeline, build, question + label) | "on what" in product nouns; the surface map |
| `contract` | on `thread create` (deliverable, acceptance checks, due) | contracts board, overdue work, accept / send back |
| `deliverable` | on `checkpoint` (kind, label, none/draft/ready/delivered) | handoff readiness without reading the thread |
| `escalation` | `ask.via`, appended as an ask is relayed | "Rook → Procurement → you" and who answers it |
| `health` | derived from age, waits and due time; optionally self-reported | slipping or stuck without a stat line |

Trees go three or four levels deep at `medium` and `large`. Play and Step
generate creations, handoffs on finish, blockers escalated to the parent,
status and findings, peer coordination, and replies. The thread drawer now
lists an agent's messages and its contract.

| # | Mockup | Spectrum | Idea |
| --- | --- | --- | --- |
| 19 | Conversation cards | conservative | Today's cards grouped by team, plus a "talking with" line and an owes/awaits line. A toggle shows the view with today's signals only or with the proposed ones. |
| 20 | Message ledger | conservative | A tree table with Owes, Awaiting and Last-message columns; expand a row for its threaded exchanges and a composer. Includes a "Today only" switch. |
| 21 | Live wires | moderate | Org chart under a "You" band of asks. Dots travel edges as messages are sent, open requests are dashed with their wait, and relay paths climb to your tile. |
| 22 | Sequence diagram | moderate | A UML sequence diagram per team: lifelines, intent-labeled arrows, pulsing open requests, phase activation bars, and asks answered on your lifeline. |
| 23 | Team channels | moderate | Group chat with one channel per team and cross-team directs. Asks are pinned, replies threaded, and you can post or @mention an agent. |
| 24 | Surface map | moderate | Surfaces (pages, services, screens, docs, builds) with agents attached. Traffic arcs connect surfaces and overlaps glow; a toggle switches to today's scope directories. |
| 25 | Contracts | radical | Each delegation is a contract moving from Assigned to In progress, Handed off, and Accepted. Acceptance checks tick, and handoffs get Accept / Send back. |
| 26 | Comms matrix | radical | Who talks to whom at 180 agents: a matrix ordered by team with You first, a chord view by team, and small multiples of the last hour. |
| 27 | Critical path | radical | You as a node in the dependency graph. It shows each outcome's longest chain, which answer unblocks the most work, and reflows as you answer. |
| 28 | Office floor | radical | Teams are rooms with agents at desks. Notes fly between desks, and agents that need you queue at your desk; zoom into a room for its log. |

Positions the builders converged on independently:

- **The title is the assignment, not the identity.** It is long and it changes
  with every task. Every mockup leads with a stable `name` and falls back to
  role and surface, a runtime handle, or the title in a placeholder style.
  The cheapest real fix is to have the app assign a short name at creation;
  no model cooperation needed.
- **"On what" is the surface; phase drops to a stepper or the drawer.** Write
  scope is a usable fallback today, but read-only agents (researchers,
  reviewers, subagents) declare none, and file paths do not say which page or
  service is affected.
- **Most of orchestration is drawable today.** Creations, reply obligations,
  `in_reply_to` and `next_actor=user` already give the graph, open requests,
  and who waits on whom. `intent` and `escalation` are the two proposed fields
  that most change what the views can say.

### Real records and message dots (27, 29)

`export-threads.py` now also exports agent-coord messages from the last 14 days
(`--message-days N`) that touch an exported thread, plus names for
counterparts outside the snapshot. With `?scale=real`, `orchestration.js` builds
`field.messages` from those rows. It keeps the database's classification,
reply obligation, `in_reply_to`, closure, and wake. It guesses `intent` from the
body and flags it as derived, and it invents nothing on Play. Critical path (27,
now a vertical tree) and Orchestration atlas (29, Atlas's vibe with families
as territories) open on the real snapshot when it exists and show messages as
dots floating between agents. A replay scrubber sweeps through the message log.

```
python3 docs/agent-display-mockups/export-threads.py
open "docs/agent-display-mockups/mockups/29-orchestration-atlas.html?scale=real"
```

## Who built what

Mockups 01, 11, and 18, the shared fixture, shell, tokens, lens, probe, index,
and this README were written in the orchestrating thread. Mockups 02–10 were each
built by a persistent Ribbon Field app agent created in orchestration mode and
named "Mockup NN · <title>" (for example "Mockup 06 · Swimlane timeline").
The same agents built round two: 04 built 12, 09 built 13, 06 built 14, 05
built 15, 10 built 16, and 02 built 17. Those conversations keep their context:
to iterate on one mockup, message its agent directly. `BRIEF.md` is the
contract they followed, including the per-mockup specification.

Round three (19–28) was built the same way by ten new app agents named
"Mockup NN <title>" (for example "Mockup 25 Contracts"), working from the
"Round three" section of `BRIEF.md` and `shared/orchestration.js`.

## Files

- `shared/tokens.css`: design tokens mirroring the shipped `styles.css` and
  `theme.css`, plus shared components (chips, dots, phase stepper, ask box,
  drawer).
- `shared/fixture.js`: `AgentField.create({scale})` builds the field; `tick()`,
  `answer()`, `spawn()`, and `subscribe()` drive it. Includes the provider logo
  marks already used by the model picker.
- `shared/orchestration.js`: the agent-to-agent message log and the proposed
  orchestration signals for round three; load it between `fixture.js` and
  `mockup.js`.
- `shared/mockup.js`: `Mockup.mount({...render})` builds the top strip, faux app
  chrome, transport, signals popover, and thread drawer; `Mockup.askBox()`
  renders an answerable ask.
- `mockups/NN-*.html`: one page per mockup; each only adds layout and its own
  rendering.
