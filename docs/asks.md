# Asks: items agents address to the user

**Status:** Stage 1 specification, proposed 2026-11-22. The direction was agreed
with the owner in thread `1506df07-ad54-41cc-b6d3-118e2719268d`; this document
awaits his review before implementation is merged.
Stage 1 is implemented on branch `asks-stage-1`; the [decision record](#decision-record)
lists where the implementation departs from or adds to this specification.

## Problem

Asks are the durable records behind the **Decisions** dialog
(`agent-coord ask`, the `ask_human` tool). Observed in the production database
on 2026-11-22:

- 24 asks have been created since October. One was answered through the
  dialog; 17 were withdrawn; 6 are open, one of them for 12 days.
- Most asks are not questions. "Upload your ID photo to Stripe" or "add payout
  billing details in Apify" is work the user does elsewhere. An ask has no
  completed state, so the agent withdraws it after noticing the work was done,
  or rewrites it repeatedly.
- The dialog sits behind a question-mark button. The owner forgot it existed.
- Agent guidance restricts asks to "a decision only the user can make".
  Nothing invites an agent to request a tool, dataset, account or budget that
  would materially improve its result, and agents rarely do.

## Stage plan

1. **This stage: asks.** Kinds, lifecycle, fields, a primary view, guidance and
   measurement.
2. Reports and artifacts appear in the same view.
3. Explicit asks and user-owned checkpoint steps stop generating thread
   attention; thread attention keeps conversation state only.
4. The response classifier becomes a backstop that counts replies asking
   something of the user without a filed ask.

## Stage 1 boundaries

Unchanged in this stage:

- Thread attention derivation (`codex_app_server.py`). An open blocking ask
  still marks its thread exactly as today, through `AskStore.open_blocking`.
  A regression test pins that output.
- Checkpoint `next_step`, the response classifier, and the notification policy.
  Open blocking asks keep their existing one-time alert claim.
- No integration with Stoic or any other external service.

## Model

### Kinds

| Kind | Meaning | Resolutions |
| --- | --- | --- |
| `decision` | Choose between options or answer a question. Default; existing asks migrate here. | `answered` |
| `action` | Do something only the user can do, usually elsewhere. | `done`, `not_done` |
| `approval` | Permit something beyond the agent's authorization. | `approved`, `declined` |
| `review` | Read, verify or try a result. | `accepted`, `changes_requested` |
| `request` | Provide a tool, dataset, account, credential, budget or access that would materially improve the result. | `granted`, `declined` |

Every kind also accepts `dismissed`: the user closes it without acting, for
example because it is stale. Each resolution may carry a free-text note.
Decisions keep option and free-text answers.

### Fields

Existing: `question` (shown as the title), `context`, `options`, `blocking`,
`supersedes`, timestamps, notification claim.

New, all optional except `kind`:

| Field | Purpose |
| --- | --- |
| `kind` | One of the five kinds. |
| `unblocks` | What resolving it enables. For a request: the expected gain and what the agent does without it. |
| `effort_minutes` | The agent's estimate of the user's time. |
| `link` | Where the user acts: an HTTPS URL, an `agentcoord://` link or an absolute file path. |
| `due_at` | A real external deadline, not an agent-invented target. |
| `project` | Grouping label; defaults to the asking thread's project. |
| `request_id` | Idempotency key. The same session and key return the existing ask. |
| `resolution`, `note`, `resolved_at` | The user's outcome. |
| `snoozed_until` | Hidden from the open list until this time. |

### Status

`open`, `resolved`, `withdrawn`. Existing `answered` rows migrate to `resolved`
with resolution `answered`; readers continue to accept the legacy value during
migration. Duplicate prevention: besides `request_id`, creating an ask whose
normalized title matches an open ask from the same session returns that ask.

### Derived states (computed, not stored)

- **Stale:** the asking thread is closed or archived, or the asking session
  has recorded no activity for 72 hours.
- **Overdue:** `due_at` has passed.
- **Snoozed:** `snoozed_until` is in the future. In stage 1, snoozing affects
  only the asks view; a snoozed blocking ask still marks its thread.

## Delivery

Resolving an ask inserts the existing self-addressed `action_required` message
in thread `ask:<id>`, so idle app conversations wake through the current inbox
path. The body states the kind, resolution and note. For `done`, it asks the
agent to verify the result before relying on it. When the asking thread is
closed, the view says so before the user resolves the ask.

## Interfaces

**CLI**

```bash
agent-coord ask --kind action --blocking \
  --unblocks 'Pricing for all 18 verified actors.' --effort 10 \
  --link 'https://console.apify.com/billing' --project 'HoldCo' \
  --request-id apify-payout-1 'Add payout billing details in Apify'
agent-coord ask --kind request --non-blocking \
  --context 'Default: continue with the public endpoint at 10 requests per minute.' \
  --unblocks 'A paid key removes the rate limit and cuts the crawl from 9 hours to 40 minutes.' \
  'Provide a SerpAPI key'
agent-coord ask --show <id>
agent-coord ask --list [--all-sessions] [--status open|resolved|withdrawn] [--since <unix>]
agent-coord ask --metrics [--since <unix>]
```

`--all-sessions` is read-only; it lets an orchestrator see asks already filed
by other agents before filing its own.

**MCP:** `ask_human` gains `kind`, `unblocks`, `effort_minutes`, `link`,
`due_at`, `project` and `request_id`. Its description covers all five kinds.

**Browser API**

- `GET /api/browser/asks?status=open|resolved|all&since=` returns display
  fields plus stale, overdue and snoozed flags.
- `POST /api/browser/asks/<id>` with `{resolution, option?, answer?, note?}`.
  The legacy `{option}` and `{answer}` bodies remain valid.
- `POST /api/browser/asks/<id>/snooze` with `{until}`.

## View

A primary sidebar destination named **Queue**, above the thread list, with an
open count that turns red when any item is blocking. It replaces the dialog;
the question-mark button opens the view.

- **Open:** blocking first, then overdue, then grouped by project. Each row
  shows the kind, title, `unblocks`, effort, age, the asking thread, an Open
  control for `link`, and resolution controls for that kind. Stale and overdue
  states are labelled.
- **Snoozed** and **Resolved**. Resolved is the decision log, filterable by
  project, kind and date.
- Resolution happens in place. Unsaved input is preserved on failure.
- The same view works at phone width.
- **In a conversation.** A conversation shows its own asks in a strip above its
  composer: the open ones with the same resolution controls, note, Snooze and
  Dismiss, and its resolutions of the last 30 days folded away beneath them.
  The strip is absent when the conversation has neither.
- **While the navigation is closed** (a phone, or a collapsed sidebar), the
  navigation toggle carries the open count.
- Follow `DESIGN.md`. Reference apps: Linear Inbox for triage and Things for
  the action list. The owner may change these references.

## Agent guidance

Replace "Ask the user a decision" in the agent-coordination skill with "Ask the
user". It covers when to use each kind and requires `unblocks` for actions and
requests. Add: when a tool, dataset, account, credential, budget or access
would materially improve the outcome, file a `request` stating the expected
gain and the default without it; do not limit the work to the resources at
hand. When the user must act, file an ask instead of only writing it in a reply.
Update `docs/reference.md` and the `ask_human` description to match.

## Measurement

`agent-coord ask --metrics` reports, for a period:

- median and 90th-percentile time from creation to resolution, by kind;
- share of closed asks resolved by the user versus withdrawn by the agent;
- supersede chains per distinct need;
- requests filed and the share granted;
- blocked hours: the summed open duration of blocking asks;
- currently stale asks.

The baseline is the 24 asks recorded before this stage.

## Acceptance

- The six currently open asks appear in Queue as decisions, with the 12-day ask
  marked stale.
- An action filed through the CLI is marked done in the view; the asking app
  conversation receives the message and wakes.
- A request is granted with a note; the agent receives the note.
- Thread attention output for a fixed fixture is identical before and after.
- `python3 -W error::ResourceWarning -m unittest discover -s tests -v`, the web
  tests and `scripts/measure_database_contention.py --check` pass.
- Desktop and phone-width screenshots of Open, Resolved and an empty Queue.

## Decision record

Choices made while implementing stage 1 on 2026-11-22, with the reason for
each. Items marked **deviation** differ from the text above; the rest settle
points the specification left open.

### View

| Decision | Reason |
| --- | --- |
| The view opens from the sidebar entry in a workspace dialog that uses the auxiliary-page grammar of Usage and Activity, and fills the screen on narrow layouts. It is not a new region of the workspace shell. | It reuses an existing surface type instead of adding one and keeps the change out of the workspace's view state. The iOS app wraps the same web interface, so it gets the view too. |
| The whole view ships in `web/asks.js` and `web/asks.css`. There is no separate page, route or script URL. | After an interface install, the retained backend serves the new web files through its existing asset list until its update is applied. A new URL would return 404 in that window and break the workspace; these two URLs are already served by every installed backend. |
| Against a backend from before kinds, the view shows decisions only and answers them with the legacy bodies. It hides notes, snooze, dismissal and the log, and says so. It learns this from `askKinds` in the browser config. | Open asks, including blocking ones, stay answerable between the interface install and the backend update, however long that takes. |
| The name "Queue" appears only in the copy block at the top of `web/asks.js`. Files, identifiers and tests use `asks`. | The name and the reference apps are defaults the owner has not confirmed; renaming the view is a one-line copy change. |
| **Deviation:** the open count moved from the question-mark button to the sidebar entry. The button stays and opens the view, without a count. | Two counts for one list duplicate an affordance. |
| The affirmative control of each row is the standard button, not the primary button. | A list has no single principal action; a primary button on every row would spend the view's emphasis many times over. |
| The note field of an action, approval, review or request appears when the user chooses **Add note**. A decision's answer field is always shown. | An image-only design review found the always-present optional field dominating every row. Most resolutions carry no note; one click reveals the field. |
| Red marks only the Blocking group and the sidebar count. Overdue and stale use the attention colour; resolved outcomes are muted. The link sits in the row's meta line, next to the title. | The same review found red used four ways and the link far from the title. The specification's red count is the sidebar entry's. |
| An ask whose text has several lines leads with its first line; the remainder and a long context start collapsed behind **Show more**. | The six open production asks carry whole instruction lists in the title. Without this one ask fills the screen. |
| Typing text and then choosing a decision's option sends the text as the note. Enter in a note field never chooses a resolution. | The typed text is not lost, and a resolution is always a deliberate click. |
| Snooze offers four fixed choices (1 hour, 3 hours, tomorrow 9 AM, next week). | It matches the thread snooze choices; a free date can follow if the fixed set proves too narrow. |
| A link that is an `.html` file path opens in the existing HTML output viewer; any other file path offers **Copy path**. | The browser cannot open local files. The viewer already exists for HTML results. |
| The Resolved section lists user resolutions only. Withdrawn asks remain available from the API and CLI (`--status withdrawn`). | Resolved is the user's decision log; an agent's withdrawal is not the user's decision. |
| The Resolved date filter applies to the resolution time and runs in the page. | The API's `since` selects by creation time, which would hide an old ask resolved today. |
| A conversation's strip (added 2026-11-22, after stage 1) reuses the view's rows and resolves through the view's data, so the count, the view and the strip never disagree. It ships in the same `web/asks.js` and `web/asks.css`; its element and the toggle count are in `index.html`. | The owner could not see a thread's asks in the thread. One row builder keeps the kinds' controls identical in both places, and no new URL keeps the change safe before a backend update. |
| The strip starts unfolded while an ask waits and folded when the conversation has only snoozed asks or resolutions. The user's folding choice is kept per conversation until the page reloads. Its height is capped at 35% of the window (55% of the conversation on a phone; 75% while a phone keyboard is open on one of its fields) and it scrolls inside that. | An ask waiting in the open conversation is the reason to look; history is one muted line. The cap keeps the conversation readable and the composer in place. |
| In the strip one ask is shown in full: the first in order, or the one the user opened by its title. The others show their title and meta line. The ask in full does not change while it stays open, even when a more urgent one arrives. Folded, the strip's line gives the count, the blocking count and the first ask's title. | An image-only design review found that two full rows hid the second ask's controls behind the composer with no sign it could scroll, and on a phone cut the first ask's controls. One ask in full fits the cap; a row must not move under a half-typed answer. |
| Review findings not adopted: a card surface and inset for the strip; a filled primary button; moving Answer into the field and Snooze and Dismiss into an overflow menu; a tappable count row on the phone overview. | The contract uses dividers, not cards, and no primary button per row. The strip must keep the view's controls. The overview row is a larger change than the count on the toggle and can follow if the count proves too quiet. |
| The strip selects its thread's asks in the page from the two existing listings. The resolved log is read when a conversation opens, at most once a minute, and again when one of the conversation's asks leaves the open list without a local resolution. | No backend change was allowed, and the store's `session_id` selection is not exposed by the browser API. The log is at most 500 rows and changes rarely, so a per-thread read is not worth a new parameter yet. |
| In a conversation, a row carries the Blocking flag and does not name its thread. Snoozed asks stay listed, after the others, with **Show now**. | The view's Blocking heading and thread link are absent or redundant there. A person who opened the thread should still find an ask they postponed. |
| The count on the navigation toggle shows only while the toggle reports the navigation closed. It is the toggle's description for assistive technology; the toggle keeps its own name and action. | The sidebar entry is the count's home. On a phone that entry is inside the drawer, which is how the earlier dialog went unnoticed. |

### Model and delivery

| Decision | Reason |
| --- | --- |
| **Deviation:** the store rejects an `action` or `request` without `unblocks`, and options on any kind but `decision`. | A rule the store enforces holds for every client; guidance alone does not. |
| **Deviation:** a legacy `{option}` or `{answer}` body sent to a non-decision ask is rejected with the resolutions that kind accepts. | "Answered" is not an outcome of an action, approval, review or request; the note carries free text for those. Legacy bodies still answer decisions. |
| Dismissing an ask also delivers a message. It tells the agent not to assume the thing happened. | Dismissal is a resolution; an agent that never hears of it would wait or ask again. |
| Stale uses the thread's Closed state or 72 hours without session activity. It does not use the session's ended time. | App conversations end their process between turns and remain resumable, so an ended session is not evidence of abandonment. |
| A repeated `request_id` returns its ask whatever its status. A matching title returns only an open ask. `--supersedes` is not matched against the ask it replaces. The returned record carries `"existing": true`. | A retry must be idempotent after resolution too; a resolved need may legitimately be asked again; a rewrite with the same title is an intended replacement. |
| Records keep `answered_at` beside the new `resolved_at`. | Readers written before kinds continue to work during migration. |
| A due date without a time means the end of that local day; a time without an offset is local. | That is how the user reads a date. |
| `--status` and the API also accept `withdrawn` and `all`; `--since` selects by creation time. | An orchestrator checking for earlier asks needs the closed ones too. |
| `--show`, `--metrics` and `--list --all-sessions` need no caller session. | They are read-only inspection, useful from a terminal and for measuring a database copy. |

### Measurement

| Decision | Reason |
| --- | --- |
| The 90th percentile uses the nearest rank. | Interpolation invents values on samples as small as these. |
| Closure separates superseded withdrawals from withdrawals without a successor. | A rewrite and an abandoned ask are different failures. |
| Stale asks are the current state of every open ask, whatever the period. | Staleness is a property of now, not of the period in which an ask was created. |
| Blocked hours count an open blocking ask up to the time of measurement. | An unanswered blocking ask is the most expensive case and must not read as zero. |

### Unchanged in stage 1

Thread attention text still reads "Decision needed:" for a blocking ask of any
kind, because that derivation is frozen until stage 3. Resolving an ask is not
written to the Activity ledger, as before. The runtime schema version is
unchanged: the migration only adds columns and renames one status value, the
earlier code reads the migrated table, and a higher schema version would forbid
rolling the backend back.
