## Keep a work-thread checkpoint

Threads preserve the user's open conversations across stopped processes and
repositories. They do not require Beads. A thread's Now/Later/Closed placement
is the user's choice; checkpoints and activity do not change placement. Routing
new actionable work to a closed app conversation automatically moves it into Now.

Save a short factual checkpoint only when saved assignment state materially
changes: a new objective, decision, constraint, substantial result, actual phase
transition, blocker, required next action, or handoff. Ending a reply is not a
checkpoint trigger. Skip routine explanations, clarifications, reformatting,
acknowledgments, and unchanged state. Do not rewrite the summary to describe
each answer. Runtime activity and turn completion provide recency and unread
results without a checkpoint. Prefer `update_checkpoint` when available.
Send fields directly, such as `{"phase":"validation"}`. MCP writes
require host-supplied request identity or an inherited caller environment; an
explicit argument cannot establish identity. If the MCP process has no bound
caller, use the CLI from your current tool shell:

```bash
agent-coord checkpoint --json '{
  "title": "Database write performance",
  "domain": "backend",
  "phase": "investigation",
  "summary": "Compared the two approaches and documented the findings.",
  "next_step": null,
  "links": [{"kind": "document", "label": "Findings", "target": "docs/findings.md"}]
}'
```

The first checkpoint requires `phase` and `summary`. Later updates send only
changed fields; omission preserves all saved values, including a pending next
step. Set `next_step` to `null` to clear it, or send `actor` and `action` together.
Legacy CLI `next_actor` and `next_action` fields remain supported as a pair;
do not mix them with `next_step`. Each update saves a complete snapshot in
history. Tool and CLI success responses contain only `status` and
`checkpoint_id`; read state/history with `thread show` or CLI `--full`.

Use phases `discussion`, `investigation`, `planning`, `orchestrating`, `implementation`,
`validation`, `deployment`, or `finished`. Describe what was established or
changed in one or two sentences. Use `orchestrating` for ongoing routing and coordination of specialist agents,
including while a specialist implements or deploys. Coordination does not itself
require a user action. Distinguish proposals, implemented changes,
validation results, and deployment. Record the next required action and its
actual owner (`user`, `agent`, or `external`). Set `next_step.actor` to `user` only when
progress or completion requires a specific user answer, approval, decision, or
action, and describe that requirement in `next_step.action`. Ending an agent turn
does not create a required user action.

Keep optional advice, invitations to continue, and nonblocking reminders in
`summary`; do not turn them into user next steps. Keep the phase of the underlying activity: an answered investigation stays
`investigation`, a completed plan stays `planning`, and a status question during
deployment stays `deployment`. Use `finished` only after delivering the requested
implementation or execution, including validation/deployment if requested, with
nothing remaining. Set `next_step` to `null` when
there is no required next step. Required user review belongs in `validation`
with `next_step.actor` set to `user`. If work remains, keep its actual phase and assign
any required next step to its actual owner; do not invent follow-up tasks.
Skip unchanged checkpoints. The command preserves history and the original
request; it does not mark a Bead complete or close the thread.

At the first meaningful checkpoint, include an optional `title`: a specific
3–6 word name that makes the thread recognizable in the overview. Use the
thread's purpose, not a progress report or a copy of the opening message.
The saved context includes `title` and `title_source` (`auto`, `agent`, or
`user`). Replace an `auto` title when you understand the work. Omit `title`
on later checkpoints, including new assignments, to keep the conversation name
stable. Routine checkpoints can name only an `auto` title; they preserve established
`agent` and `user` names. Use `thread update --title` for a deliberate rename.
A user-chosen name takes precedence: agent checkpoints still
save progress but cannot replace a title whose source is `user`. The original
request remains unchanged. Titles accept 1–160 characters; the word count is
writing guidance.

An optional `domain` names the current assignment's category, such as `web`,
`backend`, or `docs`. Reuse one of the workspace domains supplied in hook context
unless the work is clearly elsewhere. Set it once per assignment; omit it on
routine checkpoints until the work moves. Unlike the conversation title, an
agent-set domain can change with a later assignment. Explicit names accept
1–64 characters and normalize whitespace and case; omission preserves the value.
Saved context includes `domain`, `domain_source`, and `scope_cluster`.
Sources are `derived`, `agent`, or `user`; unset fields are null. A user-chosen
domain cannot be overwritten by an agent or scope derivation.

Successful scope declarations supply a fallback based on the common directory
of their literal path prefixes, relative to the workspace (`.` for the entire
workspace). It persists after `end-work`; a later declaration updates a derived
domain. Explicit names take precedence. Path-derived labels are excluded from
the suggested vocabulary. Threads with neither signal remain unset.
At the user's request, use `thread update --domain <name>` for an override or
`thread update --no-domain` to reset to the last scope fallback and permit agent
reporting again. The checkpoint editor offers the same override and reset.
User-authored checkpoints may reset with `"domain": null`; agents must omit the
field instead of sending null. Domain metadata does not change thread placement.

Use `summary` for durable assignment state, not a log of individual replies. Its
first sentence should concisely describe the objective or substantial result;
use the second sentence for useful progress detail. Update it when starting a
new assignment or when that state materially changes. Ordinary discussion can
leave it unchanged across several turns. For example, the title
"Chat interface" stays the same while summaries change from "Adding thread
mentions." to "Making unread responses clearer." and then "Implemented clear
read/unread response cards." Describe failures as failed results. Do not leave
ongoing-work wording on a completed result or rename the conversation to report
its latest task. The full summary remains available in thread context/history.

