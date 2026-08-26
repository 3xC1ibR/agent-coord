# Agent Coord

Agent Coord is a local coordination channel for Claude Code and Codex sessions.
It uses one dependency-free Python CLI, SQLite, hooks, and a shared skill. It
does not need an MCP server. An optional receiver-side watcher can wake an idle
agent that runs in Zellij.

The plugin answers these questions:

- Which sessions are online, stale, or offline?
- Which sessions are discussing, planning, implementing, validating, or waiting?
- Which claimed Beads issue and file scopes does each working session own?
- How can a session atomically transfer a complete scope and its validation
  boundary without briefly releasing it to competing work?
- How can a session reserve a stable scope for read-only validation?
- How can one session send a durable message to another session or to the live
  owner of a Beads issue?
- Which messages require action or a reply, and which are history-only receipts?
- How can a Zellij session safely start a turn when a message arrives while it
  is idle?
- How can a session delegate ready Beads work to a new interactive Codex
  session in a visible Zellij pane?

## Design

Beads is the source of truth for implementation work. Agent Coord does not
claim, update, or close issues. Before a session can declare implementation
work, `agent-coord begin-work` verifies that the issue is claimed and has the
`in_progress` status.

Session and message state is in a WAL-mode SQLite database at:

```text
~/.local/state/agent-coord/state.sqlite3
```

Set `AGENT_COORD_DB` to use another path. Set
`AGENT_COORD_STALE_AFTER_SECONDS` to change the default 30-minute stale
threshold.

The same plugin directory contains Codex and Claude manifests. Both clients use
the same hooks, skill, CLI, and database schema.

Delegation lifecycle state is also in SQLite. Launch mechanics are in a
Zellij-specific adapter. The durable parent, child, result, and failure state
does not depend on pane inspection.

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
the absolute path to the bundled CLI. Before implementation:

```bash
bd update <bead-id> --claim

<agent-coord-path> begin-work \
  --session-id <session-id> \
  --bead <bead-id> \
  --scope 'src/**' \
  --scope 'tests/test_feature.py' \
  --lease-mode write
```

`begin-work` rejects a declaration when another live session owns the same
Beads issue or an overlapping scope in the same repository.

Use `--lease-mode validation` to reserve an exclusive, stable scope without
granting the session permission to use structured write tools. This is distinct
from `--activity validating`: a normal write lease may enter the `validating`
activity and still fix files. A validation lease blocks every overlapping lease
until it is handed off or released.

Common commands:

```bash
agent-coord list --cwd /path/to/repo --relevant
agent-coord status --session-id <session-id>
agent-coord conflicts --session-id <session-id>

agent-coord send \
  --from-session <session-id> \
  --session <peer-session-id> \
  --classification action_required \
  --thread-id <thread-id> \
  'Can you release src/api/**?'

agent-coord send \
  --from-session <session-id> \
  --bead <bead-id> \
  --classification informational \
  'I need to coordinate a shared interface change.'

agent-coord inbox --session-id <session-id>
agent-coord inbox --session-id <session-id> --unread
agent-coord inbox --session-id <session-id> --all
agent-coord inbox --session-id <session-id> --wait
agent-coord inbox --session-id <session-id> --wait --timeout 120
agent-coord ack --session-id <session-id> --message-id <message-id>
agent-coord ack --session-id <session-id> --all-unread
agent-coord end-work --session-id <session-id>
```

Messages stay in SQLite until the recipient reads them. Delivery and explicit
acknowledgement have separate timestamps. Addressing by Beads issue succeeds
only when exactly one live session declares that issue.

Messages are classified as `action_required`, `informational`, or `closure`.
Only undelivered `action_required` messages enter hook context or wake an idle
agent. `reply_required` is separate: an actionable message may require work but
no conversational reply, as with an atomic handoff. The default is true for
`action_required` and false for the other classifications; use
`--no-reply-required` to opt out explicitly. Transport acknowledgement is
silent and never creates another message.

Continue a conversation with the same `--thread-id`. A thread accepts only the
same pair of sessions, in either direction. Close it with one terminal message:

