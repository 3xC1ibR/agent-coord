---
name: agent-coordination
description: Coordinate concurrent Claude Code and Codex sessions through conflict detection, durable local communication, Agent Coord-owned PTY delegation, and an optional local operator UI. Use before implementation when another coding-agent session may be active, when checking file conflicts, when sending or receiving agent messages, or when delegating ready Beads work to a new Codex or Claude Code worker.
---

# Agent Coordination

Use the bundled `scripts/agent-coord` CLI. Resolve it from this file's installed
plugin root: the plugin root is two directories above this `SKILL.md`. Hooks
announce the current coordination session ID and the absolute CLI path at
session start.

Agent Coord stores ephemeral session activity, write scopes, durable local
messages, and optional local wake state. Beads is optional for direct work. If
the repository uses Beads, it remains the durable task source of truth; Agent
Coord does not claim or update issues.

## Before implementation

1. Inspect other working sessions with `<agent-coord> list --relevant --cwd
   <repo>`. If no other session is doing work, proceed without a Beads issue or
   scope. The write hook records the solo session as implementing.
2. When another session is doing work, declare the smallest useful scope:

   ```bash
   <agent-coord> begin-work \
     --session-id <session-id> \
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

- List relevant sessions with `<agent-coord> list --relevant --cwd <repo>`.
- Recheck the current declaration with `<agent-coord> status --session-id <id>`.
- Check overlap with `<agent-coord> conflicts --session-id <id>`.
- Send actionable work with `<agent-coord> send --from-session <id> --session
  <peer-id> --classification action_required '<message>'`.
- Send to the one live owner of a bead with `<agent-coord> send --from-session
  <id> --bead <bead-id> --classification action_required '<message>'`.
- Continue a conversation by passing its `--thread-id`. Threads permit the same
  two sessions in either direction and reject unrelated participants.
- Use `--no-reply-required` when work is actionable but a conversational reply
  is unnecessary. Informational and closure messages default to no reply.
- Read the compact unacknowledged inbox with `<agent-coord> inbox --session-id
  <id> --unread`. Use `--all` only for complete history.
- Block for a peer handoff without polling with
  `<agent-coord> inbox --session-id <id> --wait`. It returns immediately if a
  message is undelivered, otherwise it polls the local store and
  refreshes session liveness until a message arrives, waiting indefinitely.
  Add `--timeout <seconds>` to bound the wait; on timeout it exits with
  status `5`. `--timeout` alone (without `--wait`) is rejected, and `--wait`
  cannot be combined with `--all`.
- Acknowledge a delivered message with `<agent-coord> ack --session-id <id>
  --message-id <message-id>`, or acknowledge the current unread set with
  `--all-unread`. Acknowledgement is a silent transport update and never sends
  a conversational receipt.

## Atomic handoff and thread closure

Atomic handoff requires a Bead-backed declaration. Use one transactional
handoff instead of releasing, notifying, and asking the recipient to reacquire
the same paths:

```bash
<agent-coord> handoff \
  --from-session <owner-session-id> \
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
<agent-coord> send \
  --from-session <id> \
  --session <peer-id> \
  --classification closure \
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
<agent-coord> wake-zellij enable --session-id <session-id>
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

Hook-delivered messages include their thread and `reply_required` value. Reply
conversationally only when `reply_required=true`, reuse the same thread, and use
transport acknowledgement independently. Never reply to or acknowledge an
acknowledgement; acknowledgements do not create messages.

## Delegate work to a managed agent

Use `delegate` when a registered parent session must create a separate Codex or
Claude Code worker. The work must have one open and ready Beads issue and
explicit repository scopes. Codex is the default child; pass `--client claude`
to launch Claude Code.

Run a dry-run preview first:

```bash
<agent-coord> delegate \
  --from-session <parent-session-id> \
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
between turns. It does not require Zellij or tmux and does not use the parent's
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
<agent-coord> delegation status --delegation-id <delegation-id>
<agent-coord> delegation list --parent-session <parent-session-id>
<agent-coord> delegation list --parent-session <parent-session-id> --active
<agent-coord> delegation logs --delegation-id <delegation-id>
<agent-coord> ui --parent-session <parent-session-id>
<agent-coord> ui --cwd /absolute/repository/path
```

The managed supervisor wakes an inactive child only for undelivered actionable
messages and submits one generic prompt through its owned PTY. The prompt hook
then supplies the durable body and thread metadata. This keeps delegated agents
long-lived and lets children coordinate with their parent or with one another.
The loopback-only UI shows the parent/child tree, lifecycle and process status,
recent bounded output, complete received-message history, and activity. Managed
terminal output is rendered using its cursor and erase controls rather than by
concatenating repaint traffic. Live Zellij screens are snapshotted into the
same durable output area, and the last successful capture remains available
after a pane or UI restart. Use
`--cwd` (or `--repo`) to restrict it to one repository; omitting the filter
shows delegation trees across the shared database. The tree sorts by most
recent activity by default and can switch to creation time or name. A selected
parent shows clickable child summaries and recent child output. The UI is
read-only, so follow-up work should be sent through Agent Coord messaging.

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
`<agent-coord> delegation cancel --delegation-id <id> --from-session
<parent-id> --message '<reason>'`.

## Release work

Run `<agent-coord> end-work --session-id <id>` when the session no longer owns
work, including after an unscoped solo change. A stopped turn with unfinished
work remains in `waiting` activity so a newcomer can detect it. Session-end
hooks mark the process offline, but they do not mutate Beads status.
