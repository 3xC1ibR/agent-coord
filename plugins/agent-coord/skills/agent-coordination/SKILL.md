---
name: agent-coordination
description: Create and reuse persistent Ribbon Field app agents for direct user conversations and later requests. Coordinate Codex and Claude sessions through messaging, conflict detection, and scoped managed terminal delegates. Use when creating or routing work to an app agent, sending or receiving agent messages, checking file conflicts, before implementation when another session may be active, or delegating ready Beads work to a terminal worker.
---

# Agent Coordination

## Choose the agent mechanism

- **App agents** are persistent, independently addressable Ribbon Field
  conversations. For a conversation the user can address directly and return to
  later, search existing app agents with `thread search --app-only`, reuse one
  with `send`, or create one with `thread create`.
- **Provider-native subagents** use the provider's tools and follow its context
  and lifecycle rules. Use them only when permitted by the active instructions,
  and follow those instructions on when and how to delegate. Creating one does
  not fulfill a request for a Ribbon Field app conversation.
- **Terminal delegates** use Agent Coord `delegate` for scoped terminal work.
  They report to their parent and stop after a completed or failed delegation.

## Use the coordination CLI

Use `agent-coord` on PATH. Ribbon Field and managed workers add the bundled
scripts directory to PATH; Claude's SessionStart hook also exports it for Bash.
For terminal setup, run the installed plugin's `scripts/agent-coord install-cli`
once to install a stable launcher in `~/.local/bin` (which must be on PATH).
Rerun after plugin refresh to retarget the launcher. If unavailable, use the
absolute CLI path announced by the hook; the plugin root is two directories
above this `SKILL.md`.

Routine commands infer the caller from `AGENT_COORD_SESSION_ID`, then
`CODEX_THREAD_ID`. Explicit `--session-id` / `--from-session` override it.
Claude browser sessions receive their identity at launch; terminal sessions
receive it through `CLAUDE_ENV_FILE`. If context is unavailable, use the session
ID announced by the hook. Never guess an identity from cwd or another session.
Recipients and other threads remain explicit.

Omit `--db` normally: resolution is `--db`, `AGENT_COORD_DB`, then
`${XDG_STATE_HOME:-~/.local/state}/agent-coord/state.sqlite3`. Follow an explicit
hook-provided database override when the shell cannot inherit that context.
Routine mutations return compact JSON receipts; add `--full` for the complete
result, or use `status` / `thread show` to inspect saved state.

Agent Coord stores ephemeral session activity, write scopes, durable local
messages, and optional local wake state. Beads is optional for direct work. If
the repository uses Beads, it remains the durable task source of truth; Agent
Coord does not claim or update issues.

For a session dedicated to reviewing or organizing the user's open threads,
use the [Thread Manager skill](../manage-threads/SKILL.md). It discovers threads
across workspaces from any directory and uses the same installed CLI and database.

## Keep a work-thread checkpoint

Threads preserve the user's open conversations across stopped processes and
repositories. They do not require Beads. A thread's Now/Later/Closed placement
is the user's choice; checkpoints and activity do not change placement. Routing
new actionable work to a closed app conversation automatically moves it into Now.

Before returning control to the user, at a phase change, or after a significant
result, save a short factual checkpoint with the bundled CLI:

```bash
agent-coord checkpoint --json '{
  "title": "Database write performance",
  "phase": "investigation",
  "summary": "Compared the two approaches and documented the findings.",
  "next_action": "",
  "next_actor": "nobody",
  "links": [{"kind": "document", "label": "Findings", "target": "docs/findings.md"}]
}'
```

Use phases `discussion`, `investigation`, `planning`, `orchestrating`, `implementation`,
`validation`, `deployment`, or `finished`. Describe what was established or
changed in one or two sentences. Use `orchestrating` for ongoing routing and coordination of specialist agents,
including while a specialist implements or deploys. Coordination does not itself
require a user action. Distinguish proposals, implemented changes,
validation results, and deployment. Record the next required action and its
actual owner (`user`, `agent`, or `external`). Set `next_actor: "user"` only when
progress or completion requires a specific user answer, approval, decision, or
action, and describe that requirement in `next_action`. Ending an agent turn
does not create a required user action.

