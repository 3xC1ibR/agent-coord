# Dispatcher reference

The dispatcher is an opt-in entry point for sending work to persistent Ribbon
Field app conversations. It reuses useful context, creates a conversation when
needed, and lets the specialist answer the user directly there. This reference
describes the shipped feature as of October 9, 2026: the dispatch skill, project
UI, Jev routing, and persistent LLM dispatcher work together.

**Dispatch** leaves answers and follow-ups with specialists. **Orchestration**
owns the complete outcome and delivers the integrated result in the initiating
conversation. Both use the same app agents; provider-native subagents and managed
terminal delegates are separate mechanisms. See the
[Dispatch skill](../plugins/agent-coord/skills/dispatch/SKILL.md),
[Orchestrate skill](../plugins/agent-coord/skills/orchestrate/SKILL.md), and
[coordination commands and rules](../plugins/agent-coord/skills/agent-coordination/SKILL.md).

## Quick start and controls

1. Open a project overview and click **Open dispatcher**, separate from
   **New session**. This is the dispatcher entry point; it reuses the existing
   eligible dispatcher for that directory with its saved model, reasoning, and
   permissions. When no dispatcher exists, the control reads **Start dispatcher**
   and creates a new Codex chat with normal permissions. It waits for startup
   discovery, and opening it does not submit a provider prompt.
2. The action opens a separate dispatcher workspace. Its canvas starts with
   conversations routed through that dispatcher, leaving unrelated project
   sessions in the existing overview. Type a request in the bottom composer;
   routed work opens a readable conversation tile when space is available.
   Additional work stays accessible in the activity rail.
3. Select **Ask dispatcher directly** for a question or decision you want the
   dispatcher LLM to handle. **Conversation** and **Open dispatcher chat** expand
   its transcript within the workspace.
4. Return to the overview with the project breadcrumb or dispatcher close control;
   **Open dispatcher** brings the workspace back.
   Disable routing with **Stop using as dispatcher** in the overview thread menu.
   Hiding the dock alone does not disable it.

Existing designated Codex and Claude conversations remain available through
**Open dispatcher**. Asking an agent to follow the dispatch skill is a workflow
instruction; the UI designation separately enables automatic request routing.

The dispatcher workspace leaves the overview's stages, filters, and saved
placement unchanged. Its rail distinguishes **Needs you**, **In progress**, and
**Results** using existing runtime and reply-classification signals. Findings
and completed work can settle into Results without a manual close action;
uncertain replies, failures, and required input remain prominent. Reading a
result clears its unread state through the existing conversation behavior.

Tiles have a readable size limit; overflow stays in the rail. Background work
does not rearrange tiles while a conversation is focused, has an unsent draft,
or contains an open dialog. Explicitly opened results stay on the canvas until
removed. A tile's close control only removes it from this layout: it does not
stop, archive, or change the placement of its session. A new dispatched request
can bring the same destination back. Workspace membership and tile visibility
are saved locally using identifiers only; the page URL restores dispatcher
mode and the selected conversation after reload. Ctrl/Cmd+F opens the existing
overview search without keeping search or filters on the dispatcher page.

On phones, the activity and results list fills the workspace until a conversation
is opened. Opening a thread fills the screen with that conversation's header,
transcript, and composer. Its back button returns to dispatcher results and keeps
both drafts. New background work stays in the list until explicitly opened.
Eligible response cards offer **Dismiss** directly in the list, using the same
acknowledgement as the overview. Dismissing keeps the conversation available and
does not open it, close it, or change its Now/Later placement. Required input
remains actionable; a newer response still receives its own notification.
**Projects** reveals project tabs, and **Chat** opens the dispatcher
transcript. The compact composer keeps attachments and Send visible; **Options**
reveals model, reasoning, permissions, and **Ask dispatcher directly**. A selected
direct mode remains visible when Options is collapsed. These disclosures do not
change the desktop layout.

The dock retains model, reasoning, permissions, attachment, and approval controls.
Direct replies and approval requests can expand the conversation. Overview cards
retain their **Dispatcher** designation. Navigation preserves unsent drafts between
chats and projects; the chosen dispatcher and dock visibility are remembered
locally. Multiple manually designated dispatchers are possible: the overview
prefers its remembered eligible identity, otherwise the most recently active
eligible one in that directory.

## Routing and delivery

1. **Intake:** ordinary composer input gets a stable request ID and durable
   receipt. Slash commands keep their normal behavior. Internal queue deliveries
   bypass intake to prevent recursive routing.
