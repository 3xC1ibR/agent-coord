# Ribbon Field technical reference

[← Product overview](../README.md)

Setup, commands, provider integrations, and development details. Run commands
from the repository root unless a section says otherwise.

Ribbon Field is the app for managing coding-agent conversations and work across
projects. Its CLI, plugins, and coordination internals retain the Agent Coord
name and existing commands.

The [macOS app](../desktop/macos/README.md) can be packaged as a standalone
drag-to-Applications disk image for Apple Silicon or Intel. Recipients do not
need to clone this repository or install Python. See the app guide for build,
validation, Developer ID signing, and notarization commands.

Agent Coord is a local coordination channel for Claude Code and Codex sessions.
It uses one dependency-free Python CLI, SQLite, hooks, and shared skills. It
does not need an MCP server, terminal multiplexer, or long-lived parent process.
Delegated workers run in Agent Coord-owned PTYs; an optional compatibility
watcher can still wake an ordinary agent that runs in Zellij.

The plugin answers these questions:

- What work is open across my workspaces, and what needs my attention?
- How can a dedicated agent help me review, group, rename, or park my threads?
- Which sessions are online, stale, or offline?
- Which sessions are discussing, planning, implementing, validating, or waiting?
- Which sessions are working without scopes, and which file scopes do declared
  sessions own?
- How can a session atomically transfer a complete scope and its validation
  boundary without briefly releasing it to competing work?
- How can a session reserve a stable scope for read-only validation?
- How can one session send a durable message to another session or to the live
  owner of a Beads issue?
- Which messages require action or a reply, and which are history-only receipts?
- How can an Agent Coord-owned worker safely start a turn when a message arrives
  while it is idle?
- How can a session delegate scoped work to a new interactive Codex or
  Claude Code session without Zellij or tmux?
- How can an operator inspect the parent, its children, and each child's current
  status and recent output?

## Design

Agent Coord is a conflict detector and communication substrate. A sole working
session does not need a Beads issue or write scope. When a second session tries
to write, the hook stops the newcomer, sends the unscoped incumbent an
actionable scope request, and requires concurrent writers to declare scopes.

Beads is optional for direct work, delegation, and atomic handoff. Session,
delegation, and handoff IDs provide durable identity; file scopes protect
ownership. Without `--bead`, Agent Coord does not look up or invoke `bd`.
When `begin-work` includes `--bead`, Agent Coord verifies that the issue is
claimed and has the `in_progress` status. With `delegate --bead`, it verifies
that the issue is open, ready, and unclaimed. The CLI does not claim, update,
or close issues; an opted-in worker follows the Beads workflow.

Session and message state is in a WAL-mode SQLite database at:

```text
~/.local/state/agent-coord/state.sqlite3
```

Database resolution is `--db`, then `AGENT_COORD_DB`, then
`${XDG_STATE_HOME:-~/.local/state}/agent-coord/state.sqlite3`. Normal commands
need no database flag. Set `AGENT_COORD_STALE_AFTER_SECONDS` to change the
default 30-minute stale threshold.

### Short agent commands

```bash
# Run once from the installed plugin to install a stable executable:
/path/to/installed/plugin/scripts/agent-coord install-cli
# ~/.local/bin must be on PATH; --bin-dir selects another directory.
agent-coord inbox --unread
agent-coord status
agent-coord begin-work --scope 'src/**'
agent-coord send --session <recipient-id> --reply-required 'Ready for review.'
agent-coord end-work
```

Caller identity resolves from explicit `--session-id` / `--from-session`, then
`AGENT_COORD_SESSION_ID`, then `CODEX_THREAD_ID`. Missing identity is an error;
the CLI never selects a caller from the working directory. Other threads and
message recipients remain explicit. Ribbon Field supplies PATH and database
context to both providers, and Claude receives its own session identity.
Terminal Claude sessions use the SessionStart hook's `CLAUDE_ENV_FILE` exports.
Child launches clear inherited parent identity. Without inherited context,
hooks retain explicit executable, database, and identity fallbacks.

Routine writes (`register`, `set-activity`, `checkpoint`, `thread update`,
`begin-work`, `end-work`, `unregister`, `send`, `ack`) return compact JSON
receipts. Add `--full` to retain the previous complete output; read commands
such as `status` and `thread show` still return complete state.
Checkpoint writes return only `status` and `checkpoint_id` by default.
The stable launcher is refreshed by rerunning `install-cli` from the new
installed plugin. It refuses to replace an unmanaged executable.

The same plugin directory contains Codex and Claude manifests. Both clients use
the same hooks, skills, CLI, and database schema.

Delegation lifecycle state is also in SQLite. The default launch adapter owns a
detached PTY for each child, captures bounded output, and wakes an idle child
when actionable messages arrive. A Zellij adapter remains available for
compatibility. Durable parent, child, result, and failure state does not depend
on pane inspection.

Wake registration is transport-neutral durable state: each session has a
transport name, transport-owned endpoint metadata, enablement and watcher
status, and a shared atomic message-reservation history. The `managed-pty` and
`zellij` adapters own their different delivery safety checks; the message store
does not. Registrations reject unknown transport names instead of accepting a
target that no adapter can service.

Scope declarations use conservative repository-relative paths and globs. Agent
Coord does not claim symbol- or line-range ownership because the write hooks can
reliably enforce paths, but cannot reliably identify every symbol changed by a
patch. Atomic handoff transfers the complete declaration rather than attempting
unsafe glob subtraction.

## Install

Clone or place this repository at `/opt/projects/agent-coord`, or replace that
path in the commands below.

For Codex:

```bash
codex plugin marketplace add /opt/projects/agent-coord
codex plugin add agent-coord@personal
```

For Claude Code:

```bash
claude plugin marketplace add /opt/projects/agent-coord
claude plugin install agent-coord@agent-coord
```

Start a new session after installation so the client loads the lifecycle hooks.
If `claude plugin` is unavailable, update Claude Code or fix `PATH` so it selects
the current installation.

## Use

At session start, the hook registers the session and provides the session ID and
the absolute path to the bundled CLI. A sole session can write immediately. The
hook records its first structured repository write as active work. Run
`end-work` when that solo work is finished so another session does not treat it
as an incumbent.

When another session is working, declare the smallest useful scope. Include a
Beads issue only when the work already has durable task identity:

```bash
<agent-coord-path> begin-work \
  --scope 'src/**' \
  --scope 'tests/test_feature.py' \
  --lease-mode write
```

```bash
bd update <bead-id> --claim
<agent-coord-path> begin-work \
  --bead <bead-id> \
  --scope 'src/**'
```

`begin-work` rejects a declaration when another live session owns the same
non-null Beads issue or an overlapping scope in the same repository. Scope-only
declarations receive the same overlap protection. Repeat `--scope` for each
path. A comma-separated list is rejected before the declaration changes. For
a filename that contains a comma, declare its parent directory.

If a newcomer finds an unscoped incumbent, its structured write is denied and
the incumbent receives one actionable, `reply_required=false` request to
declare a scope. The incumbent is also denied on its next structured write
until it declares a scope. Repeated newcomer attempts do not duplicate the
pending request.

Use `--lease-mode validation` to reserve an exclusive, stable scope without
granting the session permission to use structured write tools. This is distinct
from `--activity validating`: a normal write lease may enter the `validating`
activity and still fix files. A validation lease blocks every overlapping lease
until it is handed off or released.

Common commands:

```bash
agent-coord list --cwd /path/to/repo --relevant
agent-coord status
agent-coord conflicts

agent-coord send \
  --session <peer-session-id> \
  --classification action_required \
  --reply-required \
  --thread-id <thread-id> \
  'Can you release src/api/**?'

agent-coord send \
  --bead <bead-id> \
  --classification informational \
  --no-reply-required \
  'I need to coordinate a shared interface change.'

agent-coord inbox
agent-coord inbox --unread
agent-coord inbox --all
agent-coord inbox --wait
agent-coord inbox --wait --timeout 120
agent-coord reply --message-id <message-id> 'Received, thanks!'
agent-coord ack --message-id <message-id>
agent-coord ack --all-unread
agent-coord end-work
```

Messages stay in SQLite until the recipient reads them. Delivery and explicit
acknowledgement have separate timestamps. Addressing by Beads issue succeeds
only when exactly one live session declares that issue.

Messages are classified as `action_required`, `informational`, or `closure`.
Only undelivered `action_required` messages enter hook context or wake an idle
agent. `reply_required` is separate: an actionable message may require work but
no conversational reply, as with an atomic handoff. Every CLI `send` must choose
`--reply-required` or `--no-reply-required`; an omitted or contradictory choice
is rejected before a message is created. Use `--no-reply-required` for
informational and closure messages. Transport acknowledgement is silent and
never creates another message.

Answer a request with `agent-coord reply --message-id <id> '<response>'`.
The command derives the recipient and exchange from the original message,
records that message ID as `in_reply_to`, and sets `reply_required=false`.
Replies remain `action_required` so they enter hook context and wake the
recipient to process the answer. Only the original recipient can reply; replies
to messages requesting no reply, closed exchanges, or superseded requests are
rejected. A successful reply also acknowledges the original message in the same
transaction; no separate `ack` is needed. Use `ack` for messages handled without
replying. Starting new work or asking a follow-up question uses `send`
with an explicit reply choice.

Continue a conversation with the same `--thread-id`. A thread accepts only the
same pair of sessions, in either direction. Close it with one terminal message:

```bash
agent-coord send \
  --session <peer-session-id> \
  --classification closure \
  --no-reply-required \
  --thread-id <thread-id> \
  'Validation complete; no further coordination action is needed.'
```

Closure is idempotent and history-only. It marks older pending actionable
messages in that thread delivered so they cannot trigger another wake or hook
turn. A later explicit `action_required` message on the same thread reopens it
for a material state change.

The default `inbox` returns undelivered messages. `inbox --unread` is the compact
unacknowledged view, including messages already delivered by a hook; use
`inbox --all` only for complete history. `ack --all-unread` performs silent
transport acknowledgement in one operation.

`inbox --wait` blocks without model-token use until a message arrives,
polling the local SQLite store and refreshing session liveness on each check.
Any unread message already in the inbox returns immediately. A newly received
message is returned and marked delivered using the same semantics as a
normal `inbox` call (respecting `--peek`). Add `--timeout SECONDS` to bound
the wait to a positive number of seconds; without it, `--wait` blocks
indefinitely. `--timeout` requires `--wait` — using it alone is rejected.
`--wait` is incompatible with `--all`, since previously delivered history
would make the blocking call return immediately. On timeout the command
exits with status `5` and prints a JSON error to stderr — a code distinct
from the other documented exit statuses (`2` validation, `3` conflict, `4`
ambiguous bead target).