Keep optional advice, invitations to continue, and nonblocking reminders in
`summary`; do not turn them into user next steps. Keep the phase of the underlying activity: an answered investigation stays
`investigation`, a completed plan stays `planning`, and a status question during
deployment stays `deployment`. Use `finished` only after delivering the requested
implementation or execution, including validation/deployment if requested, with
nothing remaining. Use an empty `next_action` and `next_actor: "nobody"` when
there is no required next step. Required user review belongs in `validation`
with `next_actor: "user"`. If work remains, keep its actual phase and assign
any required next step to its actual owner; do not invent follow-up tasks.
Skip unchanged checkpoints. The command preserves history and the original
request; it does not mark a Bead complete or close the thread.

At the first meaningful checkpoint, include an optional `title`: a specific
3–6 word name that makes the thread recognizable in the overview. Use the
thread's purpose, not a progress report or a copy of the opening message.
The saved context includes `title` and `title_source` (`auto`, `agent`, or
`user`). Replace an `auto` title when you understand the work. Omit `title`
on later checkpoints to keep the name stable, unless the purpose changes
substantially. A user-chosen name takes precedence: agent checkpoints still
save progress but cannot replace a title whose source is `user`. The original
request remains unchanged. Titles accept 1–160 characters; the word count is
writing guidance.

Links accept `kind`, `label`, and `target`, with an optional `workspace_id` for a
different artifact workspace. The link field `project_id` is a legacy alias for
that workspace ID, not a named project ID. Kinds are `pull_request`, `document`, `issue`, `bead`, `branch`,
and `other`. Use exact references. Document paths resolve against the session's
workspace. Links are added or updated without removing earlier associations.
For complex text, `--json -` reads the object from stdin. `thread show
--session-id <id>` displays the original request, checkpoint history, and links.
Session-start and prompt hooks provide the latest saved checkpoint as context.

Repository and project are independent, optional thread associations. The
workspace is the actual working directory; changing associations does not move
files or change execution scope. Git repositories are detected for new threads;
plain folders remain workspaces, and named projects are assigned explicitly.
Use `project create --name <name>` / `project list` and
`repository add --path <path>` / `repository list` to obtain IDs. At the user's
request, assign them with `thread update --session-id <id> --project <id>` or
`--repository <id>`; clear either with `--no-project` or `--no-repository`.
The UI has independent filters and grouping, including No project and No repository.

