# Agent display mockups

Ten interactive mockups exploring how Ribbon Field could show the user's agents:
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

- **One coordinate system, three zoom levels.** Every agent has one position;
  the camera moves. Level 0 shows all domains as amorphous clusters, placed
  deterministically and remembered per scenario (drag a cluster to move it; it
  stays). Level 1 zooms into a domain: a scatter with labels. Level 2 zooms into
  an orchestrator: its sub-agents bloom out on spokes with labels, grandchildren
  orbit them. Esc or the background steps back one level. Nothing moves on its
  own; working dots breathe in place.
- **Encodings.** Dot size is how many messages the user has exchanged with the
  agent (a runtime signal). Dot color is age: full accent blue for a tool call
  just now, fading toward the paper over a day. A ring is state: warm blinking
  for needs-you, dashed for waiting on something, green for done.
- **Needs-you panel.** Right or bottom, plain: kind, who, from whom, the
  question, a one-click first option, More… for the lens, and "show on map",
  which flies to the node (or to the hub that contains it) and rings it.
- **Domain scenarios.** The Domains selector in the top strip switches the
  fixture between 7 clean domains, 3 coarse, 14 fine, a messy mix (synonyms
  such as "frontend" and "UI", agents in two domains, one-off labels, missing
  labels), and no domain signal at all. The map copes visibly: synonyms merge
  with "also …" shown, two-domain agents sit on the edge facing the other
  cluster with a short dashed tie, one-off labels gather into "Other", missing
  labels form "No domain reported", and with no signal the clusters come from
  write scopes, which cost the model nothing.

## Who built what

Mockups 01, 11, and 18, the shared fixture, shell, tokens, lens, probe, index,
and this README were written in the orchestrating thread. Mockups 02–10 were each
built by a persistent Ribbon Field app agent created in orchestration mode and
named "Mockup NN · <title>" (for example "Mockup 06 · Swimlane timeline").
The same agents built round two: 04 built 12, 09 built 13, 06 built 14, 05
built 15, 10 built 16, and 02 built 17. Those conversations keep their context:
to iterate on one mockup, message its agent directly. `BRIEF.md` is the
contract they followed, including the per-mockup specification.

## Files

- `shared/tokens.css`: design tokens mirroring the shipped `styles.css` and
  `theme.css`, plus shared components (chips, dots, phase stepper, ask box,
  drawer).
- `shared/fixture.js`: `AgentField.create({scale})` builds the field; `tick()`,
  `answer()`, `spawn()`, and `subscribe()` drive it. Includes the provider logo
  marks already used by the model picker.
- `shared/mockup.js`: `Mockup.mount({...render})` builds the top strip, faux app
  chrome, transport, signals popover, and thread drawer; `Mockup.askBox()`
  renders an answerable ask.
- `mockups/NN-*.html`: one page per mockup; each only adds layout and its own
  rendering.