Links accept `kind`, `label`, and `target`, with an optional `workspace_id` for a
different artifact workspace. The link field `project_id` is a legacy alias for
that workspace ID, not a named project ID. Kinds are `pull_request`, `document`, `issue`, `bead`, `branch`,
and `other`. Use exact references. Document paths resolve against the session's
workspace. Links are added or updated without removing earlier associations.
For complex text, `--json -` reads the object from stdin. `thread show
--session-id <id>` displays the original request, checkpoint history, and links.
The startup snapshot includes the latest saved checkpoint. Later hooks deliver unread messages and user metadata changes.

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
To move a Codex or Claude app conversation to another project directory at the
user's request, use the `move_thread` tool with an absolute `cwd`. Omit
`session_id` for your own thread, or provide the target ID. `from_session`, when
supplied, must match the bound caller. If MCP has no caller identity, use the CLI:

```bash
agent-coord thread move --session-id <thread-id> --cwd /absolute/destination --request-id <unique-key> --wait 0
agent-coord thread request-status --request-id <unique-key>
```

The move changes both the thread's project and execution directory, preserves
history and links, detects the destination repository, and clears a group from
the old project. It uses the same provider directory mechanism as `/cd`, without
adding a model message. Release the target thread's work scope with `end-work`
before requesting a move. The owning app waits for idle and queued messages;
your own active thread moves after your turn ends. A queued receipt is not
confirmation. Reuse the request ID for retries, inspect `request-status`, or
cancel a queued move with `thread cancel-request`. Closed threads must reopen;
providers with fixed workspaces require a new conversation instead. Terminal
sessions cannot be moved this way.

The CLI's legacy `archived` placement appears as **Closed** in the UI. To close
a session, use `thread close --session-id <id>`; the owning Ribbon Field runtime
performs the same lifecycle action as **Close thread** / **Stop and close**.
For your own thread, save the checkpoint and use `--after-turn` so your final
response arrives before closure. New input cancels the pending request. Inspect
with `thread close-status`, or cancel a queued request with `thread cancel-close`
(both take `--session-id`). A `queued` receipt is not confirmed closure; active
sessions need the updated app runtime. Do not search for ports or private HTTP
routes. See the [thread-management workflow](thread-management.md).
Closing releases the scope and keeps conversation history, checkpoints, links,
and settings; **Reopen** allows continuing later.
Closing does not imply task completion. Terminal shutdown requires a verified
Codex transcript owner and never targets a remembered Zellij pane or shared
app-server. When that cannot be verified, exit the terminal session first.

## Context lifecycle for app agents

App agents report context lifecycle advice at substantial milestones or actual
handoffs when another agent or replacement needs recovery information, including
substantive blocked or failed work. Routine discussion, clarification, simple
answers, and ordinary turn completion need no report. A checkpoint does not
itself require a handover. This policy also applies to user-created and
ordinary dispatch conversations.
Terminal delegates keep their existing behavior. Reporting is passive metadata;
it does not change creation mode, ownership, notifications, or conversation lifecycle.
Independent peer coordination and direct user follow-ups remain available.

When a milestone or handoff warrants recovery information, add
`context_lifecycle` to that checkpoint before transferring the work or sending
a requested substantive result reply. Keep only useful recovery facts and
failed approaches. The runtime binds the report to the current turn. Include
`assignment_message_id` for an agent assignment; omit it for direct user work:

```json
{
  "context_lifecycle": {
    "assignment_message_id": 123,
    "recommendation": "reuse",
    "reason": "Retained debugging constraints help with related fixes.",
    "reuse_for": ["Fixing this implementation"],
    "fresh_for": ["Independent review"],
    "handover": {
      "confirmed_facts": ["The parser rejects empty input."],
      "constraints": ["Keep the CLI dependency-free."],
      "tried_and_failed": ["Trimming after parsing preserves invalid whitespace."],
      "open_questions": []
    }
  }
}
```

Use `reuse`, `fresh`, or `either`; explain the recommendation with observed
conditions and identify the kinds of follow-up work each context would serve.
Preserve concrete findings, constraints, and failed approaches needed by a
replacement. Do not invent token counts or noise ratios. Use existing checkpoint
links for artifacts. Each array accepts at most eight non-empty strings of
400 characters; `reason` accepts 500 characters. The complete serialized report
must fit within 6000 characters. Arrays may be empty when there is nothing to report.

Omission preserves a saved report within the current turn; a new turn does not
inherit it. `"context_lifecycle": null` clears it. Changes save checkpoint history
even when the progress summary is unchanged. Automatic initial-assignment
completion events and explicit result replies attach a snapshot matched to the
assignment and exact turn, with its source `checkpoint_id`. Missing reports
remain unknown and do not block ordinary result delivery. This requires an
updated app runtime for automatic completion delivery as well as refreshed plugins.
The chat view's right-hand panel displays the saved recommendation, task-specific
reuse advice, and expandable handover. Advice from an earlier turn is marked as such.

Do not send a separate lifecycle assessment message. Only send a conversational
reply where one was requested. A recommendation for fresh context does not close,
archive, or reset this conversation; the orchestrator chooses the next assignment.