Move a thread only at the user's request, using the UI or `thread update
--session-id <id> --attention now|later|archived`. Parking preserves conversation
state and does not interrupt a running turn or release a file scope. Call
`end-work` independently when you no longer own that scope.
The CLI's legacy `archived` placement appears as **Closed** in the UI. To close
a session, use `thread close --session-id <id>`; the owning Ribbon Field runtime
performs the same lifecycle action as **Close thread** / **Stop and close**.
For your own thread, save the checkpoint and use `--after-turn` so your final
response arrives before closure. New input cancels the pending request. Inspect
with `thread close-status`, or cancel a queued request with `thread cancel-close`
(both take `--session-id`). A `queued` receipt is not confirmed closure; active
sessions need the updated app runtime. Do not search for ports or private HTTP
routes. See the [thread-management workflow](references/thread-management.md).
Closing releases the scope and keeps conversation history, checkpoints, links,
and settings; **Reopen** allows continuing later.
Closing does not imply task completion. Terminal shutdown requires a verified
Codex transcript owner and never targets a remembered Zellij pane or shared
app-server. When that cannot be verified, exit the terminal session first.

## Before implementation

1. Inspect other working sessions with `agent-coord list --relevant --cwd
   <repo>`. If no other session is doing work, proceed without a Beads issue or
   scope. The write hook records the solo session as implementing.
2. When another session is doing work, declare the smallest useful scope:

   ```bash
   agent-coord begin-work \
     --scope '<file-or-directory-glob>' \
     --lease-mode write
   ```

   Repeat `--scope` for distinct areas. Add `--bead <bead-id>` only when the
   work has a claimed, `in_progress` Beads issue. Direct scope declarations do
   not require Beads.
3. If an unscoped session is already working, the newcomer write stops and
   sends that session an actionable scope request. Wait for the incumbent to
   declare its scope, then declare a disjoint scope or resolve the overlap by
   message before editing.

   Use `--lease-mode validation` only for an exclusive, stable validation
   reservation that must not edit through structured write tools. Ordinary
   `--activity validating` on a write lease may still make fixes.

The write hooks permit an unscoped write only while no other session requires
coordination. With concurrent work, they stop unscoped newcomers, request an
incumbent scope, deny edits outside declared scopes, and deny overlaps.

## Inspect and communicate

- List relevant sessions with `agent-coord list --relevant --cwd <repo>`.
- Recheck the current declaration with `agent-coord status`.
- Check overlap with `agent-coord conflicts`.
- Send actionable work with `agent-coord send --session
  <peer-id> --classification action_required --reply-required '<message>'`.
- Send to the one live owner of a bead with `agent-coord send --bead <bead-id> --classification action_required --reply-required '<message>'`.
- Every `send` must explicitly choose `--reply-required` or
  `--no-reply-required`; omission is an error before a message is created.
- Answer a requested reply with `agent-coord reply --message-id <id> '<response>'`.
  It derives the recipient and thread from that message, records exactly which
  request it answers, and sets `reply_required=false`. The response still enters
  hook context and wakes an idle recipient. Do not use generic `send` for a
  confirmation or answer, and do not reply to a message that requests no reply.
- Continue a conversation by passing its `--thread-id`. Threads permit the same
  two sessions in either direction and reject unrelated participants.
- Use `--no-reply-required` when work is actionable but a conversational reply
  is unnecessary, and for informational and closure messages.
- Read the compact unacknowledged inbox with `agent-coord inbox --unread`. Use `--all` only for complete history.
- Block for a peer handoff without polling with
  `agent-coord inbox --wait`. It returns immediately if a
  message is undelivered, otherwise it polls the local store and
  refreshes session liveness until a message arrives, waiting indefinitely.
  Add `--timeout <seconds>` to bound the wait; on timeout it exits with
  status `5`. `--timeout` alone (without `--wait`) is rejected, and `--wait`
  cannot be combined with `--all`.
- Acknowledge a delivered message with `agent-coord ack --message-id <message-id>`, or acknowledge the current unread set with
  `--all-unread`. Acknowledgement is a silent transport update and never sends
  a conversational receipt.

## Atomic handoff and thread closure

Atomic handoff requires a Bead-backed declaration. Use one transactional
handoff instead of releasing, notifying, and asking the recipient to reacquire
the same paths:

```bash
agent-coord handoff \
  --to-session <idle-recipient-session-id> \
  --patch-label <patch-name> \
  --validation-boundary '<state already validated>' \
  --validation-responsibility '<checks the recipient owns>' \
  --mode validation
```

The recipient must be registered, online, idle, and in the same repository.
The command transfers the complete declaration, stores the patch and validation
boundary, and sends one actionable notification with `reply_required=false` in
the same SQLite transaction. Partial scope handoffs are rejected because glob
subtraction is unsafe. Use `--mode write` for continued implementation and
`--mode validation` for an exclusive non-editing reservation.

Close a finished coordination thread with one terminal message:

```bash
agent-coord send \
  --session <peer-id> \
  --classification closure \
  --no-reply-required \
  --thread-id <thread-id> \
  'No further coordination action is needed.'