## Atomic handoff

Use `handoff` when the current owner has a scope declaration, has reached
a named patch or validation boundary, and the recipient must acquire the
complete declaration without a scope-free race:

```bash
agent-coord handoff \
  --from-session <owner-session-id> \
  --to-session <idle-recipient-session-id> \
  --patch-label <patch-name> \
  --validation-boundary '<state already validated>' \
  --validation-responsibility '<checks the recipient owns>' \
  --mode validation
```

The sender, recipient, scope transfer, audit record, and one actionable
`reply_required=false` notification are updated in one SQLite transaction. The
recipient must be registered, online, idle, and in the same repository. The
command rejects partial scopes; optionally repeat every current `--scope` to
assert the expected declaration. A Beads issue is optional; an existing issue
link is carried with the declaration. Use `--target-bead` only for a claimed
`in_progress` issue; the CLI revalidates a changed target immediately before the
transaction. Beads and SQLite remain separate stores, so their updates cannot be
one cross-database transaction.

## Create persistent app agents

An **agent** is an independent Ribbon Field conversation. A **specialist** is
an agent focused on a subject or responsibility; a **dispatcher** routes work.
Provider-native subagents remain a separate concept. App conversations retain
context between assignments and accept direct user follow-ups.

The project overview's opt-in **Start dispatcher** action opens a bottom composer
that routes requests to these conversations. See the
[Dispatcher reference](../docs/dispatch.md) for quick start, UI controls, Jev and LLM
routing, delivery/history semantics, commands, and source locations.

For ongoing routing, use the [Dispatch skill](../plugins/agent-coord/skills/dispatch/SKILL.md):
it reuses relevant app agents, creates new ones when needed, and directs answers
and follow-ups to their own conversations. Use the
[Orchestrate skill](../plugins/agent-coord/skills/orchestrate/SKILL.md) when the
initiating conversation should own the full outcome, coordinate contributions,
and deliver the integrated result. Both support normal discovery in Codex and
Claude, and reuse the same agents for later discoveries, questions, and revisions.
Workflow findings collection is off by default; enable it only with an explicit
request for that dispatch or orchestration conversation.
Normal task tracking, checkpoints, and result reporting remain in use.