```bash
agent-coord send \
  --from-session <session-id> \
  --session <peer-session-id> \
  --classification closure \
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

Use `handoff` when the current owner has reached a named patch or validation
boundary and the recipient must acquire the complete declaration without a
scope-free race:

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

## Delegate work to a Codex Zellij pane

A registered Codex or Claude parent can launch a new Codex worker for an open,
ready, and unclaimed Beads issue. First, preview and validate the launch without
changing SQLite or opening a pane:

```bash
agent-coord delegate \
  --from-session <parent-session-id> \
  --cwd /absolute/path/to/repository \
  --bead <ready-bead-id> \
  --scope 'src/**' \
  --scope 'tests/test_feature.py' \
  --zellij-session friendly-lemur \
  --floating \
  --name delegated-feature \
  --model <codex-model> \
  --reasoning-effort <level> \
  --dry-run \
  'Implement the feature, run the focused tests, and report the result.'
```

Remove `--dry-run` to launch the pane. If the parent runs inside the target
Zellij session, the command reads `ZELLIJ_SESSION_NAME`, and you can omit
`--zellij-session`. Omit `--floating` to create a normal pane. Floating panes
use 90 percent width and 85 percent height by default. Use `--width` and
`--height` to change these values.

Use `--model` to select the child Codex model. Use `--reasoning-effort` to set
the child `model_reasoning_effort` configuration value. The options are
independent and optional. If you omit one, Codex uses its normal configuration
for that setting. Agent Coord records both requested values in durable
delegation state. It does not restrict model and effort combinations because
Codex support can differ by model and release; the child Codex process validates
the selected combination.

The default command opens the interactive Codex TUI with `--approve-for-me` in
the requested Zellij pane. The optional `--yolo` flag starts Codex with
`--dangerously-bypass-approvals-and-sandbox`. Use it only after explicit
authorization.

Codex also requires persisted trust before it runs hooks. Review and trust the
installed Agent Coord hook before delegation. If the target repository's
complete enabled hook set was reviewed, `--bypass-hook-trust` starts the
interactive Codex session with
`--dangerously-bypass-hook-trust`. This separate flag keeps reviewed command
permissions but runs all enabled hooks without persisted trust for that one
invocation. Do not use it as a default.

Before launch, Agent Coord verifies the Git repository, `bd`, Codex login,
Zellij, issue readiness, live write-scope conflicts, and active delegation
state. It then creates one durable delegation record and starts Codex with an
inherited delegation ID. Reviewed mode adds only the Agent Coord database
directory as an extra writable root, so child CLI calls can update lifecycle
state outside the repository. The child SessionStart hook attaches the new
session. The generated instructions require the child to:

1. Read repository instructions and run `bd prime`.
2. Verify and claim the ready issue.
3. Declare the exact write scopes with `begin-work`.
4. Apply repository validation, changelog, and git-authority rules.
5. Record a completed or failed result before exit.

Inspect delegation state with:

```bash
agent-coord delegation status --delegation-id <delegation-id>
agent-coord delegation list --parent-session <parent-session-id>
agent-coord delegation list --parent-session <parent-session-id> --active
agent-coord delegation cancel \
  --delegation-id <delegation-id> \
  --from-session <parent-session-id> \
  --message 'The child did not attach.'
```

The result creates a durable message in the parent inbox. If the child process
exits before it reports a result, the SessionEnd hook records the delegation as
failed and sends that failure to the parent. A second active delegation for the
same repository and Beads issue is rejected. The parent can use `delegation
cancel` to release an active record after a confirmed launch or attachment
failure.

## Wake idle Zellij sessions

An idle agent has no model turn in which a hook can run. Enable the optional
Zellij watcher from the agent's own pane to let Agent Coord start one safe turn
when an unread message arrives:

```bash
agent-coord wake-zellij enable --session-id <session-id>
agent-coord wake-zellij status --session-id <session-id>
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
do not wake the agent. The watcher does not focus the pane, erase input, mark
the message delivered, or retry a reserved message. A typed prompt, active
model turn, missing pane, or failed Zellij command therefore cannot corrupt user
input or block the sender. Inspect `wake-zellij status` for the last error and
recent wake attempts.

Wake-up is currently Zellij-specific. Durable messaging, inbox reads, and
sender behavior do not require Zellij.

## Hook behavior

