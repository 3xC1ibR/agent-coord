# Asks: items agents address to the user

**Status:** Stage 1 specification, proposed 2026-11-22. The direction was agreed
with the owner in thread `1506df07-ad54-41cc-b6d3-118e2719268d`; this document
awaits his review before implementation is merged.

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
