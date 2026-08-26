---
name: agent-coordination
description: Coordinate concurrent Claude Code and Codex sessions in a Beads repository. Use before implementation, when checking active work or file conflicts, when sending and receiving messages between local coding-agent sessions, or when delegating ready Beads work to a new Codex Zellij pane.
---

# Agent Coordination

Use the bundled `scripts/agent-coord` CLI. Resolve it from this file's installed
plugin root: the plugin root is two directories above this `SKILL.md`. Hooks
announce the current coordination session ID and the absolute CLI path at
session start.

Beads remains the durable source of truth for work. Agent Coord stores
ephemeral session activity, write scopes, durable local messages, and optional
local wake state. Do not use Agent Coord instead of claiming or updating a
Beads issue.

## Before implementation

1. Run `bd show <bead-id>` and confirm the work belongs to that issue.
2. Claim the issue with `bd update <bead-id> --claim` if it is not already
   assigned to this user.
3. Run the bundled CLI with:

   ```bash
   <agent-coord> begin-work \
     --session-id <session-id> \
     --bead <bead-id> \
     --scope '<file-or-directory-glob>' \
     --lease-mode write
   ```

   Repeat `--scope` for distinct areas. Declare the smallest useful scope.
   Use `--lease-mode validation` only for an exclusive, stable validation
   reservation that must not edit through structured write tools. Ordinary
   `--activity validating` on a write lease may still make fixes.
4. If the command reports a conflict, send the owning session a message and do
   not edit until the overlap is resolved.

The write hooks deny edits without a claimed Beads issue, edits outside the
declared scope, and edits that overlap another live work declaration.

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

Use one transactional handoff instead of releasing, notifying, and asking the
recipient to reacquire the same paths:

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

## Wake an idle Zellij session

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

## Delegate work to a new Codex pane

Use `delegate` when a registered parent session must create a separate Codex
worker. The work can have any implementation instructions, but it must have one
open and ready Beads issue and explicit write scopes.

Run a validation-only preview first:

```bash
<agent-coord> delegate \
  --from-session <parent-session-id> \
  --cwd /absolute/repository/path \
  --bead <ready-bead-id> \
  --scope 'src/**' \
  --scope 'tests/test_feature.py' \
  --zellij-session <session-name> \
  --floating \
  --name <pane-name> \
  --model <codex-model> \
  --reasoning-effort <level> \
  --dry-run \
  'Implement the specific requested change and run the focused tests.'
```

Remove `--dry-run` to launch the worker. The command can read the Zellij
session from `ZELLIJ_SESSION_NAME`, so `--zellij-session` is optional when the
parent runs inside the target session. `--floating` is optional.

`--model` selects the child Codex model. `--reasoning-effort` sets its
`model_reasoning_effort` configuration value. Each option is optional and is
stored with the durable delegation. Do not guess a model or reasoning effort
when the user did not request one. Let Codex validate whether the selected
model supports the requested effort.

The default launch opens the interactive Codex TUI in the Zellij pane with
`--approve-for-me`. Use `--yolo` only when the user explicitly authorizes Codex
to bypass approvals and sandboxing. Never infer that permission from a request
to delegate work.

Codex requires persisted trust before it runs hooks. Review and trust the
installed Agent Coord hook before delegation. If that is not possible,
`--bypass-hook-trust` is an explicit escape hatch for a repository whose
complete enabled hook set was reviewed. This flag does not enable yolo mode,
but it runs every enabled hook without persisted trust for that invocation. Do
not add it by default or infer permission to use it from a request to delegate
work.

The launcher rejects a blocked, claimed, or active Beads issue. It also rejects
live scope conflicts and a second active delegation for the same issue. The
child hook uses the inherited delegation ID to attach the new Codex session.
The generated prompt requires the child to read repository instructions,
verify and claim the issue, declare the exact scopes, validate the work, obey
git authority, and report a completed or failed result.

Inspect durable lifecycle state with:

```bash
<agent-coord> delegation status --delegation-id <delegation-id>
<agent-coord> delegation list --parent-session <parent-session-id>
<agent-coord> delegation list --parent-session <parent-session-id> --active
```

The child sends its result to the parent inbox. The SessionEnd hook records a
failure if the child exits without a result. The parent does not need to poll
the Zellij pane to determine the final lifecycle state. If a launch cannot
attach and remains active, the parent can release it with
`<agent-coord> delegation cancel --delegation-id <id> --from-session
<parent-id> --message '<reason>'`.

## Release work

Run `<agent-coord> end-work --session-id <id>` only when the session no longer
owns the declared write scope. A stopped turn with unfinished work remains in
`waiting` activity so another session can still see the lease. Session-end hooks
mark the process offline, but they do not mutate Beads status.
