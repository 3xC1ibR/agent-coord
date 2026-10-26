# Request review

Ribbon Field can review a **text answer or plan** before presenting its result.
This is an opt-in experiment for a single request, intended to measure useful
corrections and repeat use before expanding into artifact or action workflows.

In an idle conversation, choose **Review**, choose an available critic provider
and model, and edit the review standard if needed. The default uses the current
provider and model and asks for material errors, missed requirements, unsupported
claims, and consequential assumptions. Send the task normally. Review turns off
after that request; configuration commands do not create reviews.

The conversation shows Drafting, Reviewing, Revising, and Rechecking. A clean
initial review ends early. Otherwise the generator addresses every finding and
the critic rechecks those findings once. Expand the review card to see both draft
versions, evidence labels, accepted or rejected feedback, and unresolved issues.
The critic has its own linked conversation. Approvals or questions there must be
answered there. **Stop** ends the review; a new direct message supersedes it.
Queueing another message during review is rejected with an explanation.

## Contract and limits

- The packet preserves the original request and selected reference expansion,
  previous user messages (including earlier reviewed requests), ancestor
  `AGENTS.md` contents, and the explicit standard. It states its coverage. It does
  not claim that referenced files, nested instructions, or external resources
  were inspected. The generator still has its own conversation context.
- The critic may report no material findings or insufficient evidence. Each
  finding has an ID, issue, supporting evidence, evidence status (`checked`,
  `inferred`, `unverified`), and proposed correction/check. These are agent
  assessments, not application-certified truth.
- The generator accepts, rejects, or leaves each finding unresolved, with a
  reason. The recheck addresses every prior finding by ID and can flag material
  regressions. Agreement never counts as independent verification.
- Maximum four provider turns: draft, review, revision, recheck. Maximum ten
  minutes from submission, including creation and approvals. There are no
  automatic extensions, format-repair turns, or retries after uncertain sends.
- Context packets are limited to 40,000 characters, drafts to 24,000, and stage
  prompts to 100,000. Oversized material is rejected or ends review incomplete;
  it is never silently truncated. These limits are not token or dollar caps.
- A completed exchange can retain disputed or unresolved findings. The UI says
  so. Failure, timeout, cancellation, or an invalid/stale record leaves the draft
  provisional; the app does not silently publish an unreviewed answer.

Review gates **response delivery**, not tool execution. Both agents are instructed
to produce text/plans and avoid changes or external actions. The critic starts
with normal approval permissions, but provider permissions differ and this is
not an enforced read-only sandbox. Tool activity and approval requests remain
visible. Do not use this feature as pre-action approval, a deployment gate, or
proof that a changed workspace is safe.

## Implementation

`scripts/agent_coord/reviews.py` owns SQLite `review_runs` and `review_steps`.
Each run snapshots input, owns a stable request ID, and records its participants,
deadline, state, draft hash, exact message/turn bindings, and structured results.
Run transitions and next-stage messages commit together. Duplicate identical
request IDs return the existing run; changed payloads with reused IDs fail.

The existing `browser_wake.py` claims durable messages before submitting turns.
During review it claims only the expected stage and injects that stage's packet
directly into the provider prompt, so critics do not require a coordination tool
or native steering support. Codex, Claude Code, Kimi, Muse, and Antigravity use
their existing adapters, settings validation, approval UI, and interruption
paths. Review protocol support does not imply equal factual quality or equal
permission enforcement across providers.

`codex_app_server.py` retains raw provider history internally. Public response
events/history replace stage messages with review markers. Tool events remain
visible. Stage completion notifications are suppressed; one synthetic completion
receipt signals the reviewed result or incomplete exchange. Explicit subscriptions
to a provider turn still describe that turn's completion, not task success.
`web/reviews.js` renders the request, stage progress, final result, and inspectable
provisional material from the review record. `browser_queue.py` prevents queued
work from overtaking a review. `ui.py` starts the worker and exposes cancellation.

After restart, recorded completions and exact saved histories advance at most
once. Already-claimed submissions are not replayed. A provider creation or send
whose acceptance cannot be established stops or expires incomplete; resuming
such work requires a new explicit request. Cancellation becomes durable before
interruption, so a late completion cannot schedule another stage. New input
does not reuse a previous review's findings or budget.

This launch slice does not include attachment review, immutable repository
snapshots, enforced tool restrictions, pre-action checks, multi-critic voting,
continuous monitoring, custom workflow graphs, per-run monetary limits, or
automatic retries. Sidecar watchers remain a separate post-action feature.

## Validation and adoption experiment

Automated tests cover clean/contested reviews, exact draft binding, malformed
records, missing dispositions, deduplication, cancellation, superseding input,
timeouts, uncertain sends, restart recovery, routing isolation, publication, and
all five provider adapters using simulated transports. Browser tests cover
provisional versus final presentation, chronology, safe text rendering, stable
retry keys, and provider choice. These tests establish mechanics, not that a
critic improves answers; live model quality remains an evaluation question.

Before promoting the feature, use 20 representative text/plan tasks with explicit
constraints and independently checkable rubrics. Compare generator alone,
generator with self-revision, and generator plus critic with the same total
token/cost allowance (and report any incomplete runs). Randomize the presentation
and have a reviewer blind to the condition score useful corrections, introduced
regressions, material missed constraints, and unsupported claims. Record elapsed
time, provider usage/cost where available, and missing cost coverage separately.
Do not score agent agreement as success.

Ship more broadly only if the critic provides additional material corrections
over self-revision without increasing serious regressions and users accept its
latency/cost. Invite a small cohort to choose review on real tasks, then observe
whether they choose it again on a later eligible task and why. Review records
retain timestamps, chosen critic identity, outcomes, and step counts for a local
audit; existing provider usage remains attached to each conversation. This slice
adds no external analytics or claims of measured quality improvement.
