# Agent dispatch experiment

Started October 8, 2026. The user wants one dispatcher to route requests to
specialists that retain context, with direct user conversations and a dedicated
deployment agent when needed. This journal records the operating agreement,
observed results, friction, and possible improvements. Tasks remain in Beads;
this is not a second task queue.

Dispatcher thread: `01a11da7-0760-75d3-a2a2-1c6dd2f55275`.
Initial setup: `agent-coord-jcb`.

## October 8 review of app-agent findings

Consolidation: `agent-coord-74l`. Read all five agent findings files and checked
the latest release's saved `build/macos/combined-qd9/final-audit.json`. This
review supersedes older operating assumptions below; historical entries remain
evidence of what was known at the time.

**Assessment:** The experiment supports persistent app agents as a useful daily
workflow. The user can close completed conversations and leave context discovery
and later routing to the dispatcher, while still talking directly to each agent.
The strongest result is continuity across real follow-ups and releases. We have
not measured a time or cost advantage against fresh agents, broader routing
accuracy, or reliability across long periods of stale context.

**Observed successes:** The same UI agent handled creation cards, Orchestrating,
and completed-turn Work disclosures; the conversation agent investigated delivery
and implemented successive lifecycle refinements. The dedicated release agent
validated combined artifacts and resumed under the same identity after two app
restarts. Its latest audit records all 172 saved conversations preserved and
the requested UI/reopening changes installed and verified. These are completed
assignments, beyond the initial hello-world exercise. Sources: [UI findings](agent-dispatch-findings/01a11d25-8d6f-7d30-b2a4-8b819bc38817.md)
and [release findings](agent-dispatch-findings/01a11dfa-2d86-7d53-afa3-c99fe6e72b03.md).

**Avoidable overhead:** Assigning a shared journal produced unrelated peer work;
separate agent files now let the dispatcher consolidate without those requests.
The dispatcher also delayed a known-recipient handoff by inspecting all
contributors and repeating status/settings checks. Release readiness belongs
with the release specialist. Luna received the complete request inline before
its first tool call, so three help commands and two inbox fetches were avoidable.
The delivered-request hook wording has since been clarified; no repeated model
trial has established how much this reduces calls. Required acknowledgement and
checkpoint bookkeeping still has a fixed cost, particularly visible on trivial
tasks. Sources: [dispatcher findings](agent-dispatch-findings/01a11da7-0760-75d3-a2a2-1c6dd2f55275.md)
and [conversation findings](agent-dispatch-findings/01a11dab-055a-7770-85b1-72190772fb4f.md).

**Context and coordination limits:** Reusing a conversation does not make its
code assumptions current; the notification agent explicitly rechecked changed
source/runtime state. Shared plugin manifests and cache refreshes also needed
coordination even where product edits were disjoint. Per-agent files solve the
journal collision, not all shared-workspace contention. Source: [notification
findings](agent-dispatch-findings/01a11d8b-936e-7170-bc88-5a6e96901ddc.md).

**Instruction opportunity, still a proposal:** App-agent versus provider-native
subagent terminology is already encoded and installed, but the audit found it
too late in the skill and incompletely advertised by skill/CLI descriptions.
A small chooser near the top of the guidance and a concise dispatcher workflow
are a better next experiment than a new orchestrator subsystem or replacement
overview. Keep app conversations, provider-native subagents, and managed terminal
delegates distinct; retain direct user access and explicit release ownership.
Evaluate routing mistakes and time to useful work on ordinary follow-ups before
claiming that a larger orchestration product is needed.

**Evidence boundaries:** Latest release checks used simulated provider adapters
for packaged interaction; earlier live trials covered real Codex/Claude reuse
and restart. Physical iPhone delivery and the known full-native-smoke focus issue
remain unverified. Test counts demonstrate checked behavior, not agent efficiency
or a general success rate. No product or skill changes were made by this review.

## Operating agreement

- Route directly to a known suitable app agent. When the recipient is unclear,
  use bounded app-thread search across open and closed conversations, then
  inspect a shortlist. Match project, subject, original request, checkpoint,
  and artifacts; use titles as clues rather than proof. Avoid a global inventory
  scan for every request.
- Reuse the agent with the strongest relevant context, including an idle agent
  whose earlier task is complete. A follow-up about the same artifact normally
  belongs to that agent. Inspect routing evidence when the match is uncertain;
  the specialist must refresh current source assumptions before implementation.
