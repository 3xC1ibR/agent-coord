## Choose the agent mechanism

- **App agents** are persistent, independently addressable Ribbon Field
  conversations across supported providers (`codex`, `claude`, `antigravity`,
  `kimi`, `muse`). For a conversation the user can address directly and return to
  later, search existing app agents with `thread search --app-only`, reuse one
  with `send`, or create one with `thread create`.
- **Provider-native subagents** use the provider's tools and follow its context
  and lifecycle rules. Use them only when permitted by the active instructions,
  and follow those instructions on when and how to delegate. Creating one does
  not fulfill a request for a Ribbon Field app conversation.
- **Terminal delegates** use Agent Coord `delegate` for scoped terminal work.
  They report to their parent and stop after a completed or failed delegation.

Use [dispatch](../../dispatch/SKILL.md) for ongoing routing with direct specialist
follow-ups, or [orchestrate](../../orchestrate/SKILL.md) to coordinate and deliver
the integrated result in the initiating conversation. Both use the same app agents.

### When to orchestrate vs. work solo

Do not default to doing everything in a single polluted context window, and do
not spawn theatrical roleplay agents for trivial edits. Escalate to multi-agent
orchestration ([orchestrate](../../orchestrate/SKILL.md)) when at least one of
these holds:

1. **Context isolation (high-noise / compact-output):** Broad exploration, log
   triage, or subsystem debugging would flood your context with intermediate
   noise before implementation starts.
2. **True parallelism across disjoint write scopes:** Independent modules or
   workstreams have a clear contract and non-overlapping `--scope` paths.
3. **Epistemic independence (blind verification or NGT search):** You need an
   unbiased code review, security audit, or parallel root-cause investigation
   from a fresh agent (often a different `--client` provider) that has not
   absorbed your chain-of-thought rationalizations.
4. **Long-lived domain ownership:** Work touches a recurring domain (such as
   release/deployment, UI, or database migrations) where reusing a persistent
   specialist preserves negative memory (`tried_and_failed`) and direct user
   follow-up.
5. **Cross-provider arbitrage:** A task benefits from pairing distinct provider
   strengths (`--client codex | claude | antigravity | kimi | muse`) or running
   a `sidecar attach` acceptance monitor alongside implementation.

## Supervise delegated work

Use an independently addressable Ribbon Field app agent for long-running work.
Keep foreground provider-native subagents for bounded tasks. A foreground call
keeps its parent turn occupied and can leave subsequent instructions waiting.

Ribbon Field monitors delegated turns automatically. Leave progress-check
scheduling to Ribbon Field. It alerts the current supervisor first. If there
is no reachable supervisor, the first alert goes directly to the user. If the
condition persists, Ribbon Field repeats the alert and escalates to the user.

Act on every stall alert before dismissing it. Open the reported turn and
check its last confirmed progress and oldest queued instruction. For a live
tool, inspect its output and CPU activity before deciding whether to interrupt.
For a pending approval, route the original request to its authorized decision
maker. For a silent turn, use the guarded supervisor controls when recovery is
needed. Verify the outcome of the action you took.

Give each Bash call a finite timeout appropriate to the command. The Claude
Bash tool accepts `timeout` in milliseconds. For work that can outlast a tool
call, use a managed job with `--timeout` in seconds and subscribe to its result.
Inspect any partial effects before retrying a timed-out command.

## Create and reuse app agents

An **app agent** is an independent app conversation with its own context. A
**specialist** is an agent focused on a subject or responsibility; a
**dispatcher** routes work. Reserve **subagent** for provider-native subagents.
The existing `delegate` command creates a terminal worker for a scoped task.

Prefer an existing relevant app conversation, including Closed. Discover with
`thread search 'topic keywords' --app-only --limit 10` without `--cwd` unless
the user selected a workspace. Search saved context and user/assistant messages;
all keywords must match saved context or occur together in one message. Use
`--source context` for routing metadata alone or `--my-messages` for what the
user wrote. Excerpts include message links, and coverage reports missing history;
external provider transcripts are not searched. Follow `next_cursor` with `--cursor`
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
agent-coord thread provider-limits --client codex --cwd /absolute/workspace --wait 30
agent-coord thread create \
  --cwd /absolute/workspace --client codex --name 'Release specialist' \
  --model <advertised-model> --effort <supported-level> \
  --request-id <unique-request-key> --wait 30 \
  'The exact user request, including its scope and constraints.'