```

Closure is idempotent and suppresses older pending actionable messages in that
thread. It does not require a reply or wake the recipient. A later explicit
`action_required` message on the same thread reopens it for a material change.

## Wake an ordinary idle Zellij session (compatibility)

An agent at an idle prompt cannot receive hook context until a new turn starts.
When automatic wake-up is wanted, run this once from that agent's Zellij pane:

```bash
agent-coord wake-zellij enable
```

Alternatively, start the client with `AGENT_COORD_ZELLIJ_WAKE=1` so its
`SessionStart` hook enables the watcher. Use `wake-zellij status` to inspect the
registered pane, watcher PID, last error, and recent attempts. Use
`wake-zellij disable` to stop wake-up for the session.

The watcher sends one generic prompt only for undelivered `action_required`
messages, when the model turn is inactive and the visible Claude or Codex prompt
has no typed input. It does not deliver or acknowledge messages itself; the
resulting prompt hook performs normal inbox delivery. Informational and closure
messages remain in history without waking the agent. Do not manually inject
input into another pane as a substitute for this guard.

Hook-delivered messages include their ID, thread and `reply_required` value. Reply
conversationally only when `reply_required=true`, using `reply --message-id`, and use
transport acknowledgement independently. Never reply to or acknowledge an
acknowledgement; acknowledgements do not create messages.

## Wake idle app conversations

While Ribbon Field is running, open and closed app conversations automatically start a
turn for undelivered `action_required` messages. This works for both Codex and
Claude, including conversations not currently displayed. Pending messages are
combined into one generic inbox prompt; normal hooks deliver the durable bodies
and thread metadata. Informational and closure messages do not wake recipients.

The dispatcher waits for running turns and gives queued user prompts priority.
Now/Later placement and snoozes are preserved. Saving actionable work to a closed
app specialist moves it into Now immediately, including while busy or offline.
Eligible wake restores its provider context; older work queued before Close also
reopens the conversation when dispatched. Closing alone does not cancel pending
messages. A pending Close still blocks wake. Stop, failed turns, and uncertain submissions
pause inbox wake until the user sends another message in that conversation.
Claims persist across app restarts so an uncertain turn submission is not
automatically retried. Reopening visibility does not clear these execution guards.
Messages remain in the inbox for normal delivery. Informational and closure
messages do not reopen closed conversations.

## Create and reuse app agents

An **app agent** is an independent app conversation with its own context. A
**specialist** is an agent focused on a subject or responsibility; a
**dispatcher** routes work. Reserve **subagent** for provider-native subagents.
The existing `delegate` command creates a terminal worker for a scoped task.

Prefer an existing relevant app conversation, including Closed. Discover with
`thread search 'topic keywords' --app-only --limit 10` without `--cwd` unless
the user selected a workspace. Search title, original request, latest checkpoint,
and artifacts; each keyword must match. Follow `next_cursor` with `--cursor`
using the same query and filters. Pages are bounded (maximum 50), ordered by
relevance then meaningful-work recency, and include identity, stable URL,
placement, provider, summary, `last_work_at`, and wake pause state.

Use `thread show --session-id <id>` on promising matches. Inspect current code
and ask older specialists to recheck assumptions against it; age is evidence
about context freshness, not an automatic reason to discard that identity.
Renaming, moving, or refreshing an unchanged checkpoint does not count as fresh
work. Search pages are live; repeat discovery when the inventory changes.
Use global `thread list` for general open-thread management, not a full dump for
each routing decision. Inspect settings or obtain a stable link separately with:

```bash
agent-coord thread settings --session-id <agent-id>
agent-coord ui link --thread <agent-id>
```

When a new conversation is needed, ask the running Ribbon Field app to create
it. Creation needs no Bead or scope; editing assignments still follow the
repository's work rules. Model discovery and creation use the app's provider:

```bash
agent-coord thread models --client codex --cwd /absolute/workspace --wait 30
agent-coord thread create \
  --cwd /absolute/workspace --client codex --name 'Release specialist' \
  --model <advertised-model> --effort <supported-level> \
  --request-id <unique-request-key> --wait 30 \
  'The exact user request, including its scope and constraints.'
```

Use `--client claude` for Claude Code. Model and effort are optional; select
them when the user authorized that choice. Add `--yolo` only with explicit
authorization for full machine access without approval prompts. Defaults do
not inherit the dispatcher's YOLO mode. Creation does not bypass hook trust.
Pass `-` as the prompt to read stdin without shell escaping.

A `completed` receipt contains `thread_id` and `result`: session identity,
effective settings, stable URL, and initial coordination message ID. It confirms
creation and inbox queueing, not completion of the agent's task. The exact
request is saved as the original request, then sent as actionable work with no
reply obligation to the dispatcher. The agent answers the user in its own chat.

Reuse the same `--request-id` and arguments when retrying a timed-out command;
do not create another agent. Inspect or cancel a pending request with:

```bash
agent-coord thread request-status --request-id <key> --wait 30
agent-coord thread cancel-request --request-id <key>
```

`queued` requires an updated running app in the target workspace. `running` is
not confirmation. `uncertain` means the provider may have accepted the request;
inspect the returned identity and thread inventory before creating anything
else. Uncertain operations are never replayed automatically. Only queued
requests can be cancelled.

Change an existing app agent's settings through the app-owned path:

```bash
agent-coord thread settings --session-id <agent-id> \
  --model <advertised-model> --effort <supported-level> \
  --request-id <unique-settings-key> --wait 30