- If no suitable agent exists, create an independent app conversation through
  `thread create` through the installed app controls.
  Reuse its returned session identity and conversation link. The existing
  `delegate` command remains available for scoped terminal work, with a ready
  Bead and explicit scopes. Release scopes after each completed assignment.
- Preserve the user's exact request in the handoff and distinguish questions,
  proposals, implementation, and deployment. A feasibility question calls for
  an answer; it does not authorize implementing the feature. The landing-page
  and webhook requests in the opening conversation were examples, not tasks.
- Send actionable requests with an explicit reply choice. Let app specialists
  answer the user in their own conversation and continue there. Use
  `--no-reply-required` unless the dispatcher actually needs a coordination
  answer; do not require every user-facing answer to return through the hub.
  Provide a stable thread link when routing. A send receipt establishes queueing,
  not confirmed execution or completion.
- The user explicitly authorized YOLO permissions for participating agents.
  New app agents and editing terminal workers use `--yolo`. Reuse known settings;
  inspect or change them when needed, rather than as a routine routing step.
  Tool receipts establish configuration. Omit model/effort/permission confirmation
  boilerplate from handoffs. Permissions do not expand the requested task's scope.
- Choose a supported model and effort for the work. Use an economical capable
  model with low or medium effort for bounded edits and routine questions; use
  stronger reasoning for ambiguous architecture, difficult bugs, or deployment
  coordination. Preserve existing settings unless there is a concrete reason to
  change them; receipts record changes. Avoid maximum effort by habit, and
  preserve valuable context when choosing whether to reuse.
- Establish one deployment specialist when the first requested change needs
  deployment. Reuse that agent for subsequent releases, coordinate shared app
  restarts, and distinguish validated changes from installed changes. Existing
  deployment experience is a candidate source of context, not an assignment.
- Involve the dispatcher for routing, conflicting scopes, dependencies,
  deployment handoffs, or material blockers. Keep other conversations direct.
  Checkpoints and activity do not move threads. New actionable routing to a
  closed app agent automatically returns it to Now using the same send interface;
  informational/closure messages do not reopen it. Execution guards remain
  separate, and other placement/snooze choices are preserved. Keep waiting
  event driven where supported.
- Each agent records useful findings in its own stable-session file during
  normal work, reserving only that file. The dispatcher alone consolidates into
  this journal during reviews; no journal-only wake-ups or peer transcription.
  See [the findings convention](agent-dispatch-findings/README.md). Suggestions
  remain proposals until worth acting on; avoid building infrastructure before
  the experiment demonstrates a need.

## Initial context map

Candidates discovered from the global inventory on October 8. This is a small
index, not a fixed roster or assignment; recheck live state before dispatch.

| Subject | Existing thread | Useful context |
| --- | --- | --- |
| Mobile overview | `01a11d93-04dc-7540-9f70-6e0db0622579` | Simplify mobile overview controls; implementing at discovery. |
| Ribbon Field launch | `01a11d9b-24df-7912-88eb-7ccb80e4322b` | Launch brain dump, including landing-page clarity; discussion only so far. |
| Agent messaging | `01a11d25-8d6f-7d30-b2a4-8b819bc38817` | Message visibility, cards, and installed fixes. |
| Message delivery semantics | `01a11d74-c306-7952-ae0f-ac807f98e81f` | Explicit reply flags, correlated replies, and idle wake-up. |
| Stoic event subscriptions | `01a11d92-19b0-7352-8eab-e812a232407a` | Durable event subscription and local listener proposal; potentially relevant to a future webhook question. |
| Notifications and deployment | `01a11d8b-936e-7170-bc88-5a6e96901ddc` | iPhone notification fix and completed native deployment; deployment specialist not yet designated. |

## Evidence and friction

### October 8 mobile table implementation

- **Route:** Dispatcher message 3796 reused mobile specialist
  `01a11c38-17f3-7230-9674-e11862c0c128` for `agent-coord-0s4` with confirmed
  `gpt-6.1-sol`, medium, YOLO settings and no reply obligation. The forwarded
  user request authorized implementation and explicitly excluded deployment.
- **Result:** Mobile Markdown table columns now have a readable minimum width
  and ordinary word wrapping, using the existing horizontal scroll container.
  No renderer or dependency change was needed. Isolated Chromium previews at
  320/390/430 CSS pixels measured a 518-pixel wide table with 128-pixel or wider
  columns; horizontal scrolling remained inside the table and vertical chat
  scrolling worked. Narrow two-column tables fit, prose stayed within the chat,
  and desktop table sizing remained unchanged at 1280 pixels.