```

Before choosing among providers or models for delegated work, use `thread models`
to discover advertised models and `thread provider-limits` for account quota
windows, percentages, reset times, observation time, and freshness. Repeat for
each relevant client. Quotas can be shared across models or scoped to a named
pool; do not infer a model-specific allowance where none is reported. A stale,
missing, unsupported, or failed reading means availability is unknown, not
unlimited or exhausted. A past reset time does not prove quota has reset. An
advertised model and remaining quota do not guarantee a successful request.
These commands require a running Ribbon Field app; inspect a queued request with
`thread request-status` if it does not complete within the wait.

Use `--client claude` for Claude Code, `--client antigravity` for Google
Antigravity, `--client kimi` for Kimi Code, or `--client muse` for Muse Code
(including Muse Wonder when available to the account). Model and effort are optional; select
them when the user authorized that choice. Add `--yolo` only with explicit
authorization for full machine access without approval prompts. Defaults do
not inherit the dispatcher's YOLO mode. Creation does not bypass hook trust.
Pass `-` as the prompt to read stdin without shell escaping.

Use `thread create --mode orchestration` for an agent created to contribute to
an outcome you are coordinating and delivering. Omit `--mode` for dispatch or
ordinary app-agent creation. The mode records creation intent independently of
reply requirements; later messages and reuse do not change it. Ribbon Field
groups orchestration-created agents beneath their creator and displays untagged
agents separately, including older creations without a recorded mode. Creator
provenance remains available in both cases. This requires an updated running
app as well as the CLI; a completed receipt's `result.mode` confirms the recorded
mode (`null` when omitted).

A `completed` receipt contains `thread_id` and `result`: session identity,
effective settings, stable URL, and initial coordination message ID. It confirms
creation and inbox queueing, not completion of the agent's task. The exact
request is saved as the original request, then sent as actionable work. By default
(or with `--no-reply-required`), the agent answers the user in its own chat with no
reply obligation to the creator.

Creation automatically subscribes the creator to completion of the created
agent's initial assignment turn. The completion event carries the saved
checkpoint and, when saved, its matching context
lifecycle envelope. No result reply is needed solely to deliver that event.
Review the reported outcome against the assignment; a completed turn does not
by itself prove the task is complete. Completion events require no reply.
Use `--no-subscribe` to opt out of the automatic subscription. A completed
creation receipt confirms `result.subscribe` and `result.subscription_id`
(`null` when opted out). This delivery requires the updated running app backend;
plugin refresh alone does not replace a running backend.

Add `--reply-required` to `thread create` when an explicit agent-authored reply
to the initial request is needed in addition to its automatic completion event.
The recipient uses `agent-coord reply --message-id <id>` with
the delivered initial message ID; the reply reaches the creator's inbox and wakes
an eligible idle app conversation. The completed creation receipt includes
`result.reply_required`, which confirms the applied contract. This flag needs an
updated running app backend as well as the CLI; refreshing plugins alone does
not update a running native app. A missing receipt field indicates an older backend.

The contract applies to that assignment, not every later turn. Direct user
follow-ups remain available, and later `send` requests choose their own reply
obligation. Use new requests for questions or revisions after completion, and
share material findings when useful without recurring check-ins.

The app runtime automatically reports app-agent failures to the creating agent,
regardless of creation mode or reply requirements. Failed turns (including usage
and credit limits), unexpected provider disconnects during work, and creation or
inbox-start failures produce durable actionable inbox messages with the agent
identity and available error details. Each failure is reported once; a later
failed turn produces a new report. These reports require no reply and wake an
eligible idle creator through the normal inbox guards. Deliberate Stop is not a
failure report. Failed work is not automatically retried. This behavior requires
an updated running app backend, not just refreshed CLI plugins.

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

To recover an app agent you created after a confirmed failed turn, inspect the
failure and current state, then send a fresh actionable continuation describing
what to inspect before repeating side effects. Authorize that continuation with:

```bash
agent-coord thread resume --session-id <agent-id> \
  --expected-turn-id <failed-turn-id> --request-id <unique-recovery-key> --wait 30
```

To interrupt a running Claude or Codex app agent, inspect its current turn ID,
then submit the interruption as its current supervisor:

```bash
agent-coord thread interrupt --session-id <agent-id> --expected-turn-id <active-turn-id> \
  --request-id <unique-interrupt-key> --reason 'The inspected turn requires intervention.' --wait 30
```

The MCP operations are `interrupt_thread` and `resume_thread`. Inspect the
terminal turn and any outstanding tool outcomes, send the agent fresh actionable
instructions, then run the turn-keyed resume command above. If execution remains
unverified, inspect it before using `--confirm-stopped` with a specific reason.
A Stop issued by the user retains its existing user-controlled release.

The current supervisor can authorize recovery. A completed request with
`result.state: ready` means the inbox can accept work again.

When a wake fails before the app records a turn, inspect `wake_pause_reason`
and `wake_message_id` in `thread show`. Repair a missing working directory
before resuming. Close each superseded assignment on its original message
thread so queued instructions reflect the work still wanted.

```bash
agent-coord thread resume --session-id <agent-id> \
  --expected-message-id <wake-message-id> --request-id <unique-recovery-key> --wait 30