All app agents, including user-created and ordinary dispatch conversations, save
a `context_lifecycle` envelope at meaningful task completion or handoff.
It recommends `reuse`, `fresh`, or `either`, explains what follow-up work would
benefit from retained context, and provides confirmed facts, constraints,
tried-and-failed approaches, and open questions for a replacement. Creation's
automatic completion event and explicit requested replies carry an immutable
snapshot matched to that assignment and turn. Direct user work omits the
optional assignment message ID; its report is bound to the current turn.
The recommendation guides the orchestrator; it does not close or reset a
conversation. The chat's right-hand panel shows the recommendation, reuse advice,
and expandable handover. See the [reporting contract](../plugins/agent-coord/skills/agent-coordination/SKILL.md#context-lifecycle-for-app-agents)
and [reuse guidance](../plugins/agent-coord/skills/orchestrate/SKILL.md).
Automatic creation subscriptions can be disabled with `--no-subscribe`; the
completed creation receipt reports `result.subscribe` and
`result.subscription_id`. An updated app backend is required to deliver these
events; refreshing plugins alone does not replace an already running backend.

A registered dispatcher can ask the running app to create an agent:

```bash
agent-coord thread models --client codex --cwd /absolute/workspace --wait 30
agent-coord thread create \
  --cwd /absolute/workspace --client codex --name 'Release specialist' \
  --model <advertised-model> --effort <supported-level> \
  --request-id <unique-request-key> --wait 30 \
  'Explain the release process. Investigate only; do not deploy.'
agent-coord thread request-status --request-id <unique-request-key> --wait 30
```

Use `--client claude` for Claude Code. Model and effort are optional. Add
`--yolo` only with explicit user authorization; defaults use normal app
permissions and do not inherit the dispatcher's permission mode. Hook trust
and provider authentication still apply. Use `-` as the prompt to read stdin.
No Bead or scope is required to create a conversation. Coding assignments follow
the target repository's issue, scope, and validation rules.

Creation runs inside the app's existing provider infrastructure. A completed
receipt includes `thread_id`, `result.session_id`, effective model, effort and
permissions, `result.url`, the initial coordination `message_id`, and
`result.reply_required`. The agent receives the request through its durable inbox
and, by default, answers the user in that conversation. The original request is
saved before the wake prompt. Creation completion confirms queueing, not that the
requested work has finished.

Use `thread create --reply-required` to request the initial result back in the
creating agent's inbox; `--no-reply-required` explicitly selects the default
direct-user behavior. The recipient answers with
`agent-coord reply --message-id <initial-message-id> '<result>'`. This preserves
the coordination thread, correlates the reply, and wakes an eligible idle creator
without another reply obligation. A final answer in the specialist's chat alone
does not send that reply. Direct user follow-ups still work, and later assignments
independently choose `send --reply-required` or `send --no-reply-required`.

For Python callers, `AppControl.request("create", sender_session_id, payload, ...)`
accepts an optional boolean `payload["reply_required"]`, defaulting to false.
Reuse the same request key and reporting choice on retries; changing the contract
under an existing key is rejected. Explicit false and omission are equivalent,
including for requests saved before this option existed.

Initial reply reporting requires the updated app backend. Plugin refresh makes
the CLI and skills available but does not replace a running native app's backend;
activate native changes through a separate app release. Confirm
`result.reply_required` is true for a reply-required creation. An older backend
omits that receipt field and uses direct-user behavior; do not assume it registered
the requested reply or create a duplicate agent. Use the returned identity for a
later explicit `send --reply-required` request if coordination is needed.

`queued` awaits an updated app in the target workspace; `running` means claimed.
Reuse the same request key and arguments after a timeout. `uncertain` is never
automatically replayed: inspect the saved identity and inventory before retrying.
`thread cancel-request --request-id <key>` cancels a request that has not started.
The bridge uses the shared database; no browser window, port discovery, or HTTP
token is required.

Find existing specialists across open and closed conversations before creating
another. Search returns compact pages (10 results by default, maximum 50):

```bash
agent-coord thread search 'release validation' --app-only --limit 10
agent-coord thread search 'release validation' --app-only --limit 10 --cursor <next_cursor>
agent-coord thread show --session-id <agent-id>
```

Each keyword must match saved context (title, original request, latest checkpoint,
or artifact labels/paths), or all keywords must occur in one saved conversation
message. User and assistant prose are searched; tool output and provider setup
instructions are excluded. Use `--source context` for routing context alone,
`--source messages` for conversation prose, or `--my-messages` for user messages
alone. Results rank relevance first and meaningful work recency
second, and include the stable identity, app link, placement, provider, summary,
and wake pause state. `last_work_at` uses turns, completions, and new checkpoints;
moving/renaming a thread or refreshing an identical checkpoint does not make its
context fresh. Search globally by default, or select `--repository`, `--project`,
`--no-repository`, `--no-project`, or an explicit `--cwd`. Omit `--app-only` to
also find terminal conversation context. Search indexes saved app history locally;
it does not scan provider transcript folders or resume agents. Each result includes
up to three message excerpts, roles, known timestamps, matching-message counts,
and links to the matching turn/item. Coverage reports missing or partial saved
histories rather than implying that all provider history has been searched.
Continue with the same query and filters until `next_cursor` is null; pages are
live, so restart discovery if conversations change during paging.

Inspect promising matches and current source before routing. Old context is a
lead, not proof of how today's application works; ask the specialist to verify
its assumptions. There is no automatic age cutoff or requirement to create a
replacement. Reuse the selected app conversation with:

```bash
agent-coord thread settings --session-id <agent-id>
agent-coord thread settings --session-id <agent-id> \
  --model <advertised-model> --effort <supported-level> \
  --request-id <unique-settings-key> --wait 30
agent-coord ui link --thread <agent-id>
agent-coord send --session <agent-id> --classification action_required \
  --no-reply-required 'The next request; answer the user in your conversation.'
```

Settings accept `--yolo` or `--default-permissions`. Changes wait for idle and
queued user follow-ups, and reject closed or pending-close conversations. A user
settings change supersedes a pending agent settings change. Confirm the receipt
before sending work that requires the new settings. Completing an assignment
and releasing its file scopes leaves the conversation available.

After inspecting a failed app agent you created, queue a fresh actionable
continuation with `send`, including instructions to inspect existing results
before repeating side effects. Then authorize a new inbox turn:

```bash
agent-coord thread resume --session-id <agent-id> \
  --expected-turn-id <failed-turn-id> --request-id <unique-recovery-key> --wait 30
```

Recovery runs through the owning app runtime and only accepts the creating
orchestrator. The expected turn must still be the latest failed turn, with a
confirmed failure pause and fresh actionable inbox work. Stop, uncertain wake
submissions, closed/pending-close threads, and queued user input remain blocked.
Recovery never replays the failed prompt or resets uncertain wake attempts.
A completed receipt's `result.state: ready` confirms authorization, not execution.
Reuse the request ID for retries. Direct user input handles deliberate Stop and
pause records saved by older runtimes without a confirmed failure cause.

Direct user follow-ups steer Codex or queue for Claude; dispatcher messages may
arrive through tool hooks during a turn. Automatic inbox wake waits for idle and
prioritizes queued user prompts. Native history and settings survive restarts,
although active work is interrupted and uncertain submissions are not retried.
Ordinary actionable messages also wake closed app conversations, restoring the
same provider identity, context, model, effort, and permissions. Saving new
actionable work automatically moves a closed app conversation into Now, even
when execution is paused or the app is offline. Now/Later placement and snoozes
are otherwise preserved. Closing alone does not cancel older pending work;
an eligible wake for that work also reopens the conversation. Informational and
closure messages neither reopen conversations nor wake agents.
Stop, failed/uncertain submissions, pending Close, and queued user prompts remain
separate gates. Stopped or failed conversations need accepted direct user input
to resume automatic wake. Automatically reopened conversations accept direct
user follow-ups without a separate Reopen action. An updated running app is
required to execute pending work; messages queue while it is offline.

The existing `delegate` command retains its scoped terminal-worker behavior.

## Delegate work to an Agent Coord-owned PTY

A registered Codex or Claude parent can launch a new Codex or Claude Code worker
with a task prompt and explicit file scopes. Codex remains the default child
for backward compatibility; pass `--client claude` to select Claude Code. The
default `managed-pty` runtime does not require Zellij or tmux. First, preview and
validate the launch without changing SQLite or starting a process:

```bash
agent-coord delegate \
  --from-session <parent-session-id> \
  --cwd /absolute/path/to/repository \
  --scope 'src/**' \
  --scope 'tests/test_feature.py' \
  --client claude \
  --name delegated-feature \
  --model <client-model> \
  --effort <level> \
  --lease-mode write \
  --dry-run \
  'Implement the feature, run the focused tests, and report the result.'
```

To associate an existing issue, add `--bead <ready-bead-id>`. This opts into
Beads validation and instructs the worker to claim and complete that issue.
An explicit invalid issue remains an error. Omitting `--bead` works when `bd`
is absent or its database is unavailable; progress and results are recorded in
Agent Coord work-thread checkpoints and delegation state.

Remove `--dry-run` to launch the worker. Agent Coord starts a detached
supervisor, allocates the child a controlling PTY, captures its output, and
keeps the interactive client alive at its prompt between turns while the
delegation is unfinished. After a completed or failed result, it disables wake,
sends EOF, and terminates the child if necessary. The supervisor
does not run inside the parent's PTY, so it survives the parent process and does
not compete with the parent's terminal input or rendering.

To use the previous pane-based adapter explicitly, pass `--runtime zellij` and
`--zellij-session <name>`. When already inside the target Zellij session, the
command can read `ZELLIJ_SESSION_NAME`. Add `--floating` for a floating pane;
`--width` and `--height` set its dimensions. Supplying a Zellij-only option
without an explicit runtime remains a compatibility shorthand that selects the
Zellij adapter.

Use `--model` and `--effort` to select the child model and effort. For Codex,
Agent Coord maps effort to the `model_reasoning_effort` configuration value; for
Claude Code it uses `--effort`. `--reasoning-effort` remains an alias for
backward compatibility. Both options are independent and optional. Agent Coord
records the requested values in durable delegation state and lets the selected
client validate model and effort support.

Use `--lease-mode validation` to launch an independent validator. Its scopes
form an exclusive stability reservation. The generated prompt prohibits
repository edits and remediation. It requires a compact verdict with each
command, exit status, duration, summary, and material finding. A failed check
completes the validation task when all required checks ran and the validator
reported the failure. Use a separate implementation task for remediation and a new
validation task for the next attempt; create Beads issues only when using that
integration. Agent Coord rejects `--yolo` with a
validation lease.

The reviewed command opens the selected interactive TUI in the owned PTY. Codex
uses `--approve-for-me`; Claude Code uses its safety-classified
`--permission-mode auto`, which can still deny or request confirmation for
risky actions. The optional `--yolo` flag maps to
`--dangerously-bypass-approvals-and-sandbox` for Codex and
`--dangerously-skip-permissions` for Claude Code. Use it only after explicit
authorization.

Codex also requires persisted trust before it runs hooks. Review and trust the
installed Agent Coord hook before delegation. If the target repository's
complete enabled hook set was reviewed, `--bypass-hook-trust` starts the
interactive Codex session with
`--dangerously-bypass-hook-trust`. This separate flag keeps reviewed command
permissions but runs all enabled hooks without persisted trust for that one
invocation. Do not use it as a default. Claude Code has no corresponding
`--bypass-hook-trust` launch option, so Agent Coord rejects that combination.
An interactive Claude Code child can still show its normal repository trust
prompt when the repository has not been trusted previously.

Before launch, Agent Coord verifies the Git repository, the selected client's
login, live write-scope conflicts, and active delegation state. Supplying
`--bead` additionally requires `bd` and a ready issue. The Zellij adapter
additionally verifies Zellij. Agent Coord
then creates one durable delegation record and starts the child with an
inherited delegation ID and explicit client identity. Reviewed mode adds only
the Agent Coord database directory as an extra allowed root, so child CLI calls
can update lifecycle state outside the repository. The child SessionStart hook
verifies the client and attaches the new session. The generated instructions
require the child to:

1. Read repository instructions.
2. When an issue was supplied, run `bd prime`, verify it is ready, and claim it.
3. Declare the exact scopes with the selected lease mode.
4. Implement and run focused checks, or validate without editing.
5. Apply repository validation, changelog, and git-authority rules.
6. Save a work-thread checkpoint and record a compact completed or failed result
   before exit. When using Beads, also update and close the issue as appropriate.

Inspect delegation state with:

```bash
agent-coord delegation status --delegation-id <delegation-id>
agent-coord delegation list --parent-session <parent-session-id>
agent-coord delegation list --parent-session <parent-session-id> --active
agent-coord delegation logs --delegation-id <delegation-id>
agent-coord delegation cancel \
  --delegation-id <delegation-id> \
  --from-session <parent-session-id> \
  --message 'The child did not attach.'
```

The managed supervisor watches the local SQLite inbox. When its attached child
is inactive at an idle or waiting prompt and has an undelivered actionable
message, the supervisor atomically reserves the wake and submits one generic
prompt through the owned PTY. The normal prompt hook supplies the durable
message body, thread, and reply metadata. Children can therefore negotiate with
the parent or with one another while remaining long-lived; informational and
closure messages do not cause turns.

Managed terminal traffic is persisted and bounded to approximately 1 MiB per
delegation under the Agent Coord state directory. The output reader interprets
cursor movement, line erasure, and screen repaint sequences before display, so
interactive progress updates appear as one readable terminal screen instead of
concatenated animation frames. `delegation logs` returns that rendered recent
output without giving the caller an arbitrary file-read path. If the client
exits before reporting a terminal result, the supervisor records the exit code,
marks the delegation failed, and notifies the parent.

### Browser sessions and coordination UI

Start the dependency-free local UI with:

```bash
agent-coord ui --parent-session <parent-session-id>
agent-coord ui --cwd /absolute/path/to/repository
agent-coord ui --cwd /opt/projects
```

The home page lets developers create independent Codex sessions, choose a
workspace and an available model/reasoning effort, chat with streamed responses,
answer approvals and questions, stop a running turn, rename sessions, and
close or reopen conversations. Creating a session requires neither a parent
agent nor a Beads issue. Different sessions can run concurrently; an individual
session accepts one active turn at a time.

Choose **Schedule…** beside Send to save a one-time text prompt. In an existing
conversation, it sends your exact message back into that thread, preserving its
history and using its workspace, model, reasoning, and permissions at delivery.
A busy thread shows **Waiting for thread** until its current turn and queued
messages finish; waiting schedules can still be edited or cancelled. A closed
or unavailable destination fails visibly instead of creating another thread.

Scheduling from a new-conversation draft creates a new thread. Set its workspace,
date and time (with your browser's time zone shown), model, reasoning effort,
and permissions, and include the context the agent needs. **Full access** uses the same unrestricted,
no-approval settings as session YOLO mode; workspace access can pause for approval.
Saving a schedule does not start a model turn. Attachments and recurrence are
not supported.

Open **Scheduled prompts** in the sidebar to edit or cancel a pending run and
open its result thread. For new-thread schedules, the saved model and reasoning
are checked again at launch; an unavailable setting fails visibly instead of
choosing a substitute.
The Mac must remain awake and Ribbon Field must remain running. Future schedules
survive restarts. A time already past when the backend starts, or over one minute
late during operation, becomes **Missed** and waits for **Run now** or rescheduling.
The one-minute allowance covers ordinary dispatch delays, not a promise to wake
the computer. Closing the browser tab does not stop the backend scheduler.

Schedules persist in the coordination database. Atomic claims prevent concurrent
runtimes from launching the same prompt; a retried save uses the same request ID.
An interrupted dispatch is never automatically sent again. After a stopped
runtime's lease expires (up to two minutes after a crash), the worker reads the
original turn's history and records its result when known; otherwise it shows
**Needs review**, with a thread link when available. **Completed** means the
scheduled agent turn ended successfully, not that every requested deployment or
business outcome was independently verified. Subsequent conversation turns do
not change the original scheduled run's result.

Use the **Expand chat** icon beside the thread actions to fill the window with
the title, conversation, and input bar. **Collapse chat** or Escape restores the
normal layout and your sidebar preference. Returning to the overview also exits
expanded mode.

Drop PNG, JPEG, WebP, or GIF files into an open browser conversation
to include up to four images of 5 MiB each. Review the
thumbnails and remove individual attachments before sending. Images work with
Send, Steer, and Queue, with or without text; sent and queued images appear in
the conversation. Failed sends keep the draft available for retry, and queued
images survive a server restart for review before resuming.

**Close thread** ends the live session and returns to the open overview. A
running session offers **Stop and close**, which waits for the turn to stop
before closing. **Closed** history retains conversations, checkpoints, links,
and session settings; **Reopen** brings a thread back so you can continue it.
Closing does not mark its task complete. **Later** keeps work you intend to
return to in the open workspace. Existing archived threads appear in Closed.

Browser sessions close through Codex's per-thread shutdown, leaving other
sessions running. For a terminal Codex, the UI only stops a process verified
by its open conversation transcript; it never targets a remembered pane ID
or a shared app-server process. If that identity cannot be verified (including
Claude terminals), exit the terminal session first, then close the thread.
The terminal's shell or Zellij pane stays open. Closed history can be read
without resuming execution.

Agents can request the same lifecycle action through the CLI and shared database,
without discovering the UI's port or making an HTTP request:

```bash
agent-coord thread close --session-id <id>
agent-coord thread close --session-id <own-id> --after-turn
agent-coord thread close-status --session-id <id>
agent-coord thread cancel-close --session-id <id>
```

Use `--after-turn` for self-closure, after saving the checkpoint. The runtime
waits for that turn to finish, so the final response can arrive. New input or a
new turn cancels a queued close, including steering and queued follow-ups.
Results distinguish `queued`, `closing`, `closed`, `cancelled`, and `failed`;
accepting a request does not mean the thread has closed. An ended terminal
can close immediately without a runtime. Other requests persist until an
updated Ribbon Field runtime serving that workspace can handle them. Restart
the updated app if necessary; refreshing installed plugins alone does not
replace an already-running backend. Failed requests retain their error and can
be retried. Requests interrupted by a runtime crash report failure for review
instead of automatically stopping a possibly resumed session. Cancellation is
available until execution starts. The existing UI process ownership checks,
history retention, and separation between closure and task completion apply.

The backend starts `codex app-server` lazily and communicates over stdio using
the [Codex App Server protocol](https://developers.openai.com/codex/app-server).
Open **New session**, then choose a model from the **Codex**, **Claude Code**, **Google Antigravity**, or **Kimi Code**
groups in the composer’s **Model** picker. The provider session starts when you send.
For Codex or Claude, install and authenticate the selected CLI first (`codex login` or
`claude auth login`). Antigravity and Kimi setup are described below. Existing conversations retain their provider. Codex sessions use
workspace-write sandboxing and on-request approvals routed to the developer by
default. **New session** and native **⌘N** open a draft conversation in the current
workspace. You can change providers and models before sending the first message.
Existing conversations offer models from their current provider. Use `/permissions yolo` or the
conversation footer’s **Permissions** control to disable sandboxing and approval
prompts for that session (full machine access). `/permissions default` restores
workspace access and approval prompts. The choice is saved
and preserved when the session resumes; other sessions keep their own settings.

Claude conversations use the installed `claude` CLI's bidirectional stream-json
protocol, with one process per conversation and no extra runtime dependencies.
Text, thinking, tool activity, approvals, questions, and attached images appear
in the same chat UI. Claude's normal permission mode honors its configured rules
and asks for restricted tool actions; it is not Codex's workspace sandbox.
YOLO explicitly selects Claude's bypass-permissions mode. Model and effort
choices come from the installed Claude CLI. Claude retains its native transcript
for resume; Agent Coord retains the display history across restarts. While Claude
is working, **Enter** queues a follow-up. **Stop** ends that conversation's process
and pauses its queue; the next message resumes its saved transcript. Importing
terminal Claude conversations is not yet supported. Idle Claude conversations
can be forked into independent threads with the same history and settings.

Google Antigravity conversations use Google's official ACP runtime. Install and
sign in on the machine running Ribbon Field:

```bash
agent-coord antigravity install
agent-coord antigravity login
agent-coord antigravity status
```

The installer downloads a pinned Google release and verifies the archive's
SHA-256 and file sizes before publishing the executable and its matching helper.
It supports macOS, Linux, and Windows on x64 and ARM64. For a manual installation,
download the pair from the [official ACP registry](https://github.com/agentclientprotocol/registry/blob/main/antigravity-acp/agent.json),
make both files executable, and set `AGENT_COORD_ANTIGRAVITY_BIN` to the ACP
executable when launching Ribbon Field. The Antigravity IDE and Gemini CLI are
different programs and cannot be used as this executable.

`login` opens Google sign-in and checks account access. Its credentials and native
sessions stay in a private Antigravity profile beside the Agent Coord database,
separate from the IDE and other ACP clients. Personal Google sign-in is supported;
API-key and enterprise authentication are not yet exposed. When using Ribbon
Field remotely, run these setup commands in a terminal on its host machine and
complete sign-in in a browser on that machine. Model discovery never opens a
sign-in browser automatically. After setup, reopen the model picker to retry it.

Select **Google Antigravity** in the Model picker to start a conversation. Models
come from the signed-in account; no model IDs or reasoning levels are assumed.
Text, thinking, tools, images, native approvals, and fixed-choice questions use
the existing chat interface. Default permissions use Antigravity's native tool
approval mode; YOLO selects its native `yolo` mode. This is not an OS sandbox.
Enter queues follow-ups while a turn runs. Stop terminates that conversation's
agent and helper; the next message resumes its native session. Saved display
history opens offline. Forking and steering are not supported. Agent Coord
identity, the installed skill path, current scope state, and CLI examples are
supplied at startup and resume. Codex, Claude, Kimi, and Antigravity use the same
startup guidance and structured-write policy.

Before a native conversation starts, the adapter installs a `PreToolUse` hook
in the private ACP profile at `profile/config/hooks.json`. It preserves other
hook entries. The hook uses the Ribbon Field session identity and checks file
creation and edits against the current scopes,
validation leases, activity, and live conflicts. It recognizes the ACP names
`create_file`, `edit_file`, and `multi_edit_file`, and their documented aliases.
Malformed hook inputs return a denial. These checks apply in default and YOLO
modes. ACP tool notifications are not used to veto writes.

These are coordination guards, not a filesystem sandbox. Shell-mediated writes,
including `rm` and `mv`, and targets outside the registered repository are not
covered. The same shell limitation applies to Codex, Claude, and Kimi. Do not
interpret a scope declaration as OS-level write isolation. The native hook
contract is described in [Google's hook documentation](https://www.antigravity.google/docs/hooks).

After refreshing installed plugins, run the opt-in native regression probe with
an authenticated ACP runtime. It checks allowed and forbidden file creation,
single- and multiple-occurrence edits, and resumed conversations in both modes.
It writes only disposable fixtures and prints the result directory:

```bash
python3 scripts/probe_antigravity_writes.py \
  --plugin-root '<verified-installed-plugin>' \
  --runtime-dir '<installed-runtime-directory>'
```

The probe uses the existing private provider profile. Use `--provider-dir` when
that profile is stored at a different path. It does not test shell isolation.

Antigravity receives actionable coordination messages in its native prompt.
Peer messages arriving during work remain queued until the native prompt ends.
ACP has no steering operation, and ACP 1.3.0 ignores hook `injectSteps`, so
Ribbon Field does not cancel running tools or pending approvals for delivery.
The shared inbox wake worker starts the next turn in the same conversation
after natural completion. User Stop, failure, pending Close, and queued user
input retain their normal wake guards.

Each prompt contains at most eight messages and 16,000 characters of message data.
Messages that do not fit remain queued. A large message includes a retrieval
command instead of a partial body. Use `agent-coord message --message-id <id>`
to read up to 8,000 characters. Use its `next_offset` with `--offset` to read the
next page. The sender and recipient can use this command to inspect submission
state, pending age, delivery errors, wake blockers, and recipient activity.
Reading a page does not acknowledge the message. A successful explicit reply
acknowledges its original message; otherwise, the agent must acknowledge each
read message by ID with `ack`. Submission is evidence of transport, not task completion.
An accepted submission remains submitted even when a later provider turn fails;
only attempts without receipt evidence become uncertain.
Unacknowledged messages can be delivered again on a later user turn. Stable IDs
prevent repeated delivery attempts within one app turn. Native output and tool
events refresh the agent's activity timestamp.

The integration follows [T3 Code's Antigravity ACP support](https://github.com/pingdotgg/t3code/blob/main/apps/server/src/provider/acp/AntigravityAcpSupport.ts)
and [runtime setup](https://github.com/pingdotgg/t3code/blob/main/apps/server/src/provider/antigravityAuthSupport.ts).

Muse conversations use [Muse Code](https://dev.meta.ai/docs/muse-code) through
its native `muse serve` session protocol. Install Muse Code and run `muse login`
on the machine running Ribbon Field, then select **Muse Code** in the model
picker. Model and reasoning choices come from the runtime; Muse Wonder Full
requires access through a Muse Code subscription. Ribbon Field does not store
the account's credentials or call the model API itself.

The adapter uses Muse protocol schema version **1**, with Muse Code **1.4.8**
as its reference fingerprint. A different fingerprint is logged for diagnostics
and does not block a conversation: Muse can add compatible fields and events
in routine updates. Unsupported schema versions and failures to apply the
requested permission profile or preserve session identity still stop startup.
This follows [Muse's wire compatibility guidance](https://meta-models.github.io/muse-code-sdk/next/compatibility/#the-wire-protocol).
Set `AGENT_COORD_MUSE_BIN` to choose an executable if it is not on PATH.
The usual `~/.local/bin/muse`, Homebrew, and
`<Agent Coord state directory>/providers/muse/runtime/muse` locations are also
discovered. App-owned hosts disable the launcher's automatic update for their
own launches; other uses of the shared launcher can still update Muse.

Each conversation owns a Muse host. Default permissions select Muse's native
`:ask-me` profile: workspace sandboxing and approval for protected actions.
YOLO selects `:unrestricted` and disables the host sandbox. The adapter verifies
the committed profile on both start and resume before sending a turn, including
when changing back from full permissions. Muse's workspace trust still applies: open the workspace
in Muse to review and trust its rules, skills, and project hooks. Hooks for
coordination are merged with user hooks in a private temporary settings overlay;
Muse 1.4.8 does not load hooks supplied with `-c hooks`. Credentials, trust and
plugin files remain linked to the user's configuration, and global settings are
not rewritten. The hooks explicitly carry the Ribbon Field session
identity through Muse's filtered hook environment and guard structured file
writes. Native Muse children share that write scope without consuming their
parent's coordination inbox. As with other providers, shell-authored writes are
outside the coordination path gate; Muse's native permission policy still applies.
In default mode, coordination commands that access state outside the primary
workspace need native shell elevation (`sandbox_permissions: require_escalated`)
and its approval prompt. Muse receives this instruction with each turn. Approving
an ordinary sandboxed command alone does not widen filesystem access; full
permissions avoid these prompts.

While Muse runs, **Enter** steers its current turn and **Tab** queues a follow-up
for after the turn. Steering targets the named active turn; a correction is
rejected if that turn has already finished. Text and images can be steered
together. Stop affects only that conversation; the next message resumes its
native session, including after an app restart. Workspace
changes require a new Muse conversation. Approvals preserve native choice and
requirement identifiers, including changes to a pending approval. Single-choice
questions accept custom text; multiple-choice questions use the native options.
Fork and native terminal-session import are not exposed for Muse.

The protocol contract and upstream test transcripts are published in
[Meta's Muse Code SDK](https://github.com/meta-models/muse-code-sdk).

Kimi conversations use the current [Kimi Code CLI](https://github.com/MoonshotAI/kimi-code)
through `kimi acp`. Install and authenticate it on the machine running Ribbon Field:

```bash
brew install kimi-code
kimi login
```

The npm alternative is `npm install -g @moonshot-ai/kimi-code`; use a Node.js
version supported by that package (2.1.1 requires Node 22.19 or newer). The archived
Python `kimi-cli` package is not supported. Set `AGENT_COORD_KIMI_BIN` to the
executable path if the app cannot discover it. `KIMI_CODE_HOME` is honored, so
login and the app must use the same data directory. Ribbon Field reuses Kimi's
own authentication and configuration; model discovery never starts login.

Select **Kimi Code** in the Model picker. Models and their thinking choices come
from Kimi's ACP configuration, including K3 when configured and available to
your account. Text, thinking, tools, images, approvals, and multiple-choice
questions appear in the existing conversation UI. Default permissions select
Kimi's manual approval mode; YOLO selects its native `yolo` mode. Kimi executes
its tools with native permissions, not an OS sandbox. Follow-ups queue while a
turn runs. Stop cancels and closes that conversation's process; the next message
resumes its native session. Display history is available without starting Kimi.
Native sessions keep their original workspace, so use a new conversation to
change directories. Importing terminal sessions, forking, and steering are not
exposed in this first integration.

When a Kimi conversation starts, Ribbon Field maintains a marked hook block in
`~/.kimi-code/config.toml` (or `$KIMI_CODE_HOME/config.toml`), preserving other
settings and hooks. These hooks are inert outside processes launched with a
Ribbon Field Kimi session identity. They deliver coordination context and inbox
messages at prompt submission and check structured writes against declared
scopes. The app owns turn lifecycle and checkpoints retain the Ribbon Field identity across native
restarts. As with other providers, scope checks are coordination guards, not
shell sandboxing. Model discovery uses and removes a disposable native session
without installing hooks or changing an existing conversation's model.

The adapter follows Kimi's [ACP reference](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-acp.html)
and [hook contract](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/hooks.html),
and shares the dependency-free ACP subprocess transport with Antigravity.

The provider lifecycle and permission mapping were informed by
[T3 Code's Claude adapter](https://github.com/pingdotgg/t3code/blob/main/apps/server/src/orchestration-v2/Adapters/ClaudeAdapterV2.ts)
and the [official Claude Agent SDK transport](https://github.com/anthropics/claude-agent-sdk-python/tree/main/src/claude_agent_sdk/_internal).

Click the conversation title to rename it inline; Enter saves and Escape cancels.
While Codex is working, **Enter** or **Steer** sends instructions to the active
turn using Codex's steering API, which applies them at its next opportunity.
**Tab** or **Queue** saves a follow-up to start as a new turn after the current
turn finishes. Queued messages appear above the composer, can be removed, and
run in order even if you switch conversations or close the browser tab.
**Stop** interrupts the current turn and pauses queued messages; failed turns
also pause the queue. Use **Resume queue** when ready. After a server restart,
saved queued messages require review and resumption. Shift + Tab, empty Tab,
and Tab while idle keep normal keyboard focus navigation.
If the turn ends before steering arrives, the draft stays in the composer so
you can send it as a new message.
The bottom status line shows the working directory, resolved model, and reasoning effort.
Send `/cd` to see the directory or `/cd <path>` to change it. Relative paths resolve
against the current directory; absolute paths, `~`, and quoted paths are supported.
Directory changes preserve the conversation and apply to subsequent turns. Send `/model`
to list available models, `/model <model-id> [effort]` to switch, `/effort` to
list supported effort levels, or `/effort <level>` to change effort. `/help`
lists these commands. Commands do not create model turns; changes apply to the
next message and persist across server restarts. Wait for a running turn to
finish before changing settings. When switching models, an unsupported effort
falls back to the new model's advertised default.
The slash menu completes model names and supported effort levels. In Codex chats,
type `$` anywhere in the message to browse enabled skills for the current working
folder. Keep typing to filter skill names, use arrow keys to move, and Enter or
Tab to insert a shortcut; Escape dismisses the menu. Skill selection updates the
draft without sending it. In the UI,
`/fork` opens a new thread from the current idle Codex or Claude conversation. `/close`
stops any running turn and closes the current thread, keeping its history and
returning to the overview. These two commands take no arguments or images and
run immediately, including when submitted with the queue shortcut.
The UI uses the installed Codex configuration and plugins without bypassing
hook trust. Trust the Agent Coord hooks through the normal Codex setup so its
write guards run in browser sessions as they do in terminal sessions.

Each provider owns the model's native conversation history. The shared Agent Coord database
stores browser-created session names, workspaces, and model settings, plus a
display copy of streamed conversation items. The display copy supports Codex
builds that cannot yet query stored turns through app-server.
Reloading or closing the browser does not stop work while the UI server runs.
Stopping the UI server stops its Codex app-server, Claude, and Antigravity processes; saved conversations can be
reopened after restarting the UI. Browser sessions register in the same
coordination store as terminal sessions. Existing terminal/delegated sessions
remain visible in the coordination monitor and are not taken over by the chat
interface.

`--cwd` (also available as `--repo`) filters browser sessions and coordination
to a directory and its descendants. For example, `--cwd /opt/projects` lets
you switch into its repositories or nested folders with `/cd <path>`. A repository filter also includes
its linked Git worktrees. Without `--cwd`, developers can select any workspace
and the monitor shows repositories in the shared database. `--parent-session`
filters the coordination monitor only. Open **Coordination monitor** (or
`/monitor`) to inspect the parent/child process tree and standalone sessions.

The tree can be sorted by name, creation time, or last activity. Last activity
is the default, with the most recently active parent groups and children first;
parent activity includes activity by its children. Selecting a parent shows
clickable child cards with status, task, and recent output. Selecting a child
shows its bounded recent output, complete received-message history (including
delivered and acknowledged state), and activity metadata. Snapshot reads do
not deliver or acknowledge messages.

For a live Zellij delegation, snapshot reads also capture the pane's rendered
screen into the delegation's bounded output file. The last successful snapshot
remains available after the UI restarts or the pane disappears. Output from an
older Zellij delegation cannot be recovered when its pane was already gone
before any snapshot was saved; the UI explains that case and shows the durable
final result instead of implying that capture is still pending.

The pages update automatically. Use `--no-browser` on a headless machine and
`--port <port>` to choose a port. The backend binds only to loopback addresses;
browser mutations require a server-issued token and same-origin requests.
For iPhone access, choose **Remote access** in the local sidebar to enable
private Tailscale HTTPS and create a one-time pairing link. Both devices must
join the same tailnet. Paired browsers share the Mac's conversations and can
send messages and answer approvals. The Mac controls pairing, device revocation,
and disabling the route. CLI hosts can use `agent-coord ui --tailscale` with
an optional `--tailscale-port 8443`. See [Remote access](../docs/remote-access.md)
for setup, restart behavior, and the T3 Code implementation references.
The coordination monitor remains read-only. Follow-up work for PTY delegations
continues to use durable `send` messages.

When a delegated worker reports a terminal result, its next `Stop` hook records
session token usage in the delegation row and writes:

```text
<delegation-cwd>/.agent-coord/delegations/<delegation-id>.usage.json
```

The artifact contains identifiers, client, model, terminal status, raw
client-specific counters, and normalized input, cache, output, reasoning, and
total token counts. It never copies prompt or response text. Codex uses the
latest cumulative token-count event. Claude Code assistant records are
de-duplicated by message ID before their per-response counters are summed.
`SessionEnd` performs the same capture as a fallback when a worker exits before
reporting a result.

`delegation status` and `delegation list` expose `token_usage`,
`token_usage_artifact_path`, and `token_usage_error`. Transcript formats belong
to the selected client and may change; an unreadable or unrecognized transcript
records `token_usage_error` without changing the delegation outcome or breaking
the lifecycle hook. The `.agent-coord/` directory is local runtime output; add
it to the repository's ignore policy if it should stay out of working-tree
reports.

### Thread token usage

The conversation footer shows provider-reported token consumption for the current
thread. Open the count to see input, output, cache and reasoning counters, model
breakdowns when available, and whether the count includes provider subagents.
Partial coverage is labeled; unavailable telemetry is not displayed as zero.
The last reported context usage is a separate capacity meter and is never added
to consumed tokens. Independent app-agent threads retain their own usage.

Usage is persisted locally in SQLite. Cumulative provider snapshots replace earlier
snapshots; request records are deduplicated by their native event identity. Forks
exclude inherited parent consumption when the boundary is known and report unknown
coverage when it cannot be established. Native cache conventions are normalized
without adding cache or reasoning subsets twice. Only counters and attribution
metadata are stored, not copied prompt or response text.

Codex, Claude Code and Muse Code collect live usage from their native protocols.
Kimi Code imports its native usage journals, including supported compaction usage,
with explicit main-agent and child coverage. Antigravity currently exposes context
usage through its ACP bridge; consumed tokens remain unavailable until that bridge
provides a consumption feed. These counts describe recorded token processing and
do not establish actual subscription debits or billed amounts.

Opening a thread can recover existing local usage once without submitting a model
request. For an explicit recovery or to inspect the saved counters from a terminal:

```bash
agent-coord thread usage --session-id <thread-id>
agent-coord thread usage --session-id <thread-id> --backfill
```

An explicit native transcript can be supplied with `--backfill --transcript-path
<path>`; the importer verifies its native identity. Missing, truncated, unsupported,
or interrupted records retain partial or unavailable coverage. Recovery is bounded
and does not scan transcript histories during overview polling.

The result creates a durable message in the parent inbox. If the child process
exits before it reports a result, the SessionEnd hook records the delegation as
failed and sends that failure to the parent. A second active delegation for the
same repository and supplied Beads issue is rejected. Independent tasks without
issues have separate delegation IDs and still obey scope-conflict checks.
The parent can use `delegation
cancel` to release an active record after a confirmed launch or attachment
failure.

### Work threads across repositories and groups

To dedicate a fresh agent session to managing threads, start it in any directory
and ask **Help me manage my open threads**. The installed **Thread Manager**
(`manage-threads`) skill reviews all recorded workspaces by default, summarizes
what needs attention, and carries out your requests to rename, organize, or
move threads. In Codex you can also invoke it explicitly with `$manage-threads`.
The source checkout and Beads are not required. The startup hook advertises the
skill and the global `thread list` command; the plugin's default prompt starts
this review. Saved checkpoints and history provide the review context, with
stale or missing progress called out as uncertain.

To take the user directly to a view in the macOS app, from any directory:

```bash
agent-coord ui open --project "Billing" --from-session <session-id>
agent-coord ui link --project "Billing"
agent-coord ui open --view "Release review"
agent-coord ui open --repository /opt/projects/example
agent-coord ui open --thread <session-id>
```

Use the installed plugin's absolute CLI path when it is not on PATH. Names match
exactly, ignoring case; IDs also work, and ambiguous repository names require an
ID or root path. Group and repository filters can be combined; `--no-project`
and `--no-repository` select unassigned threads. A saved view or thread is a
separate destination. With no selection the command opens the All work overview.

`ui link` returns a stable `agentcoord://` app link. `ui open` launches or focuses
Ribbon Field.app and waits briefly for the UI to acknowledge the destination.
`status: displayed` confirms navigation; `requested` means macOS accepted the
request but display is unconfirmed, and `failed` includes the UI's error.
`--wait 0..30` controls that wait. An app built with URL support must be installed.
Links are bound to the selected coordination database; a different app database
produces an error instead of showing unrelated work.

`--from-session` selects the native window that submitted the session's latest
message, including queued messages when dispatched. If that window has closed,
the app uses its current window. Navigation preserves conversation drafts and
supports Back. Group/repository links use temporary All work filters and do
not edit named saved views, change thread placement, or send messages.
The existing `agent-coord ui [--port ...]` command still starts the browser UI.

**Open Project** selects a directory and opens it as a project tab. A project
can be an ordinary folder, a repository, a subdirectory, or a linked worktree.
When a project is already open, choose **New tab** or **New window**. Each window
keeps its own open projects and selection; macOS restores them by window slot.
Closing a project tab keeps conversations and running agents intact. On first
use, no directory is selected; **New session** asks for a project instead of
using the server process directory. Explicit `ui --cwd` supplies the initial
project and still limits permitted execution directories.

A conversation belongs to the exact directory it was opened from. Opening
`~/projects` does not show conversations launched from `~/projects/website`.
Accessing files in a child directory, changing a shell directory, or using
`/cd` changes neither project membership nor the project's creation default.
**New session** and **⌘N** always use the active project directory. Switching
projects preserves separate new-session drafts, attachments, filters and views.
Browser directory paths refer to the computer running Ribbon Field.

**Groups** are optional named collections within a project, directly accessible
beside **All conversations**. Choose **＋ Group**, or create one through a
conversation’s **Add to group** menu. A new group immediately appears in
navigation; there is no separate view to create or name. Opening a new group
shows its conversations arranged by stage. Each group remembers its filters,
arrangement (including optional domains), and scroll position independently.
Display preferences survive reopening on the same device; scroll is window-local.
**Now**, **Later**, and **Closed** stay within the selected group. **Reset filters**
restores the group’s default display without removing its membership.

Drag an overview card onto a group tab to assign it directly. Drops preserve the
conversation’s stage and Now/Later placement, offer **Undo**, and do not depend
on the destination’s display filters. Reopen closed conversations before moving
them this way. **Add to group** / **Change group** remains available without
dragging. **No group** removes the association. New sessions inherit the selected
group. The group’s **••• → Rename group** changes its name everywhere.

**Saved filters** are available in the **Filters** menu for reusable selections
such as “Needs me” across groups. Existing saved views remain available there,
with their identifiers, filters, display settings, and links preserved. Use
**Save current filters…** to create one. Changing selected saved filters saves
their definition automatically; failed saves offer **Retry saving**. Their
**•••** menu renames, duplicates, reorders, or deletes them. Deleting saved
filters keeps every conversation. On mobile, the overview picker includes groups
and explicitly labeled saved filters.

Groups and saved filters never change execution directories or file access.
Existing unscoped groups remain available for compatibility. Internal `project`
CLI commands, API fields, and deep links continue to identify groups. Domains
remain optional descriptions of the current work and do not change membership.

Typing in **Search conversations** searches the active project's conversations,
including Later and Closed. **Current view** also applies its placement, group
and phase filters; **My messages** restricts matches to your messages. Click a
matching excerpt to open that message. Search reports saved-history coverage.
The separate repository selector and editable repo association are no longer
part of the UI. Legacy repository metadata and CLI links remain compatible.

Use **Ctrl + Shift + Left/Right** to cycle project tabs outside text fields.
With a project tab focused, Left/Right, Home and End navigate projects. Each
project tab shows its attention count. Groups and saved filters retain their own display settings and scroll position.
The navigation indicates attention and ongoing work.

The overview keeps durable work threads with attention first and Now, Later
and Closed controls. Group by phase, group or none. Cards show title, optional
group, phase, runtime state, checkpoint and next action. Selecting a conversation
shows its history and related links. You can edit checkpoints, add/remove links,
rename, park, reopen or close a conversation.

These features work without Beads or a Git remote. Git worktrees and nested
folders are separate projects even when Git identifies one repository. Registered
terminal conversations are also captured with their exact launch directories;
delegated sessions appear under their parent. The server's optional `--cwd`
restriction remains an execution boundary independent of the selected project.
Existing conversations are assigned their recorded working directory once during
migration, since older records did not retain a separate launch directory.
A closed Codex terminal conversation can continue in the browser. Active
terminal conversations stay with their existing client, and Claude conversations
continue in Claude Code. Existing terminal sessions begin capturing requests
when they load the refreshed plugin; earlier prompts are not reconstructed.

Choose **Fork** on an idle Codex or Claude app thread to open a separate conversation with
the same history. Saved Codex terminal threads can also be forked into the
browser. Reopen closed threads first; running threads and pending input must
finish before forking. The new thread has an editable title and a **Forked
from** link, and appears independently in the overview. Its workspace,
repository, group, model, and reasoning effort carry over. Browser permission
settings carry over too; terminal forks use the browser's default workspace
permissions. File claims, queued messages, approvals, checkpoints, pins, and
read markers start fresh. Both threads share the same files; forking does not
create a Git worktree. Opening a fork starts no model turn until you send a
message. This version forks through the latest available turn; selecting an
earlier turn and launching terminal forks are not included. Claude forks snapshot
the native transcript immediately and branch through Claude Code on their first
message, preserving the snapshot even if the original thread continues first.

The **Focus** overview has three vertical levels: **Attention**, **Replies to
read**, and **Idle**. Attention keeps running agents, unread execution updates,
and completed deliveries together, alongside blockers and required reviews or
decisions. Completed deliveries stay there until dismissed, even after reading.
Replies to read contains ordinary unread answers, findings, and proposals;
reading them moves the card into Idle at its existing work stage. Classifier
results distinguish delivery from findings; when classification is unavailable,
a current Finished checkpoint supplies the delivery fallback and other unread
answers remain in Replies to read. Later threads stay in Idle unless running;
Closed threads stay in Idle. The mini-map and arrow keys navigate all three levels, preserving the
selected group and migrating saved positions from earlier layouts.

The **Cards** overview keeps one **Attention** queue above fixed work stages:
**New → Investigating → Planning → Orchestrating → Implementing → Validating →
Deploying → Done**. Discussion and debugging belong in Investigating. Answering
an investigation leaves it in Investigating; Done means the requested change or
execution was delivered. Orchestrating identifies ongoing routing and coordination of specialist agents, including while they implement or deploy. Reading a response does not change its underlying stage.
Empty stages remain visible. Within each stage, working agents come first, then
cards sort by most recent activity; ties keep a stable order. The animated green
border indicates a working agent. Idle cards keep their full detail for six hours,
then show a shorter summary with fewer secondary details. After 24 hours they show
the title, group, and age, with unread indicators and controls retained.
Older cards use a quieter background without dimming their titles. Running, pinned,
and required-action cards keep their details; pins do not override activity sorting.
New activity restores the full card, and hover previews or opening the thread
retain access to its full context. The Attention queue does not decay.

### Peek at a thread without opening it

Hovering a card or a sub-agent row opens a lens beside it (on phones and touch
screens, where there is no hover, cards and sub-agent rows show a **Peek** button
instead):
what the agent is doing right now, where things stand, its next step, what the
previous checkpoint said, the sub-agents it created or delegated to, and the
latest message. Clicking inside the lens pins it so it stays while you read,
survives list refreshes, and can be closed with **×** or Escape. Each listed
sub-agent can be peeked in turn, with a back link to its creator, and **Open
thread** jumps into the conversation.

The "now" line comes from tool calls the runtime already observes: the
coordination hook records Claude Code commands and edits as they happen, and
the app server records Codex command, file-change, and tool-call items. The
agent reports nothing extra for it. Threads whose provider exposes no tool
events show their work phase instead.

Attention shows the reason each thread surfaced. With Jev enabled, the order is
**Blocked**, **Review requested**, **Update** about execution, **Findings ready**,
then routine **✓ Done · No action needed**. Required input and failed turns take
precedence. Reading an update, findings, or success clears its notification and
returns the thread to its stage. **Dismiss** acknowledges a response, including
a blocked reply or saved request for user action, and clears its current Attention
item. The blocker, explanation, and thread placement remain unchanged; dismissal
does not approve or resume work. A new response or changed checkpoint can surface
the thread again. Live approval and input dialogs remain pending until answered
or cancelled. Uncertain or unavailable classifications remain visible as Reply
and can also be dismissed. A follow-up consumes the previous response only when
the new turn starts.

A delivered thread stays quietly visible in Done until you close it or move it.
Pins order cards within a stage and never hide attention or override its priority.
Search, saved views, group filters, and optional association grouping
continue to apply. The saved `completed` filter now selects Done.

**Now**, **Later**, and **Closed** select deliberate placement. Use **Move to Later**
on a card to set it aside, or **Move to Now** to return it. Later stays out of the
current workspace and attention counts, with checkpoint summaries visible in its
own view. Parking preserves the pending response and does not stop work. Closed
conversations retain their history and can be reopened. No completion, read receipt,
attention classification, or grouping operation changes saved placement. New
actionable Agent Coord messages to closed app conversations reopen them into Now.

New-result markers track completed turns independently of progress checkpoints,
so opening a thread while an agent is working does not consume its future reply.
Read and handled markers survive a server restart. Handling a displayed older
response cannot clear a newer response. Existing open responses start unhandled
when upgrading, even if already read; saved placement and pins are preserved.
An interrupted turn or
a disconnected process does not establish task completion.

### Automatic reply classification with Jev

The UI backend can classify completed replies using TypeSafe's
[Choice API](https://docs.typesafe.ai/primitives/choice). A one-time, optional
setup prompt introduces automatic conversation status; **Skip for now** dismisses
it without enabling requests. Setup remains available in **Settings → Automatic
conversation status**.

Get an API key from the [TypeSafe console](https://console.typesafe.ai/), paste it
into the masked field, and choose **Test and enable**. The test sends only sample
text. On success, the app stores the key in macOS Keychain (or an owner-only
local file on other systems) and enables Jev without a restart. **Disable** stops
new requests while retaining the key; **Remove key** disables Jev and deletes
app-managed credentials. Paired remote devices cannot manage these credentials.
Usage is billed through the user's TypeSafe account. Ribbon Field works without
Jev, and setup explains which conversation excerpts are sent before opt-in.

Existing manual configuration remains supported. Create `jev.json` next
to the coordination database (normally `~/.local/state/agent-coord/jev.json`):

```json
{"enabled": true, "api_key_file": "/absolute/path/to/typesafe-key"}
```

The private file referenced by `api_key_file` contains only the API key. Changes
are picked up automatically within a few seconds. `AGENT_COORD_JEV_CONFIG` can select
another configuration file. Remove the configuration or set `enabled` to false
to stop new requests; saved classifications remain valid for their original reply.
Missing or invalid configuration leaves automatic classification disabled.
`TYPESAFE_API_KEY` alone does not enable this integration. Removing a key in
Settings never deletes a user-managed key file supplied by manual configuration.

The background worker sends a bounded excerpt of recent user messages, final
assistant replies, the original request, and the current checkpoint to
`api.typesafe.ai`. It excludes tool results, reasoning, progress messages, and
image data. It never resumes a conversation or starts another agent. The key
is read locally for each request and is not stored in SQLite or diagnostic logs.

The model is pinned to `jev-1.13.0`. Choice probabilities, confidence, model,
policy version, and sanitized failure codes are saved in `response_classifications`
and exposed as `response_classification` in thread JSON for tuning. Confidence
below 0.65 leaves the response in Attention as an unclassified Reply. Without a
usable classification, a current finished checkpoint backed by an implementation,
validation, or deployment stage can supply Done; other replies stay unclassified.
Real pending input and explicit checkpoint user actions always win.
Successful and uncertain results are cached per completed turn and checkpoint;
shared SQLite leases prevent duplicate web/desktop requests. Failed requests have
at most three attempts with backoff. Startup scans also classify existing open,
unhandled completed replies; user placement, read/handled receipts, and pins stay
unchanged. Terminal history uses a bounded local transcript tail; unavailable
history conservatively leaves the reply visible.

Choose **Enable notifications** in the sidebar to receive desktop alerts when a
terminal or browser agent finishes a turn. The browser asks for notification
permission only when you enable them. Keep a UI tab open; it can be in the
background. Click an alert to open its thread. Turn failures also produce an
alert; manually interrupted turns do not.

Orchestration results are quiet: internal agent turns, successful child results,
and an orchestrator waiting for its agents do not produce alerts or routine
Attention entries. The orchestrator's delivered result, dispatched conversations,
and direct user follow-ups retain normal visibility. A concrete user need still
surfaces, including an approval, question, or fresh checkpoint naming the user as
the next actor. Child needs share an Attention card under their available
orchestrator, with links to each actual request. Filtering out the parent keeps
the child's request accessible. Recoverable child failures go to the orchestrator;
failures surface when that owner cannot continue. Message delivery, completion
subscriptions, history, and saved thread placement are independent of this policy.

Codex command, file-change, and permission approval requests also produce a
**Codex is requesting approval** alert for browser sessions. Pending approvals
are checked on connection and reconnection; answered requests stay quiet, and
each request alerts once across tabs without changing its approval decision.
Live user-input questions also produce an **Answer needed** alert. Desktop and
phone delivery use the same policy, including cancellation of resolved requests.

When a snooze expires, the thread returns to **Now** and produces a **Snooze
ended** alert through the same desktop/browser notifications and enabled phone
push notifications. Each snooze alerts once per delivery channel; phone delivery
is independent of the desktop claim. Reading or reconnecting does not repeat an
alert. Re-snoozing, resuming, changing placement, or sending a message cancels a
pending reminder. The reminder remains in Now until you explicitly resume it.
Desktop/browser alerts require an open UI, and phone push requires the Mac app
or remote-access service to be running. If expiry happens while the runtime is
stopped, an overdue reminder is delivered when it next runs.

Completion events are saved in the shared database, so short turns and stream
reconnections do not lose them. Alerts respect the UI server's workspace scope
and exclude archived threads. Opening or reloading the UI starts with new
completions instead of replaying old ones. Tabs share notification delivery,
and a conversation you are already viewing in a focused tab stays quiet. Click
**Notifications on** to disable alerts. Terminal sessions must load the refreshed
plugin to record turn completions.

Agents prefer the plugin's `update_checkpoint` tool. It accepts changed fields
directly, for example `{"phase":"validation"}` or
`{"summary":"Tests passed.","next_step":null}`. Pass the registered
`session_id` announced by the hook when the MCP process has no inherited caller
identity. The bundled server uses Python's standard library and MCP over stdio;
Codex and Claude load the same implementation. The CLI is the fallback:

```bash
agent-coord checkpoint --json '{
  "title": "Database write performance",
  "phase": "investigation",
  "summary": "Compared two approaches; no implementation has started.",
  "next_step": {"actor": "user", "action": "Choose an approach."},
  "links": [{"kind": "document", "label": "Findings", "target": "docs/findings.md"}]
}'
agent-coord thread show --session-id <id>
agent-coord thread list
agent-coord thread update --session-id <id> --attention later
```

The first checkpoint requires `phase` and `summary`. Later updates preserve
every omitted field, including the next step. Set `next_step` to `null` to clear
it; otherwise send its `actor` and `action` together. Legacy CLI payloads with
`next_actor` and `next_action` remain supported as a pair; do not combine them
with `next_step`. Updates merge atomically and save complete history snapshots.
Links are added or updated without removing earlier links. Both tool and CLI
success responses contain only `{"status":"ok","checkpoint_id":123}`; use
`thread show` or the CLI's `--full` option for complete state and history.

Caller identity defaults to `AGENT_COORD_SESSION_ID`, then
`CODEX_THREAD_ID`. Use
`--session-id` to target another thread or when running outside an agent's
shell context. Hooks announce the session ID for that fallback.

Phases are `discussion`, `investigation`, `planning`, `orchestrating`, `implementation`,
`validation`, `deployment`, and `finished`. Keep the activity phase when answering questions or completing
investigations and plans. Use `finished` for delivered implementation or execution,
including requested validation/deployment. The UI exposes a separate `work_phase`
that retains the last activity for older inquiry checkpoints marked finished.
Next-step actors are `user`, `agent`, and `external`; `null` means no next step.
Stored snapshots retain `next_actor` and `next_action` for existing readers.
`--json -` reads stdin.
Hooks and browser launch instructions ask agents to save a factual checkpoint
before returning control, when the phase changes, or after a significant result.
Starting another turn marks an older checkpoint as potentially out of date.
Ending a turn or process never implies that the work is finished. Parking a
thread does not stop its agent or release its write scope.

Checkpoints accept an optional `title` (1–160 characters). Agents choose a
specific, short name at the first meaningful checkpoint, then omit it from
later checkpoints, including new assignments. Checkpoints can name only an
automatic title; established agent and user names stay unchanged. Use
`thread update --title` or the app's Rename action for a deliberate rename.
The separate checkpoint `summary` describes current/latest work: lead with a
concise sentence, then preserve useful progress detail. Update it at the start
of each assignment and describe completed/failed results as outcomes. The
title appears on the overview; the original request remains in the detail
view. Hook context includes the current title and its source (`auto`, `agent`,
or `user`). User renames take precedence and survive agent checkpoints and
restarts; an ignored agent title does not prevent the progress update from
being saved. Existing custom names are preserved during migration, while
known placeholder names and opening-prompt excerpts remain eligible for agent
naming.

Legacy group and repository associations remain available from the CLI:

```bash
agent-coord project create --name 'Anthropic migration'
agent-coord project list
agent-coord repository add --path /path/to/repository
agent-coord repository list
agent-coord thread update --session-id <id> --project <project-id>
agent-coord thread update --session-id <id> --repository <repository-id>
agent-coord thread update --session-id <id> --no-repository --no-project
agent-coord thread list --repository <repository-id> --no-project
```

Assigning, changing, or clearing a thread's group also updates all its descendants:
app agents created in orchestration mode and attached terminal delegates, recursively.
New descendants inherit their parent's group. Standalone dispatched agents remain
independent. Group changes preserve each thread's workspace, repository, and
Now/Later/Closed placement.

Thread data lives alongside coordination data in the local SQLite database.
`work_threads.project_cwd` stores immutable directory-project membership; saved
views and new groups store their project directory in `cwd`. Artifact workspace
identities and existing association IDs are preserved.
`thread_organization` stores nullable `repository_id` and `project_id`, referencing
`work_repositories` and `named_projects`. The additive migration detects actual
Git repositories for existing threads and leaves named projects unset. Cleared
associations remain cleared across restarts. Checkpoints, titles, attention, and
artifact references retain their history. Legacy `work_projects` and the old
`project_id` columns are retained as workspace identities for already-running
clients; they do not represent named projects.

Link responses expose `workspace_id` for their artifact context. Link input
accepts `workspace_id`; `project_id` remains a legacy alias for that workspace ID.
There is no provider column or metadata blob. Supported kinds are `pull_request`,
`document`, `issue`, `bead`, `branch`, and `other`. Multiple links of each type
are supported; exact duplicates update their label. Local document paths retain
their workspace context. Link targets remain owned by their respective systems;
Agent Coord does not poll or infer their current status.

Thread placement and checkpoint phases are independent of Beads. Linking an
issue does not import the backlog or change issue status. Delegation and atomic
handoff also work without an issue.

## Resume agents when work finishes

Agents can save a durable subscription, end their turn, and resume the same
conversation when another agent's turn or a managed command job finishes. The
Ribbon Field backend matches events and uses the ordinary inbox wake mechanism;
waiting does not require model turns. Keep the backend running for automatic
delivery. Saved subscriptions catch up after a backend restart.

```bash
# Resume after this agent's current (or most recent) turn finishes.
agent-coord subscribe --session <worker-session-id> \
  --continue 'Review the result and continue the original task.'

# Resume once all four captured turns have finished, including failures.
agent-coord subscribe --session <a> --session <b> --session <c> --session <d> \
  --mode all --timeout 1800 --continue 'Collect the four results.'

# Watch an exact earlier turn, even if a newer turn has already started.
agent-coord subscribe --turn <worker-session-id>:<turn-id>

# Start a command outside the agent process and resume on completion.
agent-coord job start --wake-self --timeout 600 \
  --continue 'Read the test output and fix any failures.' -- python3 -m unittest

# After triggering a workflow, watch its exact run ID in the same way.
agent-coord job start --wake-self \
  --continue 'Inspect the workflow result and report it.' -- \
  gh run watch <run-id> --repo <owner/repo> --exit-status
```

Subscriptions default to `--mode all`. `--mode any` resumes once when the first
target finishes; `--mode each` emits one event for each target. Targets are
one-shot and tied to captured turn IDs or stable job IDs. A finished turn is not
evidence that the agent's full assignment is complete. Terminal outcomes include
failure, interruption, cancellation, and timeout; the event includes result
references and the saved continuation. `--timeout` on `subscribe` bounds the
wait, while `--timeout` on `job start` terminates the managed command.

Inspect and cancel without polling model turns:

```bash
agent-coord subscription list
agent-coord subscription show <subscription-id>
agent-coord subscription cancel <subscription-id>
agent-coord job list
agent-coord job show <job-id>
agent-coord job logs <job-id> --offset 0 --limit 8000
agent-coord job cancel <job-id>
```

Commands are passed as an argument array without an implicit shell. Jobs retain
bounded output beside the coordination database and survive the launching
agent's exit. Use a stable `--job-id` or `--subscription-id` to retry a request
without creating duplicate work. Ownership follows the caller identity, with
explicit `--session-id` available as for other coordination commands. Managed
jobs and their logs are visible only through their owner's commands.

Events arriving during a turn wait for an eligible idle turn. Stop pauses
automatic wakes; Closed conversations retain events until explicitly reopened.
Cancellation suppresses an event that has not yet been delivered. Delivery
records prevent duplicate event scheduling; an uncertain provider submission is
not blindly retried. GitHub watching may poll in the command process, without
consuming model turns.

If a supervisor is forcibly terminated, its command may still be running and
the result is reported as `unknown`. The command is never relaunched
automatically from that state; inspect the result before starting replacement work.

## Wake ordinary idle Zellij sessions (optional compatibility)

An idle agent has no model turn in which a hook can run. Enable the optional
Zellij watcher from the agent's own pane to let Agent Coord start one safe turn
when an unread message arrives:

```bash
agent-coord wake-zellij enable --session-id <session-id>
agent-coord wake-zellij status
agent-coord wake-zellij disable --session-id <session-id>
```

`enable` reads `ZELLIJ_SESSION_NAME` and `ZELLIJ_PANE_ID`, records the target,
and starts a detached watcher. To enable it automatically at session start,
launch the client with:

```bash
AGENT_COORD_ZELLIJ_WAKE=1 claude
AGENT_COORD_ZELLIJ_WAKE=1 codex
```

The watcher polls only SQLite. When an actionable message is unread, it confirms
that the model turn is inactive and that the last recognizable Claude or Codex
prompt is empty. It then atomically reserves pending actionable messages and
sends one generic prompt to the registered pane with `zellij action`. Normal
`UserPromptSubmit` hooks deliver the message body and thread/reply metadata as
context. Informational and closure messages stay available in inbox history but
do not wake the agent. The watcher does not focus the pane, erase input, or mark
the message delivered. It verifies that its generic prompt was submitted before
recording a successful wake. If the exact Agent Coord prompt remains in the
input, a later poll retries only Enter without typing a duplicate; any other
typed input remains untouched. Failed or unverified attempts remain retryable
while their actionable messages are still undelivered. An active model turn,
missing pane, or failed Zellij command cannot corrupt user input or block the
sender. Inspect `wake-zellij status` for the last error and recent wake
attempts.

This command is only the compatibility wake adapter for ordinary sessions in
Zellij. Managed delegation wake-up uses the Agent Coord-owned PTY automatically.
Durable messaging, inbox reads, sending, managed delegation, and the local UI do
not require Zellij or tmux.

## Hook behavior

- `SessionStart` registers the process, attaches an inherited delegation,
  optionally starts compatibility Zellij wake, and delivers unread actionable
  messages.
- `UserPromptSubmit` records an active model turn and delivers unread actionable
  messages.
- `PreToolUse` permits unscoped solo writes. When concurrent work starts, it
  stops the newcomer, requests an incumbent scope, and checks declared lease
  mode, paths, and live conflicts. Validation leases deny structured writes.
- `PostToolUse` refreshes liveness and delivers unread actionable messages.
- `Stop` records an inactive turn plus `waiting` for unfinished solo or
  declared work, or `idle` otherwise. After a delegated terminal result it also
  captures token usage once.
- `SessionEnd` records an unfinished child delegation as failed, performs
  fallback token-usage capture, disables its registered wake transport, and
  marks the process offline without changing Beads.

The write guard covers Claude `Edit` and `Write` tools and Codex `apply_patch`.
It does not parse arbitrary shell commands. The skill instructs agents to
declare work before any implementation, including shell-based writes.
Validation lease mode expresses non-editing intent and protects structured
write tools; it is not a security boundary for arbitrary shell commands.

## Stalled app turns

The app runtime supervises its loaded conversations every 15 seconds, even when
no agent or browser tab is polling. Provider turn events own app turn liveness;
late prompt, tool, and Stop hooks cannot resurrect an interrupted turn or finish
a newer one. A provider read confirming the exact turn has ended repairs both
the live status and durable coordination state, including completion events and
failure reports.

After five minutes without streamed work, the watchdog reads provider state.
For a delegated Codex or Claude app agent with no outstanding tool, approval,
question, queued user input, Stop, or Close, it may request **one continuation of
the same turn**. That attempt is recorded before sending; an uncertain delivery
is never retried, including after restart. Acknowledgment or echoed input is not
proof of progress. Reasoning, output, and tool events are progress; metadata and
status polling are not. Other providers receive state reconciliation without
unsupported steering.

After ten minutes without progress, an unresolved turn becomes **stalled** in
the app and its creating orchestrator receives one durable `app_agent_stalled`
message for that turn. An idle orchestrator wakes through the normal inbox; a
busy orchestrator receives the alert at its next inbox delivery. Ordinary user
conversations show the stall in the app without receiving automatic input.
Pending user approvals and questions are waiting states. A quiet outstanding
tool gets a longer, twenty-minute threshold and an uncertainty alert, without
automatic steering or cancellation.

The watchdog never assumes silence proves termination, restarts a shared
provider, replays the original prompt, releases a write scope, or declares the
assignment complete. The orchestrator must confirm termination before handing
off unfinished writes. Monitoring is limited to conversations owned by this
running app backend; it is not a machine-wide process supervisor. Loading a
saved conversation starts a fresh observation grace period, while durable
recovery and notification receipts prevent duplicate actions.

## Develop and validate

The runtime supports Python 3.10 or later and has no third-party dependencies.

[GitHub Actions CI](../.github/workflows/ci.yml) runs on pull requests, pushes to
`main`, and manual **Run workflow** requests in GitHub's Actions tab. It validates
plugin packaging, compiles Python sources, and runs the Python and JavaScript
suites on Ubuntu with Python 3.10 and macOS with Python 3.12 (both with Node 22).
The full suite needs Node.js and ripgrep; CI installs both. Test jobs skip the
optional Web Push encryption test when its third-party packages are absent.

A separate macOS job builds an Apple Silicon standalone test DMG and verifies
the mounted image and relocated app with a fresh home directory, including its
bundled Python, cryptography, backend, and CLI. This uses ad hoc signing and
needs no Apple or provider credentials. It does not publish a release or run
foreground UI smoke tests. Intel packaging, supported macOS versions, native UI,
and signed/notarized downloads still need the
[release checks](../desktop/macos/README.md#build-a-standalone-disk-image).

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
node --test tests/*.js
python3 -m compileall -q plugins/agent-coord/scripts scripts tests

python3 scripts/plugin_tools.py validate

uv run --with pyyaml python \
  /Users/walle/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  plugins/agent-coord/skills/agent-coordination

uv run --with pyyaml python \
  /Users/walle/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  plugins/agent-coord/skills/manage-threads

uv run --with pyyaml python \
  /Users/walle/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  plugins/agent-coord/skills/dispatch

uv run --with pyyaml python \
  /Users/walle/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  plugins/agent-coord/skills/orchestrate

"${CLAUDE_BIN:-claude}" plugin validate --strict plugins/agent-coord
"${CLAUDE_BIN:-claude}" plugin validate --strict .
```

The plugin layout validator and refresh helpers live in `scripts/plugin_tools.py`
and use only Python's standard library. They check this project's manifests,
shared hooks, skill entry points, and runtime files; client installation and
Claude's strict validator remain separate checks. The skill validator path is a
local Codex development helper; use its installed equivalent on another machine.

## Refresh installed plugins after changes

Run this workflow after a change under `plugins/agent-coord/`, including a
change to a skill, hook, script, or manifest. Initial marketplace installation
is not part of the update loop. Do not hand-edit `marketplace.json`, Codex
configuration, or the cachebuster suffix.

First, run the validation commands in **Develop and validate**. Then run these
commands from the repository root:

```bash
set -euo pipefail

marketplace_file="$PWD/.agents/plugins/marketplace.json"
claude_bin="${CLAUDE_BIN:-$(command -v claude)}"

marketplace_name="$(
  python3 scripts/plugin_tools.py marketplace-name \
    --marketplace-path "$marketplace_file"
)"

python3 scripts/plugin_tools.py cachebuster

codex_install_json="$(python3 scripts/plugin_tools.py install-codex "$marketplace_name")"
printf '%s\n' "$codex_install_json"

"$claude_bin" plugin uninstall agent-coord@agent-coord --scope user
"$claude_bin" plugin install agent-coord@agent-coord --scope user

codex_cache="$(printf '%s' "$codex_install_json" | jq -r .installedPath)"
"$codex_cache/scripts/agent-coord" install-cli
```

If `claude` resolves to an older installation without the `plugin` command,
set `CLAUDE_BIN` to the current Claude Code executable and use it for every
Claude command in this workflow.

The cachebuster helper preserves the base Codex version and replaces its one
`+codex.<value>` suffix. Do not increase the base version only to refresh a
local cache. Claude uses its own manifest version, so uninstall and install it
again to replace the cached files even when that version did not change.
These helpers are repository-owned because Codex manages and may replace its
`.system` skill bundle; the former `plugin-creator` helpers are not present in
every Codex installation. Do not put project maintenance dependencies there.
`install-codex` runs `codex plugin add` and preserves previous cache directories
even if installation fails. Codex can remove an old version during an update;
keeping its files prevents missing-hook errors in sessions still using that
path. Start fresh sessions for live testing of the new version.

Verify that both installed copies contain the source files. The following
continues from the variables above and ignores generated Python bytecode:

```bash
codex_cache="$(printf '%s' "$codex_install_json" | jq -r .installedPath)"
claude_cache="$(
  "$claude_bin" plugin list --json | jq -r \
    '.[] | select(.id == "agent-coord@agent-coord") | .installPath'
)"

python3 scripts/plugin_tools.py verify "$codex_cache"
python3 scripts/plugin_tools.py verify "$claude_cache"

codex plugin list
"$claude_bin" plugin list --json
```

Any verification failure means the installed cache does not match the repository.
Stop and correct the selected marketplace or installation before live testing.
After verification, start new Codex and Claude sessions. Existing sessions keep
the skills and hooks that they loaded at startup.
