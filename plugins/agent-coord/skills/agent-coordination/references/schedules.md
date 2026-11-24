# Scheduled prompts

When the user asks to schedule a prompt, use the `schedule_prompt` MCP tool.
Set `mode` to `self` to send a later message to your own Ribbon Field app
conversation, or `new` with workspace, client, model, and optional effort and
permissions for a new agent. Supply the future Unix time in seconds and an IANA
time zone; use a stable `request_id` to make retries safe. Set `yolo` only with
explicit authorization. The tool returns a request and schedule ID; `queued` is
not a saved schedule. Check `agent-coord thread request-status --request-id ID`
until confirmed, or inspect Scheduled prompts in the app. Self scheduling is
available for app conversations, not terminal sessions. The Mac must be awake
and Ribbon Field running at the scheduled time; missed one-time runs need user action.
Schedules support one-time runs or an explicitly authorized `recurrence`: use
`frequency` of `daily`, `weekdays`, `weekly`, or `monthly`, or `interval` with
whole `minutes` (1–525600). Calendar schedules keep their local time zone;
monthly runs use the last day in shorter months. Recurring runs never overlap,
skip missed occurrences, and can be managed through the agent surfaces below or Scheduled
prompts. An uncertain send stops the recurrence for review. Attachments are not
supported.

Manage schedules through the same `schedule_prompt` MCP tool: `action` is
`create` (default), `list`, `read`, `edit`, `pause`, `resume`, `cancel`, or
`run_now`. List returns schedules created by or targeting the caller, including
`id`, `version`, `state`, `recurrence_status`, `next_run_at`, settings (including
`yolo`) and `message_preview`. Read takes `schedule_id`; mutations also require
its current integer `version`. Edit accepts partial `message`, `run_at`,
`timezone`, `recurrence` and `settings`; use `recurrence: null` to remove repetition.
Existing-thread schedules retain their destination and use that thread's settings.
`run_now` applies only to missed prompts, matching the app's action.

The CLI uses the same contract:

```bash
agent-coord schedule list
agent-coord schedule read --schedule-id <id>
agent-coord schedule edit --schedule-id <id> --version <version> \
  --settings '{"yolo":true}' --yolo-authorized --request-id <unique-edit-key>
agent-coord schedule pause --schedule-id <id> --version <version> --request-id <unique-pause-key>
agent-coord schedule resume --schedule-id <id> --version <version> --request-id <unique-resume-key>
agent-coord schedule cancel --schedule-id <id> --version <version> --request-id <unique-cancel-key>
agent-coord schedule run-now --schedule-id <id> --version <version> --request-id <unique-run-key>
agent-coord schedule create --mode self --message 'Follow up' \
  --run-at <future-unix-seconds> --timezone America/Chicago --request-id <unique-create-key>
```

Creation and edit accept `--recurrence '<JSON>'` and `--settings '<JSON>'`;
new-agent creation requires `--mode new` and settings containing `cwd`, `client`
and `model`. `--message -` reads stdin. Use `--wait 0..30` (default 5), or MCP
`wait_seconds`, then inspect `thread request-status --request-id <key>` until
completed. Reuse the same request ID and identical arguments after a lost response;
never increment a version or invent a new key to retry an uncertain operation.
Creation and mutation receipts commit atomically with the schedule operation.

Caller identity is inferred and cannot be impersonated. Creators may manage
their schedules; targets may list/read schedules addressed to them. For another
creator's or a UI-created schedule, use `--authorized-schedule-id <same-id>`
(MCP `authorized_schedule_id`) only when the user explicitly instructs access
and names that schedule ID. This is the agent's explicit authorization assertion,
not a way to discover or modify unrelated schedules. Old schedules without
recoverable creator provenance also require this assertion.
Creating a new YOLO agent or editing any schedule that will execute with YOLO
requires explicit user consent for that execution plus `--yolo-authorized`
(MCP `yolo_authorized: true`). This includes changes to an already-YOLO schedule
and existing-thread schedules whose conversation currently uses YOLO. Disabling
YOLO requires no such assertion. Self schedules use their conversation's permissions
at delivery. An existing user instruction authorizing that edit and its full-access
execution is sufficient; do not ask again merely to populate the assertion.
Resuming or running another creator's YOLO schedule also requires the assertion;
pausing/cancelling requires no new YOLO assertion. The check uses the saved
schedule version verified by the mutation and the target's permissions when
processing the request. A target's permissions can still change independently
afterward; existing-thread schedules use its settings at delivery.
Creation/mutation receipts expose `authorization` with `sender_session_id`,
`authorized_schedule_id` and `yolo_authorized`. The durable app-request history
stores these with the exact submitted changes and final receipt. The schedule-ID
assertion follows the supported agent trust contract; it is not an independently
verified user capability. Version conflicts require a fresh read
and a new reviewed change. Pause/cancel prevent future recurring runs; a run
already in progress keeps its recorded outcome. No schedule-management command
bypasses browser CSRF checks or retries uncertain provider delivery.