```

Add `--yolo` or `--default-permissions` to change permissions. Changes wait for
idle and queued user follow-ups, reject closed/pending-close conversations,
and fail if settings changed after queueing. Confirm completion before sending
work that requires those settings. A session active in another app runtime
must be handled there.

Route later requests with `send --session <agent-id> --classification
action_required --no-reply-required`. Request a reply only when coordination
requires one. Messages can reach busy agents through tool hooks; automatic
wake waits for idle and prioritizes queued user input. Save checkpoints and
release scopes after assignments; completing work does not close the chat.

Closing preserves identity, context, and settings. The same ordinary `send`
wakes a closed app specialist and automatically returns it to Now, restoring
its direct user composer. No reopen flag or separate reopen command is needed.
Stop, failure, and uncertain wake attempts may also require direct user input
to resume automatic wake. An updated running app services pending messages;
offline queueing does not launch the app. Terminal `delegate` behavior is unchanged.

## Delegate work to a managed terminal worker

Use `delegate` when a registered parent session must create a separate Codex or
Claude Code worker. The work must have one open and ready Beads issue and
explicit repository scopes. Codex is the default child; pass `--client claude`
to launch Claude Code.

Run a dry-run preview first:

```bash
agent-coord delegate \
  --cwd /absolute/repository/path \
  --bead <ready-bead-id> \
  --scope 'src/**' \
  --scope 'tests/test_feature.py' \
  --client <codex-or-claude> \
  --name <worker-name> \
  --model <client-model> \
  --effort <level> \
  --lease-mode write \
  --dry-run \
  'Implement the specific requested change and run the focused tests.'