- `SessionStart` registers the process, attaches an inherited delegation,
  optionally starts Zellij wake, and delivers unread actionable messages.
- `UserPromptSubmit` records an active model turn and delivers unread actionable
  messages.
- `PreToolUse` checks structured write tools against the declared issue, state,
  lease mode, scopes, and live conflicts. Validation leases deny structured
  write tools.
- `PostToolUse` refreshes liveness and delivers unread actionable messages.
- `Stop` records an inactive turn plus `waiting` for unfinished declared work
  or `idle` otherwise.
- `SessionEnd` records an unfinished child delegation as failed, disables
  Zellij wake, and marks the process offline without changing Beads.

The write guard covers Claude `Edit` and `Write` tools and Codex `apply_patch`.
It does not parse arbitrary shell commands. The skill instructs agents to
declare work before any implementation, including shell-based writes.
Validation lease mode expresses non-editing intent and protects structured
write tools; it is not a security boundary for arbitrary shell commands.

## Develop and validate

The runtime supports Python 3.10 or later and has no third-party dependencies.

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
python3 -m compileall -q plugins/agent-coord/scripts tests

uv run --with pyyaml python \
  /Users/walle/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py \
  plugins/agent-coord

uv run --with pyyaml python \
  /Users/walle/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  plugins/agent-coord/skills/agent-coordination

"${CLAUDE_BIN:-claude}" plugin validate --strict plugins/agent-coord
"${CLAUDE_BIN:-claude}" plugin validate --strict .
```

The two Codex validator paths are local Codex development helpers. Use the
equivalent installed paths when this repository is on another machine.

## Refresh installed plugins after changes

Run this workflow after a change under `plugins/agent-coord/`, including a
change to a skill, hook, script, or manifest. Initial marketplace installation
is not part of the update loop. Do not hand-edit `marketplace.json`, Codex
configuration, or the cachebuster suffix.

First, run the validation commands in **Develop and validate**. Then run these
commands from the repository root:

```bash
set -euo pipefail

plugin_creator_root="${CODEX_HOME:-$HOME/.codex}/skills/.system/plugin-creator"
marketplace_file="$PWD/.agents/plugins/marketplace.json"
claude_bin="${CLAUDE_BIN:-$(command -v claude)}"

marketplace_name="$(
  python3 "$plugin_creator_root/scripts/read_marketplace_name.py" \
    --marketplace-path "$marketplace_file"
)"

python3 "$plugin_creator_root/scripts/update_plugin_cachebuster.py" \
  "$PWD/plugins/agent-coord"

codex_install_json="$(codex plugin add "agent-coord@$marketplace_name" --json)"
printf '%s\n' "$codex_install_json"

"$claude_bin" plugin uninstall agent-coord@agent-coord --scope user
"$claude_bin" plugin install agent-coord@agent-coord --scope user
```

If `claude` resolves to an older installation without the `plugin` command,
set `CLAUDE_BIN` to the current Claude Code executable and use it for every
Claude command in this workflow.

The cachebuster helper preserves the base Codex version and replaces its one
`+codex.<value>` suffix. Do not increase the base version only to refresh a
local cache. Claude uses its own manifest version, so uninstall and install it
again to replace the cached files even when that version did not change.

Verify that both installed copies contain the source files. The following
continues from the variables above and ignores generated Python bytecode:

```bash
codex_cache="$(printf '%s' "$codex_install_json" | jq -r .installedPath)"
claude_cache="$(
  "$claude_bin" plugin list --json | jq -r \
    '.[] | select(.id == "agent-coord@agent-coord") | .installPath'
)"

while IFS= read -r -d '' source_file; do
  relative_path="${source_file#plugins/agent-coord/}"
  cmp "$source_file" "$codex_cache/$relative_path"
  cmp "$source_file" "$claude_cache/$relative_path"
done < <(
  find plugins/agent-coord -type f \
    ! -path '*/__pycache__/*' \
    ! -name '*.pyc' \
    -print0
)

codex plugin list
"$claude_bin" plugin list --json
```

Any `cmp` failure means the installed cache does not match the repository.
Stop and correct the selected marketplace or installation before live testing.
After verification, start new Codex and Claude sessions. Existing sessions keep
the skills and hooks that they loaded at startup.
