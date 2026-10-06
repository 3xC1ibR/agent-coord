# Manage threads across workspaces

## Find the installed tools

Use the absolute `scripts/agent-coord` path announced by the session-start hook.
If the hook did not announce it, resolve the plugin root from the installed
skill: it is two directories above the `manage-threads` skill directory.
Use `<plugin-root>/scripts/agent-coord`; do not assume the executable is on PATH
or look for the source checkout. Below, `<agent-coord>` means that executable.

Keep the inherited `AGENT_COORD_DB` or announced `--db` override. Otherwise the
CLI uses the shared database under `~/.local/state/agent-coord/` (or
`XDG_STATE_HOME`). The current directory does not limit thread discovery.
The inventory contains threads recorded in this database, including stopped
sessions; it does not automatically import other clients' historical chats.

## Review what is open

Start with the global inventory unless the user asks for a narrower review:

```bash
<agent-coord> thread list
```

This returns JSON for both Now and Later threads, ordered by recent activity.
Use `thread list --archived` for closed history. Use `--repository <id>`,
`--project <id>`, `--no-repository`, or `--no-project` when requested. Use
`--cwd <path>` only for an explicitly selected workspace; do not default it to
the manager's directory. The coordination command `list --relevant` is for
working-session conflicts and is not a complete work-thread inventory.

The inventory includes titles, original requests, repository/project/workspace
associations, latest checkpoints, links, turn activity, unread results, and a
`checkpoint_stale` flag. Inspect selected threads more closely with:

```bash
<agent-coord> thread show --session-id <thread-id>
<agent-coord> status --session-id <thread-id>
```

`thread show` adds checkpoint history. It does not return the full conversation.
`status` supplies the session's current activity, presence, Bead, and scope.
Use these details where they affect the user's decision rather than reading
every thread's history up front. Identify the manager's own thread from the
announced session ID and normally leave it out of its workload review.

Present a compact review suited to the request: decisions required from the
user, work in progress, work waiting on an agent or external party, finished
work, and Later threads. Include recognizable titles, workspace or project
when needed to distinguish them, and the next concrete action. Keep IDs
available for commands without making the user work through an ID inventory.

Interpret the saved evidence carefully:

- A current checkpoint with `next_actor: user` and a specific `next_action`
  identifies a required user action. An unread result may only need review.
- `phase: finished` establishes completion only when the checkpoint is current.
  A completed model turn, stopped process, or old last-activity timestamp alone
  does not establish completion of the user's work.
- A stale or missing checkpoint leaves progress uncertain. Describe that gap;
  do not invent a next step or silently classify the work as finished.
- Now/Later/Closed placement is separate from progress and process activity.
  Preserve the user's placement while offering recommendations.

## Carry out organization requests

Resolve the requested threads from the inventory. If a title is ambiguous,
inspect its request and workspace, then ask only if the target remains unclear.
Apply concrete instructions directly; retain authorization already given in
the conversation. An open-ended review supports recommendations, while the
user chooses which threads to move, rename, or reassociate.

```bash
<agent-coord> thread update --session-id <id> --title 'Recognizable title'
<agent-coord> thread update --session-id <id> --attention later
<agent-coord> thread update --session-id <id> --attention now
<agent-coord> project list
<agent-coord> repository list
<agent-coord> project create --name 'Migration'
<agent-coord> thread update --session-id <id> --project <project-id>
<agent-coord> thread update --session-id <id> --repository <repository-id>
<agent-coord> thread update --session-id <id> --no-project
<agent-coord> thread update --session-id <id> --no-repository
```

Projects can span repositories. Associations do not move files or change the
thread's actual working directory. Reuse existing project and repository IDs;
`repository add --path <path>` registers a repository when needed.
For a request affecting multiple threads, apply the authorized change to each
resolved ID, check each result, and report any failures separately.

Moving a thread to Later does not stop its agent or release its file scope.
`thread update --attention archived` changes placement only, including for a
running session. The UI's **Close thread** / **Stop and close** action also ends
the live session and releases its scope; use that action when the request is
to close a live session. Closing preserves history and does not mark the
underlying task complete. **Reopen** restores a closed session for continuation.
Do not substitute metadata changes for lifecycle actions.

The optional `ui` command without `--cwd` opens the global overview. It provides
search, pinning, full conversation viewing where available, and lifecycle
controls. Offer that route when the requested action is not exposed in the CLI;
do not read or write database tables as a substitute. The shared
[coordination skill](../SKILL.md) covers messaging, wake-up, and delegation when
the user asks to follow up with workers. Thread review alone does not require
starting or messaging another agent.

## Preserve the review

Use the manager's own session ID for its factual checkpoint, following the
checkpoint instruction supplied by the hooks. Record what was reviewed or
changed and any actual remaining action. Do not overwrite another thread's
progress checkpoint based on a management review.

When the requested review or organization is complete, save `phase: finished`,
`next_action: ""`, and `next_actor: nobody`. A proposed next task or invitation
to continue is optional advice, not a required user action. When a specific
decision is needed to complete the request, record it with its actual owner.
Use the CLI's normal checkpoint history; no separate task tracker is needed.
