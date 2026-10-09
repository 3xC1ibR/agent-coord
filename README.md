# Ribbon Field

Ribbon Field is the app for managing coding-agent conversations and work across
projects. Its CLI, plugins, and coordination internals retain the Agent Coord
name and existing commands.

The [macOS app](desktop/macos/README.md) can be packaged as a standalone
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
- How can a session delegate ready Beads work to a new interactive Codex or
  Claude Code session without Zellij or tmux?
- How can an operator inspect the parent, its children, and each child's current
  status and recent output?

## Design

Agent Coord is a conflict detector and communication substrate. A sole working
session does not need a Beads issue or write scope. When a second session tries
to write, the hook stops the newcomer, sends the unscoped incumbent an
actionable scope request, and requires concurrent writers to declare scopes.

Beads is optional for direct work. When `begin-work` includes `--bead`, Agent
Coord verifies that the issue is claimed and has the `in_progress` status. It
does not claim, update, or close issues. Delegation and atomic handoff continue
to require Bead identity because those workflows preserve durable ownership.

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
declarations receive the same overlap protection.

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
rejected. Replying does not acknowledge the original message; use `ack`
independently. Starting new work or asking a follow-up question uses `send`
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

Use `handoff` when the current owner has a Bead-backed declaration, has reached
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
assert the expected declaration. Use `--target-bead` only for a claimed
`in_progress` issue; the CLI revalidates a changed target immediately before the
transaction. Beads and SQLite remain separate stores, so their updates cannot be
one cross-database transaction.

## Create persistent app agents

An **agent** is an independent Ribbon Field conversation. A **specialist** is
an agent focused on a subject or responsibility; a **dispatcher** routes work.
Provider-native subagents remain a separate concept. App conversations retain
context between assignments and accept direct user follow-ups.

For ongoing routing, use the [Dispatch skill](plugins/agent-coord/skills/dispatch/SKILL.md):
it reuses relevant app agents, creates new ones when needed, and directs answers
and follow-ups to their own conversations. Use the
[Orchestrate skill](plugins/agent-coord/skills/orchestrate/SKILL.md) when the
initiating conversation should own the full outcome, coordinate contributions,
and deliver the integrated result. Both support normal discovery in Codex and
Claude, and reuse the same agents for later discoveries, questions, and revisions.
Workflow findings collection is off by default; enable it only with an explicit
request for that dispatch or orchestration conversation.
Normal task tracking, checkpoints, and result reporting remain in use.

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

Each keyword must match the title, original request, latest checkpoint, or
artifact labels/paths. Results rank relevance first and meaningful work recency
second, and include the stable identity, app link, placement, provider, summary,
and wake pause state. `last_work_at` uses turns, completions, and new checkpoints;
moving/renaming a thread or refreshing an identical checkpoint does not make its
context fresh. Search globally by default, or select `--repository`, `--project`,
`--no-repository`, `--no-project`, or an explicit `--cwd`. Omit `--app-only` to
also find terminal conversation context. Search does not scan full transcripts.
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
for an open, ready, and unclaimed Beads issue. Codex remains the default child
for backward compatibility; pass `--client claude` to select Claude Code. The
default `managed-pty` runtime does not require Zellij or tmux. First, preview and
validate the launch without changing SQLite or starting a process:

```bash
agent-coord delegate \
  --from-session <parent-session-id> \
  --cwd /absolute/path/to/repository \
  --bead <ready-bead-id> \
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
reported the failure. Create an implementation issue for remediation and a new
validation issue for the next attempt. Agent Coord rejects `--yolo` with a
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

Before launch, Agent Coord verifies the Git repository, `bd`, the selected
client's login, issue readiness, live write-scope conflicts, and active
delegation state. The Zellij adapter additionally verifies Zellij. Agent Coord
then creates one durable delegation record and starts the child with an
inherited delegation ID and explicit client identity. Reviewed mode adds only
the Agent Coord database directory as an extra allowed root, so child CLI calls
can update lifecycle state outside the repository. The child SessionStart hook
verifies the client and attaches the new session. The generated instructions
require the child to:

1. Read repository instructions and run `bd prime`.
2. Verify and claim the ready issue.
3. Declare the exact scopes with the selected lease mode.
4. Implement and run focused checks, or validate without editing.
5. Apply repository validation, changelog, and git-authority rules.
6. Record a compact completed or failed result before exit.

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

Choose **Schedule…** beside Send to save a one-time text prompt. Set its
workspace, date and time (with your browser's time zone shown), model, reasoning
effort, and permissions. Each run creates a new thread, so include the context
the agent needs in the prompt. **Full access** uses the same unrestricted,
no-approval settings as session YOLO mode; workspace access can pause for approval.
Saving a schedule does not start a model turn. Attachments and recurrence are
not supported.

Open **Scheduled prompts** in the sidebar to edit or cancel a pending run and
open its result thread. The saved model and reasoning are checked again at
launch; an unavailable setting fails visibly instead of choosing a substitute.
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
Open **New session**, then choose a model from the **Codex** or **Claude Code**
groups in the composer’s **Model** picker. The provider session starts when you send.
Install and authenticate the selected CLI first (`codex login` or
`claude auth login`). Existing conversations retain their provider. Codex sessions use
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
terminal Claude conversations and forking Claude threads are not yet supported.

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
The slash menu completes model names and supported effort levels. In the UI,
`/fork` opens a new thread from the current idle Codex conversation. `/close`
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
Stopping the UI server stops its Codex app-server and Claude processes; saved conversations can be
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
an optional `--tailscale-port 8443`. See [Remote access](docs/remote-access.md)
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

The result creates a durable message in the parent inbox. If the child process
exits before it reports a result, the SessionEnd hook records the delegation as
failed and sends that failure to the parent. A second active delegation for the
same repository and Beads issue is rejected. The parent can use `delegation
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