```

The user can resume the inbox from the conversation in the app. A known
launch rejection leaves the original batch eligible. An uncertain batch
retains its reservation and later messages can wake the agent. The runtime
can confirm a Claude submission from its native source input UUID.

An `app_agent_failed`, `app_agent_recovery_overdue` or `app_agent_work_queued`
report means the agent's automatic wake is paused, not just that one assignment
failed. Later messages, including your own, are queued and do not wake it until
you recover it or hand off its scope. Acknowledging the report is not recovery.
A `send` or `reply` receipt includes `recipient_wake` (also printed as a warning
on stderr) whenever the recipient's wake is paused. Read an agent's run state
from `execution.state` in `thread list` or `thread show`.

Route later requests with `send --session <agent-id> --classification
action_required --no-reply-required`. Request a reply only when coordination
requires one. Messages can reach busy agents through tool hooks; automatic
wake waits for idle and prioritizes queued user input. Release scopes after assignments; completing work does not close the chat.

Closing preserves identity, context, and settings. The same ordinary `send`
wakes a closed app specialist and automatically returns it to Now, restoring
its direct user composer. No reopen flag or separate reopen command is needed.
A deliberate Stop requires user intervention. Use the guarded recovery actions above for failed wakes.
Antigravity delivers peer messages after the active prompt finishes, preserving
running tools and pending approvals. An updated running app services pending messages;
offline queueing does not launch the app. Terminal `delegate` behavior is unchanged.

## Delegate work to a managed terminal worker

Use `delegate` when a registered parent session must create a separate Codex or
Claude Code worker. Supply a task prompt and explicit repository scopes. Beads
is optional. Codex is the default child; pass `--client claude`
to launch Claude Code.

Run a dry-run preview first:

```bash
agent-coord delegate \
  --cwd /absolute/repository/path \
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

Add `--bead <ready-bead-id>` only when the task uses Beads. The issue must be
open, ready, and unclaimed. Without that option, the launcher does not look up
or run `bd`, and the child uses work-thread checkpoints and delegation state.
Do not create an issue solely to unlock delegation or handoff. An explicit
invalid issue is an error; do not silently discard the requested issue link.

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
failure. Use a separate implementation task for remediation and a new validation
task for the next attempt, with issues only when using Beads. Validation-only
delegation rejects `--yolo`.

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

When `--bead` is supplied, the launcher rejects a blocked, claimed, or active
issue and a second active delegation for the same issue. All delegations reject
live scope conflicts. The
child hook uses the inherited delegation ID and client identity to attach the
new session.
The generated prompt requires every child to read repository instructions,
declare the exact scopes, obey git authority, save work-thread checkpoints, and
report a compact result. A child with an issue also verifies and claims it,
records Beads progress, and closes it when its acceptance criteria are met.
An implementation child edits and runs focused
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
The loopback-only UI home page creates and manages independent Codex, Claude Code,
Google Antigravity, Kimi Code, and Muse Code sessions through their native protocols: streamed conversations, approvals and
questions, stop, rename, close, reopen, and resume. Browser sessions do not
require a parent or Bead. **New session** and native **⌘N** open a draft conversation
in the current workspace. Choose a provider's model in the composer’s
**Model** picker; the provider session starts when the first message is sent.
Existing conversations offer models from their current provider.
Each conversation retains its provider across restarts. Kimi uses the current
Kimi Code CLI (`kimi acp`) and its existing authentication; run `kimi login`
before creating a Kimi conversation. Its configured models and thinking levels,
including K3 when available, come from the runtime. Kimi uses native approvals,
queues follow-ups, and resumes the same native session after Stop. Its workspace
is fixed when created; changing directories requires a new conversation. Native
Kimi permissions are not an OS sandbox. The archived Python `kimi-cli` is unsupported.
Muse uses `muse serve` and existing authentication from `muse login`. Models and
reasoning levels come from `thread models --client muse`. It uses Muse's native
`:ask-me` permission profile by default and `:unrestricted` for explicitly
authorized full permissions. The selected profile is verified on start and
resume. Follow-ups queue, Stop affects only that conversation, and the next
message resumes the same native session. Its workspace is fixed; create a new
conversation to change directories. Muse app agents can send and receive Agent
Coord messages and create other app agents with the same CLI commands above.
Native Muse children share the parent's write scope but do not consume its inbox.
In default-mode Muse sessions, use the native bash tool's
`sandbox_permissions: require_escalated` for Agent Coord commands when its
database is outside the primary workspace, and allow Muse to request approval.
Ordinary command approval does not grant access outside that shell sandbox.
Claude uses the installed
CLI and its configured permission rules; approvals and questions appear in the
chat. Claude follow-ups queue while a turn runs, and Stop ends only that
conversation's process. The next message resumes its native transcript. Claude
terminal import is not supported. Idle Claude app conversations support **Fork**
and `/fork`, preserving history and settings without starting a model turn.
Codex sessions default to workspace-write
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
In the UI, `/fork` opens a new thread from the current idle Codex or Claude conversation;
`/close` stops and closes the current thread, preserving its history, removing
its tab, and selecting the adjacent tab (or Overview when no tabs remain).
These thread commands take no arguments or images and run
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