- **Validation and limits:** All 617 Python tests passed (one skipped), all 321
  JavaScript tests passed, and required plugin/skill validators passed. Both
  plugin caches were refreshed and verified before isolated browser validation.
  Evidence: `/private/tmp/mobile-tables-layout-results.json` and
  `/private/tmp/mobile-tables-preview.png`. Ribbon Field was not installed or
  restarted, and no deployment job was created. iPhone Safari was not tested
  directly. No routing or scope conflict occurred.

### October 8 setup

- **Observed:** Global `thread list` returned 54 open threads across workspaces.
  Several have specific, recent checkpoints suitable for context-based routing.
  `list --relevant` separately exposed the active mobile-overview write scope.
  A narrow journal scope was acquired without conflict.
- **Documented, not yet exercised by this experiment:** Actionable messages can
  wake idle app conversations and managed workers. Existing app conversations
  support direct user follow-ups. Initial setup sent no specialist task and
  launched no worker, so routing and wake-up are not yet end-to-end validated.
- **Direct-conversation gap:** The installed coordination skill describes
  `delegate` workers as persistent terminal sessions and their monitor as
  read-only. Their results go to the parent inbox. New delegates therefore do
  not automatically provide the same direct chat experience as existing app
  conversations. Prefer relevant app conversations; relay only when the worker
  surface requires it. A future app-backed delegation option may be useful if
  this repeatedly interrupts the experiment.
- **Dispatch overhead:** `delegate --help` confirms that even a new research
  worker requires a Bead and explicit scopes. Use a minimal research assignment
  and a narrow report scope when needed; do not turn a question into code work.
  The documented validation lease rejects `--yolo`, so it cannot be used as a
  universal workaround for research requests with the user's permissions choice.
- **Permissions gap:** New launches expose `--yolo`. Existing browser threads
  expose a permissions setting in the app; the inspected thread CLI offers no
  equivalent setter. Check the supported app control when actually reusing a
  thread. No existing thread's permissions were changed during setup.
- **Inventory size:** Raw global output was truncated during inspection.
  Parsing the CLI's complete JSON locally and printing only relevant fields
  produced a usable inventory. Use that pattern for future discovery.
- **Deployment context:** The existing Beads memory records an October 8 app
  restart loop caused by respawning one-shot launch jobs. Supply that evidence
  to the eventual deployment specialist: explicit `RunAtLoad=true`,
  `KeepAlive=false`, a durable completion guard, and verification against reruns.

Initial setup is documentation only. No product code, deployment, or example
request was executed. The next real user request will provide the first routing
trial; no background monitoring process was installed.

### October 8 app agent implementation

- **Decision:** Use agent as the general term, specialist as a role, and
  dispatcher for routing. Reserve subagent for provider-native agents. Preserve
  terminal `delegate`; add app-owned creation, settings, model discovery, and
  durable request receipts. Implementation: `agent-coord-6j8`, thread
  `01a11dab-055a-7770-85b1-72190772fb4f`.
- **Verified correction:** Managed PTY workers stop after `delegation finish`
  records a completed or failed result. They remain alive between turns only
  while the delegation is unfinished. App conversations are the persistent
  identity/context path for this experiment.
- **Closed-thread routing gap:** Close preserves identity, history, and settings.
  Inbox send can queue to a closed recipient, but automatic wake excludes it.
  Direct app chat rejects closed threads. The approved controls do not include
  agent-driven reopen; use the app's explicit Reopen. Stopped or failed threads
  may also need direct user input to resume automatic wake. Explicit
  reopen-and-dispatch for a fresh user request is a possible improvement, not
  an approved implementation change.
- **Validation:** The full Python suite passed (617 tests, one optional Web Push
  encryption test skipped), along with compilation, plugin layout, both skill
  checks, and Claude strict validation. Both installed plugin caches were
  refreshed through the documented helpers and verified against 85 source files.
- **Live evidence:** Fresh isolated app runtimes created Codex (`gpt-6-luna`,
  low effort) and Claude (`sonnet`, low effort) agents with YOLO enabled. Each
  answered a routed question directly, created a disposable file, accepted a
  direct user revision, then recalled the revised context after runtime restart
  when the dispatcher addressed the same session ID. The conversation API
  exposed the direct answers. Closing preserved identity and prevented a later
  inbox message from waking either agent. Four turns passed for each provider.
  Evidence: `/private/tmp/agent-coord-6j8-live/results.json` and
  `/private/tmp/agent-coord-6j8-live.log`.