**Open folder** selects where new sessions start. First use offers a folder
picker; ordinary folders, repository subdirectories, and linked worktrees work.
The sidebar shows the effective folder and recent choices. Opening a folder
shows a temporary All work overview for its repository, preserving saved views
and all existing conversation directories and drafts. Ordinary folders use All
work. Browser folder paths refer to the host running Ribbon Field.
Each window keeps its own selection; macOS restores folder selections by window
slot across launches. Explicit `ui --cwd` supplies the initial folder and still
limits permitted directories. Without a selected or view-implied folder, New
session asks for one instead of using the server process directory.

The UI calls optional thread initiatives **Groups**. Existing `project` CLI
commands, API fields, deep links, and saved filter keys remain compatible.

**Views** are named, saved filters displayed as tabs above the overview.
Start with **All work**, choose repository, group, phase, Show,
and search filters, choose a grouping, then click **＋ View** to save them.
Each tab shows a count of threads that need you and a green dot for unread
completed results. Counts follow that tab's current filters, including pinned
threads that need you; Later threads do not add to the attention count.

Use **Ctrl + Shift + Left/Right** to cycle views outside text fields. When a
view tab has focus, Left/Right, Home, and End navigate the tabs. Switching
opens the scoped overview and sidebar, preserving each view's filters,
grouping, and scroll position. Conversation drafts
stay with their threads. New sessions inherit the view’s group and repository.
A selected folder within that repository keeps its exact subdirectory or linked
worktree; otherwise the repository root supplies the default. Views without a
repository use the window’s selected folder. Existing drafts retain their folder
and group when switching views, and focused conversations do not change defaults.

Changing filters or grouping in a named view saves them automatically, including
search. **All work** keeps temporary filters local to each window; **Reset filters**
clears them. If a save fails, **Retry saving** tries again and **Reset filters**
restores the saved definition without overwriting another window's changes.
The **•••** menu renames, duplicates, reorders, or deletes a view. Duplicating
uses its saved definition. Deleting a view leaves all its threads intact.
Views can overlap; a conversation's status and read state are shared wherever
it appears. Definitions and tab order are stored in the coordination database
and shared by browser and native app windows. All work filters and scroll
positions stay local to each window. A concurrent edit requires resetting
before overwriting another window's saved changes.

The UI home page keeps durable work threads, with **Needs you** first and a
separate **Later** section. Group by phase, repository, group, or none. Filter
by repository and group independently, including **No repository** and
**No group**; phase and **Needs you** filters can be combined with them.
Every card includes its assigned repository/group, title, phase, runtime state,
latest checkpoint, and next action. Closed threads have their own view.
Selecting a thread shows its original
request, checkpoint history, related links, and conversation. You can edit a
checkpoint, add/remove links, rename, park, reopen, or close a thread.

Repository and group are both optional. A repository identifies a Git codebase;
a group is a named initiative that can span repositories. A thread can have
either association, both, or neither. Use **New group** in the sidebar or
overview to create a group without starting a session. Open an existing thread
and choose **Add to group** (or **Change group**) to select an existing
group, create a new one, or choose **No group** to detach it. The same dialog
lets you assign or clear its repository independently. **New session** defaults
to the group selected in the filter; All groups and No group start unassigned.
Creating a group does not require a repository. New sessions detect a repository
from their workspace by default; selecting **No repository** explicitly clears it.
Changing either association never changes the working directory or file access.

These features work without Beads or a Git remote. A normal folder remains a
workspace. Git worktrees group under their main repository while each session
retains its actual working directory. Run `agent-coord ui` without `--cwd` to
see all workspaces together, or use `--cwd /opt/projects` to show workspaces within
that directory. Workspace scope always follows the actual working directory,
independently of the thread's organizational associations. Registered terminal conversations are also captured;
delegated sessions appear under their parent instead of filling the main list.
A closed Codex terminal conversation can continue in the browser. Active
terminal conversations stay with their existing client, and Claude conversations
continue in Claude Code. Existing terminal sessions begin capturing requests
when they load the refreshed plugin; earlier prompts are not reconstructed.

