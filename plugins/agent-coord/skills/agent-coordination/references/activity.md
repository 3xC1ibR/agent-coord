# Retrospective activity

Use this evidence when the user asks where their day went, what they worked on,
or how to improve assignments and coordination. Ordinary workers do not need to
load it routinely. Activity observes interactions in Ribbon Field; it does not
measure mental attention or productivity.

## Read the evidence

The Activity dashboard, `query_activity` MCP tool, and CLI share one query:

```bash
agent-coord activity query --start 1793145600 --end 1793232000 --group-by project
agent-coord activity query --start 1793145600 --end 1793232000 --domain web
agent-coord activity history --thread-id THREAD_ID
agent-coord activity settings
```

Times are Unix seconds, with an inclusive start and exclusive end. Compute local
calendar boundaries using the user's timezone, including daylight-saving changes.
Omitting times selects the last 24 hours. Group by `project`, `domain`, `group`,
or `thread`; filter by project path, domain, group ID, or thread ID. Grouping uses
current organization; each raw event also preserves its organization snapshot.
Use `--workspace` (or the tool's `workspace` field) to preserve a workspace-limited
dashboard's boundary, including linked worktrees. Generated review prompts carry it.

Follow `next_cursor` with the same filters until it is null. Totals cover the
whole selected range, independently of raw-event page size. Changed organization
or cleared history may invalidate a cursor; restart the query in that case.

Use `read_activity_history` or `activity history` to page saved conversation
prose and durable dispatcher, review, queue, and coordination records without
activating its provider. Each record identifies its source; delivered input can
appear in multiple sources, so use activity events for counts. Large messages span chunks identified by
`text_offset` and `text_complete`; preserve all chunks. Checkpoints and artifact
links accompany the history. `coverage` states whether saved history is present
or partial. Provider `user` roles alone do not establish human origin. Messages
with unknown timestamps are returned as context, not proof of activity in the
selected period.

## Interpret carefully

Start with raw events and compare plausible explanations. A return without a
message may mean reading a useful answer, checking status, or interrupted work.
Many small requests may be coherent independent work or symptoms of a broader
problem. Inspect the relevant conversations and linked changes before suggesting
a dispatcher, an audit, or a different assignment boundary.

- `message_submitted` records an accepted human app input once, including queue
  acceptance. Later queue delivery, inbox wakes, and inter-agent assignments do
  not add human messages.
- `visit_started` / `visit_ended` are observed foreground visits. Automatic restore
  and programmatic selection are separate from human navigation and focus.
  Navigation visits recorded after this field was added carry `details.surface`:
  where the selection began, such as `sidebar`, `cards`, `working-set` (Focus),
  `atlas`, `search`, `tabs`, `panes`, `conversation`, `notification` (web
  notification click), `shortcut`, or `other`. Native app notification clicks
  and app links still arrive as programmatic selections without a surface.
- `decision_submitted` records explicit app decisions such as an approval, stop,
  or command. `organization_changed` retains organization edits.
- Counts use human-origin observations. Missing visit ends remain incomplete;
  they are not silently converted into attention duration.
- Coverage includes recording start, pauses, and clearing. Existing work threads
  are not backfilled into invented historical visits.

Report observations separately from interpretations. Cite event IDs, threads,
turns/messages, and artifacts so the user can inspect the basis. Prefer a few
concrete experiments or assignment briefs over a productivity score. Do not
reorganize work, modify code, or launch additional agents merely because an
activity review suggests doing so.

## Local controls and review launch

The dashboard supports pausing recording and clearing local activity. Clearing
activity does not delete conversations. Telemetry stays in the local coordination
database; launching a review gives the selected agent access to evidence through
the same tools and CLI. Evidence it reads becomes context for that agent's
configured provider. There is no separate analytics service.

Review creation uses a stable request ID and a fixed period cutoff. A transport
retry returns the same review. An uncertain startup preserves any created thread
and asks the user to inspect it instead of automatically creating another agent.
