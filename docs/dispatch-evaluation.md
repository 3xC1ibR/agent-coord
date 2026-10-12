# Evaluate dispatcher routing during dogfooding

Dispatcher routing saves local evaluation records in the existing Agent Coord
SQLite database. This is automatic for requests handled by the updated app
runtime. Refreshing the CLI/plugin alone does not update a running native app.
No dashboard, telemetry upload, or automatic threshold tuning is added.

Each routing attempt records:

- The exact constructed Jev input: model, question/instructions, request context,
  shortlisted thread IDs, titles, checkpoints, messages, assignments, and state.
- Every eligible thread's retrieval rank and score, including those outside the
  shortlist. Workspace and dispatcher exclusions retain the existing semantics.
- Prompt, retrieval, and decision versions, policy thresholds, input hash,
  snapshot time, answer probabilities, confidence, decision, and elapsed time.
- Durable request and delivery receipts. Evaluation also reads subsequent LLM
  handoffs and provider completion state from saved records.

The input is saved **before** calling Jev. Interrupted attempts remain available;
another attempt appends a record. Evidence is kept out of normal conversation
receipts and LLM catch-up, so logging does not enlarge the routing prompt.
Records contain private conversation excerpts and are retained locally alongside
the conversation. Exported JSON contains those excerpts too; keep exports local
unless you intend to share them. Credentials and image bytes are not part of
evaluation exports. There is no automatic retention cutoff in this first version.

## Inspect and replay

```bash
agent-coord thread dispatch-evaluate --session-id DISPATCHER_ID

# Replay the same saved scores with proposed thresholds; no network calls.
agent-coord thread dispatch-evaluate --session-id DISPATCHER_ID \
  --confidence 0.60 --probability 0.65 --margin 0.20

# Inspect a single request, using its stable request_id from the report.
agent-coord thread dispatch-evaluate --session-id DISPATCHER_ID \
  --request-id REQUEST_ID
```

The result includes records, attempts, labels, outcomes, replay decisions, gate
results, aggregate metrics, and `next_cursor`. Follow that cursor with `--after`
using the same filters; the default page is 20 requests, maximum 100. Metrics are
for the returned page. `dispatch-history` has a separate **event** cursor;
`dispatch-evaluate` uses a **request** cursor.

Read-only evaluation opens SQLite in read-only mode. It does not initialize the
app store, consume dispatcher history, change production settings, submit queued
work, wake agents, or create conversations. Results go to stdout; saving stdout
to a local JSON file provides an export. Never redirect output onto the database.

Old requests retain their original IDs, scores, and outcomes, but show
`classifier_input_not_recorded` when exact input is absent. Explicit
"Ask dispatcher directly" and image fallbacks also lack a Jev input. A recorded
network failure or interrupted attempt with an input but no answer cannot replay
offline; it can be re-scored with `--live`. Missing evidence is never reconstructed
from newer thread context.

## Try a different prompt or model

```bash
# Re-score the saved input and historical instructions using current credentials.
agent-coord thread dispatch-evaluate --session-id DISPATCHER_ID --live

# Try the current baseline instruction variant on the same saved shortlist.
agent-coord thread dispatch-evaluate --session-id DISPATCHER_ID --live --policy baseline

# Substitute an experimental instruction file. Existing criteria stay intact.
agent-coord thread dispatch-evaluate --session-id DISPATCHER_ID --live \
  --instructions-file /absolute/path/instructions.txt
```

`--live` makes paid Jev API calls using the existing enabled configuration. It
still sends nothing to app threads and writes nothing to the database. An optional
`--model jev-MODEL` override also requires `--live`. New answers, evaluated inputs,
and latency appear only in the output; production evidence remains unchanged.
Without prompt overrides, replay uses the saved question rather than rebuilding
it from today's prompt template.

Replay freezes the original shortlist and recipient state. All decisions are
hypothetical, including delivery readiness. It uses the current decision function
and reports its version alongside the recorded version. Retrieval rankings allow
shortlist recall analysis, but changing shortlist size/retrieval requires a
separate snapshot experiment: excerpts for excluded threads were not stored.

## Record an expected destination

```bash
# A reviewer inference; this is the default source.
agent-coord thread dispatch-label --session-id DISPATCHER_ID \
  --request-id REQUEST_ID --action reuse --target-thread EXPECTED_THREAD_ID \
  --note 'Review of the request and thread history'

# Only use source=user when the user explicitly confirmed this destination.
agent-coord thread dispatch-label --session-id DISPATCHER_ID \
  --request-id REQUEST_ID --action reuse --target-thread EXPECTED_THREAD_ID \
  --source user --note 'User confirmed the intended thread'
```

Labels support `reuse`, `new`, and `dispatcher`; only `reuse` accepts a target.
They append to history without rerouting anything. The latest label from each
source is evaluated independently, keeping user-confirmed and reviewer-inferred
accuracy, wrong automatic routes, automatic precision, and retrieval recall
separate. A label saying `new` means new work was appropriate, not a particular
newly created thread identity.

LLM handoffs are linked by the exact dispatcher provider turn, never guessed by
message similarity or timestamps. Multiple requests sharing a turn are marked
`shared_turn` and excluded from simple LLM agreement counts. Multiple handoff
destinations likewise do not imply one correct destination. No LLM choice
automatically creates a label. Provider completion means the provider turn ended;
it does not establish task success or routing correctness.

Compare a fixed labeled set across variants. Use held-out cases before changing
production thresholds; agreement with an LLM alone is not proof of correctness.

## Latency measurements

New attempts include `evidence.timings_ms`: request-context reads, index refresh,
workspace snapshot loading, configuration loading, ranking/input construction,
pre-call evidence persistence, classifier round trip, and decision evaluation.
These timings stay outside the Jev input. `elapsed_ms` still measures planning
through the decision; it excludes final persistence, creation, and delivery.
Direct dispatcher/image bypasses record request-context time without index or
classifier stages. The classifier stage includes HTTP and response parsing; it
is not a measurement of model inference alone.

Receipts now store a separate `transition_at` timestamp. Evaluation exposes
`lifecycle_timings_ms` for intake wait, queue wait, provider submission, and total
intake-to-accept time when those transitions are available and unambiguous. Exact
single LLM handoffs also expose intake-to-handoff time. Acceptance is not a first
answer or task completion. Old events without timestamps and ambiguous retries
are omitted instead of being counted as zero. `summary.stage_latency_ms` gives
per-stage sample counts and p50/p95 over the returned page.

The runtime refreshes the existing conversation index only for changed histories
in the request workspace. Its bounded routing projection preserves message order,
duplicate IDs, and the previous 200-message/6,000-character limits. Independent
history triggers invalidate this projection even when an older runtime writes
history or refreshes its search index. Metadata and delivery guards remain live.
Standalone snapshots stay read-only and parse saved history for missing or stale
cache entries. The first index backfill can still be slower than warm routing;
no threads are excluded for age, and policy thresholds and shortlist size are
unchanged.