- **Deployment boundary:** These were bounded integration fixtures, not a live
  product assignment. The running native Ribbon Field app was not rebuilt,
  replaced, or restarted; its backend needs the new runtime before these CLI
  requests can be serviced in the normal workspace.

### October 8 installed-app dogfood

- **Deployed:** `agent-coord-r79` installed and reopened Ribbon Field, verified
  the bundled snapshot and app-control worker, and exited zero after exactly
  one launchd run. The focused native smoke passed; full window-focus validation
  was blocked by the locked Mac desktop. The earlier deployment boundary above
  is now resolved.
- **New specialist:** `thread create` produced the ordinary app conversation
  `01a11dfa-2d86-7d53-afa3-c99fe6e72b03`, **Ribbon Field release specialist**,
  using Codex `gpt-6.1-sol`, medium effort, and confirmed YOLO. Request key:
  `dogfood-b5x-release-specialist`. Its stable conversation link is
  `agentcoord://thread/01a11dfa-2d86-7d53-afa3-c99fe6e72b03?database=28e472f3811ccb833b89fb70`.
- **Real assignment:** Initial message 3792 woke the specialist, which created
  `build/macos/app-agents-r79/dogfood/verify_release.py` and answered directly
  in its conversation. Five release checks passed, including signatures,
  70 backend hashes per bundle, the completed control request, and one-shot
  installer state. No additional deployment or product-code edit was needed.
- **Direct revision:** The coordinating agent used the live app's normal
  browser composer to request `--json`, explicitly labeling this as an
  authorized dogfood instruction. The specialist revised its existing script
  and answered there. Success returned exit 0; a missing-app check returned 1.
  The JSON retains the locked-desktop limitation.
- **Later routing:** After the specialist became idle, dispatcher message 3793
  targeted the same session with no reply obligation. It woke automatically,
  recalled `r79-live-dogfood` and the `passed`, `failed`, and `limitations` fields
  from its conversation context, and answered directly without rereading the
  artifact. All three turns completed under the same identity and settings;
  the conversation remains open for user follow-ups and future release work.
- **Observed friction:** A specialist cannot attach a Bead already owned by
  the coordinator to its work declaration; a disjoint scope without that
  attachment worked. Saved checkpoint artifact links also reject the native
  conversation URL returned by the CLI, while chat Markdown accepts it.
  Tracked the latter as nonblocking `agent-coord-gq3`; no product change made.
- **Evidence:** `build/macos/app-agents-r79/dogfood-evidence.json` records
  identity, settings, turn IDs, browser observations, and audit results.
  `agent-coord-b5x` covers this live exercise. This installed-app trial used
  Codex; the earlier isolated trials additionally covered Claude and restart.

### October 8 closed-specialist investigation

The detailed explicit-reopen design below is retained as investigation history.
The user subsequently approved the smaller ordinary-send design; see the
implementation entry at the end of this document.

**Later user clarification (supersedes the send-reopen rationale below):** In
thread `01a11dab-055a-7770-85b1-72190772fb4f`, the user clarified that Closed is
primarily UI organization, not a restriction on another agent messaging a
specialist, and agreed closed threads should be searchable. The revised
proposal keeps the existing send interface and ordinary `action_required` wake
behavior for open and closed app threads, restoring the provider behind the
scenes independently of UI placement. Informational and closure messages do
not wake; explicit Stop, pause, and failure guards remain separate. Proposed
discovery adds bounded, paginated text search across open and closed threads
with compact relevance and last-work summaries, followed by shortlist inspection
and refreshed code assumptions. Search flags, ranking, UI placement, and lifecycle
changes remain proposals; no product implementation was authorized in this
discussion. Closed alone should neither cancel nor supersede older queued
messages; the earlier proposed backlog supersession at a closed-thread new-task
boundary is withdrawn. Explicit cancellation and supersession remain distinct
from UI organization, as does the separately tracked suppressed-unread bug
`agent-coord-stj`. Coordination messages 3797 and 3798 requested these proposal
clarifications.

- **Route:** Dispatcher message 3795 assigned investigation Bead
  `agent-coord-w87` to existing specialist
  `01a11dab-055a-7770-85b1-72190772fb4f`. Settings were confirmed as
  `gpt-6-astra`, xhigh, YOLO. The request is investigation and proposal only;
  no product implementation, deployment, or personal-thread reopening occurred.