2. **Jev:** a read-only snapshot ranks app conversations in the dispatcher's
   exact working directory, excluding designated dispatchers. Evidence includes
   titles, checkpoints, saved user/assistant prose, artifacts, assignments,
   selected references, and recent handoffs. Closed and running conversations
   remain candidates. Jev chooses an existing conversation, a new one, or the
   dispatcher LLM.
3. **Execution:** the runtime uses the destination's durable queue, rechecks
   delivery guards, reopens an eligible closed destination, and waits for a running
   turn to finish. Fast routing does not steer an active turn. Reused conversations
   retain their provider, settings, history, and identity. Stop, failed/interrupted
   turns, pending closure, and uncertain delivery remain execution guards.
4. **Fallback:** direct requests and images bypass Jev. Ambiguity, insufficient
   confidence/context, unavailable Jev, or a paused/changed recipient can also send
   work to the persistent dispatcher LLM. It receives bounded routing history and
   the current request, follows the dispatch skill, and can search beyond the
   shortlist, inspect further, reuse/create an agent, or answer about dispatch.
   Specialists normally answer the user directly in their own conversations.

Jev uses the shared local configuration in
[Jev setup](../README.md#automatic-reply-classification-with-jev); without usable
credentials, requests fall back to the LLM. Routing is separate from Jev's
classification of completed replies in the Attention UI. The current routing
policy uses `jev-1.13.0`, eight candidates, confidence at least `0.75`, winning
probability at least `0.70`, and a lead of at least `0.20` over the next choice.
Context and recipient checks still apply after these thresholds pass. See
[`RoutingPolicy` and `decide`](../plugins/agent-coord/scripts/agent_coord/dispatch_routing.py).

The composer's model/reasoning/permissions controls configure the dispatcher LLM,
not Jev or existing recipients. Fast-created destinations use separately saved
defaults: initially the designated chat's provider/model/effort with full access
**off**. Later composer changes do not rewrite those defaults. The backend's
dispatch settings endpoint supports explicit defaults; the UI currently has no
destination-defaults editor. LLM-created agents use ordinary creation controls;
full access requires authorization and is not automatically inherited.

## Receipts, retries, and history

| Receipt | Meaning |
| --- | --- |
| Finding a conversation / Creating a conversation | Planning or creation is in progress. |
| Queued | Saved at the destination; not yet accepted by its provider. |
| Sending | Provider submission is in progress. |
| Sent | The destination provider accepted it; task completion is not established. |
| Passed to dispatcher | The dispatcher provider accepted the fallback/direct request. |
| Delivery paused / Delivery needs review | Delivery guards or an unknown submission outcome require inspection. |
| Routing failed / Cancelled / Dismissed | Routing failed, the request was cancelled, or its problem receipt was acknowledged. Dismiss does not resend. |

These labels describe runtime request receipts. An LLM handoff's **Sent to** link
instead records a durable coordination message; provider wake or delivery may
still be pending. Neither path's handoff establishes completion or deployment.

The browser keeps a retry key across failed submission attempts and reloads.
The database enforces uniqueness per dispatcher and request ID: identical retries
return the saved receipt; different input under that ID is rejected. Successful
intake clears the browser key, so deliberate later submission is a new request.
A unique queue association prevents enqueueing the same receipt twice.

Expired planning can retry. Unknown creation or provider-send outcomes are
**not automatically replayed**. Inspect the destination and queue before deciding
whether to cancel, resume, or submit new work; uncertain routed sends cannot
simply be resumed. These are guards against blind retries, not a guarantee of
exactly-once execution across unknown provider outcomes. Startup readiness and
retaining a newly created ID after configuration failure also prevent accidental
duplicate dispatcher creation.

Fast routes appear chronologically in the dispatcher UI without requiring a
dispatcher LLM turn for each request. Its next turn receives bounded catch-up
marked as historical evidence, with instructions not to repeat accepted or queued
deliveries. The catch-up cursor advances only after successful provider submission.
The UI hides internal catch-up text and duplicate fallback input. Full original
request text and receipt transitions remain available through `dispatch-history`;
automatic catch-up is not the complete history.

## Commands for agents

Replace uppercase IDs and the project path with actual values. Reuse a known
suitable agent directly; search when the recipient is unclear:

```bash
agent-coord thread search 'release validation' --app-only --limit 10
agent-coord thread show --session-id AGENT_ID
agent-coord send --session AGENT_ID --classification action_required \
  --no-reply-required 'Review the release plan and answer the user in your conversation.'
agent-coord ui link --thread AGENT_ID
```

When no existing context is useful, create a normal app conversation. Use a unique
request key for new intent and the same key and arguments on retries:

```bash
agent-coord thread create --cwd /absolute/project --client codex \
  --name 'Release review' --request-id release-review-001 --wait 30 \
  'Review the release plan. Investigate only; do not deploy. Answer the user here.'
agent-coord thread request-status --request-id release-review-001 --wait 30
```

Dispatch creation omits `--mode`; `--mode orchestration` records a new agent's
contribution to an outcome delivered by its creator. Creation intent and reporting
are independent: `--reply-required` requests a result via
`agent-coord reply --message-id MESSAGE_ID 'Result'`; it does not change the mode.
Default dispatch uses direct specialist answers and `--no-reply-required`.
That flag removes the reporting obligation, not the actionable assignment.
For authorized deployments, reuse a known release specialist and let it establish
readiness and verify the running result.

Inspect routing without sending work:

```bash
agent-coord thread dispatch-history --session-id DISPATCHER_ID --limit 20
agent-coord thread dispatch-history --session-id DISPATCHER_ID --after EVENT_CURSOR
agent-coord thread dispatch-evaluate --session-id DISPATCHER_ID
```

Follow `next_cursor` with `--after`; history uses an **event** cursor, evaluation a
**request** cursor. The [evaluation guide](dispatch-evaluation.md) covers exact
saved inputs, offline threshold replay, paid `--live` re-scoring, and separate
user/reviewer labels. Replay never delivers work. Use `--source user` labels only
for user-confirmed destinations.

## Source map and limits

| Area | Reference |
| --- | --- |
| Agent behavior and contracts | [Dispatch skill](../plugins/agent-coord/skills/dispatch/SKILL.md), [coordination skill](../plugins/agent-coord/skills/agent-coordination/SKILL.md) |
| Entry, workspace, composer, tiles | [app.js](../plugins/agent-coord/scripts/agent_coord/web/app.js), [dispatcher-workspace.js](../plugins/agent-coord/scripts/agent_coord/web/dispatcher-workspace.js), [dispatcher-workspace.css](../plugins/agent-coord/scripts/agent_coord/web/dispatcher-workspace.css), [dispatch.js](../plugins/agent-coord/scripts/agent_coord/web/dispatch.js), [thread-panes.js](../plugins/agent-coord/scripts/agent_coord/web/thread-panes.js) |
| Inventory and Jev decisions | [dispatch_snapshot.py](../plugins/agent-coord/scripts/agent_coord/dispatch_snapshot.py), [dispatch_routing.py](../plugins/agent-coord/scripts/agent_coord/dispatch_routing.py) |
| Intake, defaults, receipts, catch-up | [dispatch_runtime.py](../plugins/agent-coord/scripts/agent_coord/dispatch_runtime.py) |
| Provider delivery and inbox wake | [browser_queue.py](../plugins/agent-coord/scripts/agent_coord/browser_queue.py), [codex_app_server.py](../plugins/agent-coord/scripts/agent_coord/codex_app_server.py), [browser_wake.py](../plugins/agent-coord/scripts/agent_coord/browser_wake.py) |
| HTTP and CLI interfaces | [ui.py](../plugins/agent-coord/scripts/agent_coord/ui.py), [cli.py](../plugins/agent-coord/scripts/agent_coord/cli.py) |
| Regression coverage | [routing](../tests/test_dispatch_routing.py), [runtime](../tests/test_dispatch_runtime.py), [queue](../tests/test_browser_queue.py), [dispatcher UI](../tests/test_web_dispatch.js), [evaluation](../tests/test_dispatch_evaluation.py) |

Retrieval is lexical and bounded, not a full semantic search of provider history.
Jev matches exact directories; the LLM can search more broadly. Missing or partial
saved history can require fallback. Jev receives bounded conversation excerpts;
evaluation retains exact input locally, including private text, without automatic
expiry. Consult the evaluation guide before exporting it.

The running app must include the feature. Plugin refresh alone does not update a
native app's backend or UI; use the documented
[plugin refresh](../README.md#refresh-installed-plugins-after-changes) and
[native app build/release](../desktop/macos/README.md) workflows as applicable.

This is shipped, opt-in behavior. Default-entry/primary-product positioning remains
a proposal; comprehensive onboarding and automatic routing-policy tuning are not
implemented. Controlled provider fixtures and desktop/mobile viewport checks do
not establish production routing accuracy, time/cost savings, or physical-phone
reliability. Historical evidence and proposals live in the
[dispatch experiment journal](agent-dispatch-experiment.md). Findings collection
stays off unless the user explicitly requests it.