```

Remove `--dry-run` to launch the worker. The default `managed-pty` runtime
starts a detached Agent Coord supervisor, gives the child a controlling PTY,
captures bounded output, and keeps the interactive client alive at its prompt
between turns while the delegation is unfinished. After a completed or failed
result, the supervisor stops wake-up and shuts down the child. It does not
require Zellij or tmux and does not use the parent's
PTY. Use `--runtime zellij --zellij-session <name>` only when the user requests
the compatibility pane adapter; `--floating` is optional for that runtime.

`--model` and `--effort` select optional child settings. Effort maps to
`model_reasoning_effort` for Codex and `--effort` for Claude Code;
`--reasoning-effort` remains a compatibility alias. Both values are stored with
the durable delegation. Do not guess either setting when the user did not
request it. Let the selected client validate the combination.

Use `--lease-mode validation` for an independent validator. The delegated
scopes become an exclusive stability reservation. The generated prompt makes
the child declare a validation lease, prohibits repository edits and
remediation, and requires a compact verdict. A failed check is a completed
validation result when every requested check ran and the child reported the
failure. Use a new implementation issue for remediation and a new validation
issue for the next attempt. Validation-only delegation rejects `--yolo`.

The reviewed launch opens the selected interactive TUI in the owned PTY. Codex
uses `--approve-for-me`; Claude Code uses safety-classified auto permission mode.
Use `--yolo` only when the user explicitly authorizes bypassing the selected
client's permission safeguards. Never infer that permission from a request to
delegate work.

Codex requires persisted trust before it runs hooks. Review and trust the
installed Agent Coord hook before delegation. If that is not possible,
`--bypass-hook-trust` is an explicit escape hatch for a repository whose
complete enabled hook set was reviewed. This Codex-only flag does not enable yolo mode,
but it runs every enabled hook without persisted trust for that invocation. Do
not add it by default or infer permission to use it from a request to delegate
work. Claude Code rejects this flag combination and can show its normal
repository trust prompt in a newly opened pane.

The launcher rejects a blocked, claimed, or active Beads issue. It also rejects
live scope conflicts and a second active delegation for the same issue. The
child hook uses the inherited delegation ID and client identity to attach the
new session.
The generated prompt requires every child to read repository instructions,
verify and claim the issue, declare the exact scopes, obey git authority, and
report a compact result. An implementation child edits and runs focused
validation. A validation child does not edit and reports its independent
verdict.

Inspect durable lifecycle state with:

```bash
agent-coord delegation status --delegation-id <delegation-id>
agent-coord delegation list --parent-session <parent-session-id>
agent-coord delegation list --parent-session <parent-session-id> --active
agent-coord delegation logs --delegation-id <delegation-id>
agent-coord ui --parent-session <parent-session-id>
agent-coord ui --cwd /absolute/repository/path
```

The managed supervisor wakes an inactive child only for undelivered actionable
messages and submits one generic prompt through its owned PTY. The prompt hook
then supplies the durable body and thread metadata. This keeps terminal workers
alive while their delegation is unfinished and lets them coordinate with their
parent or with one another.
The loopback-only UI home page creates and manages independent Codex and Claude Code browser
sessions through `codex app-server` or Claude's stream-json protocol: streamed conversations, approvals and
questions, stop, rename, close, reopen, and resume. Browser sessions do not
require a parent or Bead. **New session** and native **⌘N** open a draft conversation
in the current workspace. Choose a Codex or Claude Code model in the composer’s
**Model** picker; the provider session starts when the first message is sent.
Existing conversations offer models from their current provider.
Each conversation retains its provider across restarts. Claude uses the installed
CLI and its configured permission rules; approvals and questions appear in the
chat. Claude follow-ups queue while a turn runs, and Stop ends only that
conversation's process. The next message resumes its native transcript. Claude
terminal import and thread forking are not supported. Codex sessions default to workspace-write
sandboxing and on-request approvals. `/permissions yolo` enables full machine
access without approval prompts; `/permissions default` restores workspace access.
Idle browser sessions can also change this setting through **Permissions**
in the conversation footer; the choice
persists for that session and applies to subsequent turns. Running or closed
sessions must finish or reopen before their permissions can change.
Click the conversation title to rename it inline. The bottom status line shows
the working directory, effective model, and reasoning effort. `/cd` shows the
current directory; `/cd <path>` changes it for subsequent turns. Relative paths
resolve against the current directory; quoted paths and `~` are supported.
Directory changes stay within the UI workspace filter. `/model` lists
models, `/model <model-id> [effort]` changes the next turn's model, and `/effort
<level>` changes its reasoning effort. The slash menu completes model names and
supported effort levels. `/effort` lists supported levels and `/help` lists commands.
In the UI, `/fork` opens a new thread from the current idle Codex conversation;
`/close` stops and closes the current thread, preserving its history and returning
to the overview. These thread commands take no arguments or images and run
immediately, even when submitted with the queue shortcut.
Setting changes are saved without starting a model turn and
require an idle session. The installed Codex configuration is used without
bypassing hook trust.
Codex must be authenticated and Agent Coord hooks trusted through normal setup.
Closing a browser tab does not stop work; stopping the UI server stops its
app-server, and saved conversations can be resumed after a restart.

The Coordination monitor link (`/monitor`) shows the parent/child tree, lifecycle and process status,
recent bounded output, complete received-message history, and activity. Managed
terminal output is rendered using its cursor and erase controls rather than by
concatenating repaint traffic. Live Zellij screens are snapshotted into the
same durable output area, and the last successful capture remains available
after a pane or UI restart. Use
`--cwd` (or `--repo`) to filter it to a directory and its descendants; a
repository also includes its linked Git worktrees. For example,
`--cwd /opt/projects` permits `/cd` into its child repositories. Omitting the filter shows delegation trees across the shared database.
The tree sorts by most recent activity by default and can switch to creation
time or name. A selected
parent shows clickable child summaries and recent child output. The monitor is
read-only, so follow-up work for delegated workers uses Agent Coord messaging.

After the child reports a completed or failed result, its `Stop` hook records
token usage in durable delegation state and writes
`.agent-coord/delegations/<delegation-id>.usage.json` under the delegated
repository. `SessionEnd` is the fallback for an early exit. Inspect
`token_usage`, `token_usage_artifact_path`, and `token_usage_error` in
`delegation status`; usage-capture problems do not change the task outcome.

The child sends its result to the parent inbox. The SessionEnd hook or managed
supervisor records a failure if the child exits without a result. The parent
does not need to poll a pane or process to determine the final lifecycle state.
If a launch cannot attach and remains active, the parent can release it with
`agent-coord delegation cancel --delegation-id <id> --message '<reason>'`.

## Release work

Run `agent-coord end-work` when the session no longer owns
work, including after an unscoped solo change. A stopped turn with unfinished
work remains in `waiting` activity so a newcomer can detect it. Session-end
hooks mark the process offline, but they do not mutate Beads status.