- **Current behavior verified:** `CoordinationStore.send_message` accepts
  closed recipients; `BrowserInboxWake.process_once` and `_eligible` exclude
  archived conversations. `BrowserSessions.close_work_thread` archives the
  provider, releases work, and retains identity/history/settings.
  `reopen_work_thread` reuses provider unarchive and moves Closed to Now;
  `read` resumes the same identity. An ordinary reopening does not clear an
  inbox Stop/failure pause or resume paused user drafts. `ThreadStore.list`
  separates open from closed: dispatch discovery must combine `thread list`
  with `thread list --archived`, then inspect candidates by identity.
- **Why message classification is insufficient:** `reply_message` creates an
  `action_required` message too, as do scope coordination and other peer work.
  Newness, `reply_required=false`, or lack of `in_reply_to` cannot establish
  that a sender is forwarding a fresh user task. Automatically reopening every
  actionable recipient could undo a deliberate Close for a late reply.
- **Fixture evidence:** Both Codex and Claude adapters accepted messages while
  closed but made no wake attempt. After explicit reopen, both claimed an old
  pre-close message together with the new message. Separate fixtures confirmed
  that Stop and paused user drafts still prevent automatic wake after reopen.
  The 20 inbox-wake and eight work-thread-close tests passed. These used
  disposable databases and fake providers, not live personal conversations.
- **Additional friction:** Closure-suppressed messages are excluded from
  hook delivery but still appear in `inbox --unread`; its message projection
  omits suppression metadata. A new-task boundary must exclude superseded work
  from both automatic hooks and the unread command, while keeping history.
  Filed the existing unread-filter defect as `agent-coord-stj`; not implemented.

**Recommendation (proposed, not implemented):** add explicit per-message
reopen intent, used automatically by the dispatcher for fresh user-requested
work. Keep ordinary send/reply semantics unchanged. Suggested CLI:

```bash
agent-coord send --session <specialist-id> \
  --classification action_required --no-reply-required \
  --reopen --request-id <stable-user-task-id> --wait 30 \
  'Make the landing-page hero blue.'
```

This is one durable app-owned dispatch request, not separate reopen and send
commands. Extend `AppControl` with a dispatch operation carrying the exact
message, sender, target, reply choice, stable request key, close/stop generation,
and a snapshot of the old inbox boundary. Reuse `AppControlWorker`, provider
adapters, ordinary inbox delivery, wake claims, and stable navigation links.
Require an app-backed target and actionable new work; informational messages,
closure notices, and correlated replies cannot request reopening. The flag
declares sender intent; it cannot prove human authorship. No new agent registry,
scheduler, or per-specialist process manager is needed.

The owning app claims the request, checks lifecycle and ownership, and uses
the existing per-thread lock. A clean closed conversation is unarchived and
resumed with its existing provider ID, model, effort, permissions, and context.
Move it to Now and publish the ordinary changed event; preserve title, group,
repository, and pins. Ordinary messages to already-open Later/snoozed threads
keep their placement. Do not silently reacquire the old editing scope.

Before unarchiving, hold ordinary inbox wake for this in-progress dispatch.
For a request accepted while Closed, retain older pending actionable inbox
messages as superseded history, exclude them from hooks and unread work, and
report their IDs. Use the captured boundary so later independent fresh
requests are not accidentally suppressed. Insert the new message and record
its ID/result in one SQLite transaction, then release it to the existing wake
worker. Never treat provider RPC and SQLite as one atomic transaction.

Preserve the launch-sized lifecycle guardrails:

- Reject an existing queued/closing Close rather than cancel it implicitly.
  Newer Close or Stop invalidates an older queued dispatch. Record a small
  durable per-thread lifecycle generation at accepted close/stop intent,
  including native/UI close and CLI after-turn close; compare it immediately
  before reopen and submission. Do not rely only on a worker-local lock.
- For this first version, do not override Stop, failed/uncertain submission,
  or paused user drafts. Return the precise blocked reason; explicit user
  continuation remains the recovery path. This bounds automatic reopening to
  the requested completed-specialist case. Broader resume-stopped controls
  would be a separate product decision, not an implied effect of a late message.
- When busy, keep current semantics: inbox hooks may deliver agent messages
  within the running turn, but do not start a second turn or inject steering.
  Queued user follow-ups retain priority before an automatic new turn.