Choose **Fork** on an idle Codex thread to open a separate conversation with
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
earlier turn and launching terminal forks are not included.

The overview keeps one **Attention** queue above fixed work stages:
**New → Investigating → Planning → Orchestrating → Implementing → Validating →
Deploying → Done**. Discussion and debugging belong in Investigating. Answering
an investigation leaves it in Investigating; Done means the requested change or
execution was delivered. Orchestrating identifies ongoing routing and coordination of specialist agents, including while they implement or deploy. Reading a response does not change its underlying stage.
Empty stages remain visible. Within each stage, working agents come first, then
cards sort by most recent activity; ties keep a stable order. The animated green
border indicates a working agent. Idle cards keep their full detail for six hours,
then show a shorter summary with fewer secondary details. After 24 hours they show
the title, group/repository, and age, with unread indicators and controls retained.
Older cards use a quieter background without dimming their titles. Running, pinned,
and required-action cards keep their details; pins do not override activity sorting.
New activity restores the full card, and hover previews or opening the thread
retain access to its full context. The Attention queue does not decay.

Attention shows the reason each thread surfaced. With Jev enabled, the order is
**Blocked**, **Review requested**, **Update** about execution, **Findings ready**,
then routine **✓ Done · No action needed**. Required input and failed turns take
precedence. Reading an update, findings, or success clears its notification and
returns the thread to its stage. Required actions stay until resolved. Uncertain
or unavailable classifications remain visible as Reply and can be marked handled.
**Mark reviewed** acknowledges a classified review; explicit checkpoint actions
and active approvals require an answer or resolution. A follow-up consumes the
previous response only when the new turn starts.

A delivered thread stays quietly visible in Done until you close it or move it.
Pins order cards within a stage and never hide attention or override its priority.
Search, saved views, repository/group filters, and optional association grouping
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
[Choice API](https://docs.typesafe.ai/primitives/choice). Create `jev.json` next
to the coordination database (normally `~/.local/state/agent-coord/jev.json`):

```json
{"enabled": true, "api_key_file": "/absolute/path/to/typesafe-key"}
```

The file referenced by `api_key_file` contains only the API key. Restart the web
server and desktop app after configuring it. `AGENT_COORD_JEV_CONFIG` can select
another configuration file. Remove the configuration or set `enabled` to false
to stop new requests; saved classifications remain valid for their original reply.
Missing or invalid configuration leaves automatic classification disabled.

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

Codex command, file-change, and permission approval requests also produce a
**Codex is requesting approval** alert for browser sessions. Pending approvals
are checked on connection and reconnection; answered requests stay quiet, and
each request alerts once across tabs without changing its approval decision.

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

Agents maintain checkpoints with:

```bash
agent-coord checkpoint --session-id <id> --json '{
  "title": "Database write performance",
  "phase": "investigation",
  "summary": "Compared two approaches; no implementation has started.",
  "next_action": "Choose an approach.",
  "next_actor": "user",
  "links": [{"kind": "document", "label": "Findings", "target": "docs/findings.md"}]
}'
agent-coord thread show --session-id <id>
agent-coord thread list
agent-coord thread update --session-id <id> --attention later
```

Phases are `discussion`, `investigation`, `planning`, `orchestrating`, `implementation`,
`validation`, `deployment`, and `finished`. Keep the activity phase when answering questions or completing
investigations and plans. Use `finished` for delivered implementation or execution,
including requested validation/deployment. The UI exposes a separate `work_phase`
that retains the last activity for older inquiry checkpoints marked finished.
Next actors are `user`, `agent`,
`external`, and `nobody` (with an empty next action). `--json -` reads stdin.
Hooks and browser launch instructions ask agents to save a factual checkpoint
before returning control, when the phase changes, or after a significant result.
Starting another turn marks an older checkpoint as potentially out of date.
Ending a turn or process never implies that the work is finished. Parking a
thread does not stop its agent or release its write scope.

Checkpoints accept an optional `title` (1–160 characters). Agents choose a
specific, short name at the first meaningful checkpoint, then omit it from
later checkpoints unless the thread's purpose changes substantially. The
title appears on the overview; the original request remains in the detail
view. Hook context includes the current title and its source (`auto`, `agent`,
or `user`). User renames take precedence and survive agent checkpoints and
restarts; an ignored agent title does not prevent the progress update from
being saved. Existing custom names are preserved during migration, while
known placeholder names and opening-prompt excerpts remain eligible for agent
naming.

Groups and repositories can also be managed from the CLI:

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

Thread data lives alongside coordination data in the local SQLite database.
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
issue does not import the backlog or change issue status. Legacy delegation and
atomic handoff still require a Bead as described above.

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

## Develop and validate

The runtime supports Python 3.10 or later and has no third-party dependencies.

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
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