- Identical request-key retries return the same message/result; changed
  arguments conflict. Reusing a completed key after another Close must not
  reopen again. Plain send currently lacks this general idempotency guarantee.
- With no eligible app runtime, the request stays durably queued. Start/restart
  of the matching app services it; send need not launch the app. Respect
  another runtime's ownership. Lost responses across provider boundaries stay
  uncertain and are not blindly replayed, reusing existing claim/lease rules.
- Report queue acceptance separately from execution. A control result may
  confirm reopened plus message queued; expose message ID, delivery status,
  wake turn ID, blocked/uncertain reason, and eventual provider turn completion
  through `thread request-status`. A queued receipt is never an answer or a
  proof that the specialist ran.

Codex requires real `thread/unarchive` followed by `thread/resume`. Claude's
archive closes its per-conversation process, unarchive is adapter metadata,
and the next turn recreates the CLI with `--resume` using the same stored ID.
Use the common orchestration path with provider-specific tests; do not change
the existing terminal `delegate` lifecycle.

Affected components are `cli.py` (flags/receipts), `app_control.py` (durable
dispatch), `codex_app_server.py` and `thread_control.py` (owned lifecycle and
newer-close guard), `browser_wake.py` (dispatch barrier and execution status),
`store.py`/`hook.py` (idempotent message association and superseded-work
filtering), and coordination guidance (closed discovery and intent).
`claude_code.py` should primarily need regression coverage, not a new protocol.

Acceptance scenarios should cover both providers: completed specialist closed
then a fresh task resumes the same identity; direct user revision afterwards;
late replies/information remain closed; old backlog is visible only as history;
duplicate requests create one message/turn; Close/Stop races win over older
dispatch; paused drafts and uncertain submissions remain blocked; offline
queueing and restart preserve status without duplicate work; and closed-thread
discovery returns the existing specialist instead of creating a replacement.

### October 8 closed-specialist implementation (agent-coord-6v7)

The user approved treating Closed as UI organization, searchable alongside open
conversations. Implemented ordinary actionable inbox wake for closed app agents;
the `send` interface is unchanged. The common runtime restores Codex with
`thread/unarchive` plus `thread/resume`, and Claude with the existing saved-ID
resume path. Identity, history, model, effort, permissions, and Closed placement
remain intact. Older pending work is retained. Stop/failure/uncertain submission,
pending Close, and queued user-input guards remain in force. Direct user follow-ups
use the existing Reopen/composer flow. Terminal delegation is unchanged.

Added `thread search` with bounded keyword search of title, original request,
latest checkpoint, and artifacts across all placements. Results carry compact
routing context, app links, provider and pause state. Relevance ranks first;
meaningful work time breaks ties, excluding metadata edits and identical
checkpoint refreshes. Cursor pages use the same query and filters; live inventory
changes call for a fresh search. Dispatcher guidance requires checking current
source before trusting old context rather than imposing an arbitrary age cutoff.

The earlier `send --reopen`, new dispatch-control operation, lifecycle generation,
and blanket old-message suppression proposals are superseded. Validation and
deployment status for this implementation are recorded separately below.

Validation completed with 630 Python tests passing (one skip), compile checks,
plugin-layout validation, both skill validators, and both strict Claude manifest
validators. Refreshed Codex to `0.1.0+codex.20261009004539129616`, reinstalled
Claude, and verified both caches against all 86 plugin source files.

Fresh isolated Codex (`gpt-6.1-sol`, medium, YOLO) and Claude (`sonnet`, high,
YOLO) conversations each completed four turns: an inbox question answered
directly, a coding request queued while Closed then serviced after runtime
restart, a direct user revision after Reopen, and a later dispatcher request
back to the same Closed identity. Both recalled their token and revision from
conversation context. Search found the closed context through cursor pages,
and wake preserved Closed placement and saved model/effort/permissions.

Evidence is in `build/validation/closed-specialists-6v7/results.json`, with
the reproducible driver `live.py` and Claude continuation `finish_claude.py`
alongside it. The first Claude discovery assertion assumed the requested title
had persisted; its existing creation path had instead saved an automatic title.
Searching saved task context completed the trial. This separate naming defect
is tracked as `agent-coord-s23`; it does not prevent context discovery or reuse.

The installed CLI search also returned bounded results and conversation URLs
from the real thread database. This increment has **not** been bundled into or
deployed as a new native Ribbon Field app; live wake validation used fresh
isolated runtimes from the verified plugin cache.
