# Responsiveness

Ribbon Field must feel immediate while many agents run. This document records
the latency budgets, how they are measured on real data under agent load, what
caused the October 2026 slowness, and the bottlenecks that remain.

## Budgets

| Interaction | Budget | Where it is checked |
| --- | --- | --- |
| Enter → own message visible | p95 ≤ 50 ms, independent of the backend | Browser driver; `tests/test_web_outgoing_messages.js` |
| Send acceptance (`POST …/messages`) | p50 ≤ 150 ms, p95 ≤ 400 ms | Load harness `--check` |
| Sent message reaches other windows | p50 ≤ 150 ms, p95 ≤ 400 ms | Load harness `--check` |
| Thread list | p50 ≤ 300 ms, p95 ≤ 800 ms | Load harness `--check` |
| Conversation read / thread detail | p50 ≤ 300 / 100 ms, p95 ≤ 800 / 300 ms | Load harness `--check` |
| Sidecar status, explicit reads and independent window polls | p50 ≤ 100 ms, p95 ≤ 300 ms | Load harness `--check` |
| Search | p50 ≤ 300 ms, p95 ≤ 800 ms | Load harness `--check` |

Server budgets apply to the realistic profile below. A sent message shows
"Sending…" until the backend accepts it, then remains until the conversation
shows the provider's own copy. A rejected message stays visibly "Not sent" with
the reason, and its text returns to an empty composer. Pending state is never
presented as delivered, and a slow backend is not hidden behind a longer timeout.

## Measuring

Both tools use real data and never touch the installed runtime unless pointed at it.

```bash
# Backend under agent load: repository code on an APFS clone of a snapshot.
python3 scripts/measure_backend_latency.py --snapshot PATH/state.sqlite3 \
  --agents 6 --delta-rate 6 --hook-rate 3 --check --output report.json
# The same workload against another backend tree, e.g. a retained payload:
python3 scripts/measure_backend_latency.py --snapshot PATH/state.sqlite3 \
  --source ~/.local/state/agent-coord/.state.sqlite3.runtime/payloads/<digest>/backend
# Keep the loaded server running, then drive the real web UI in a browser:
python3 scripts/measure_backend_latency.py --snapshot PATH/state.sqlite3 --sends 0 --reads 0 --hold 600
node scripts/measure_interaction_latency.mjs --base http://127.0.0.1:PORT --thread ID --sends 15 --switch ID1,ID2
```

Snapshot a live database with Python's `sqlite3` backup API (`src.backup(dst)`);
the `sqlite3` shell's `.backup` restarts on every write. The harness replaces
provider processes with in-process synthetic providers that stream JSON-decoded
events with realistic transcript sizes, emits the provider notifications that
make windows refresh, runs three windows that refresh exactly like `app.js`,
and runs a hook writer process. Its `--profile` option writes a Python
stack-sample profile of the backend. `/usr/bin/sample` on the live backend PID
shows native GIL and SQLite waits without stopping it.

Run measurements while the machine is otherwise quiet: concurrent builds and
test suites raise load averages above 50 on the development Mac and move every
percentile.

## October 2026 findings

Live, before any change (backend build 10, about twenty agents): Enter → own
message visible p50 1.39 s, p90 2.29 s, max 2.77 s; acceptance p50 1.80 s, max
3.13 s (12 sends, real browser). Search took 0.9–4.3 s and the thread list was a
5.4 MB response.

Causes, each measured:

1. **The UI drew a sent message only after the backend accepted it**, and the
   backend accepted only after a whole-transcript provider read and the
   provider's `turn/start`. Any backend delay was therefore input lag.
2. **A thread-list refresh storm.** Each window rebuilt the full inventory
   (555 threads, about 130 ms of mostly GIL-bound work idle, over 1 s under
   load) on nearly every provider event: about 1.7 refreshing events per second
   per window live, including `mcpServer/startupStatus/updated`. Three windows
   kept several builds in flight continuously. Native sampling of the live
   backend found an average of 7 threads queued on the GIL and about 25
   short-lived request threads per second; every user request waited in that queue.
3. **Claude Code restarted before most messages.** Its process was restarted
   whenever developer instructions changed, and those embed the thread's saved
   checkpoint, which agents update nearly every turn. Each message then waited
   for a Claude Code start with plugins, MCP servers and session hooks. The
   same context already reaches every prompt through the UserPromptSubmit hook.
4. **Per-row Python work inside SQLite** (search UDFs, row iteration) and an
   unindexed agent-wake join in every list build, each multiplied by GIL convoy.

Changes:

- `OutgoingMessages` in `web/app.js`: local echo on Enter with Sending/Sent/Not sent.
- Send skips the whole-transcript read for a loaded idle conversation; a
  conversation believed to be running still reconciles first.
- One shared thread-list build serves all concurrent readers and is reused
  for at most one second unless this runtime changes a thread; app-agent
  creators are shared the same way for display projections only.
- Windows refresh the list at most once per second and ignore events that
  cannot change it.
- Claude Code keeps running when only its instructions' saved context changes.
- An index for agent-woken turns; search without per-row Python callbacks.

Harness results, realistic profile (6 streaming agents, 3 windows, hook
writer), installed build 10 backend versus this repository, same snapshot:

| Metric | Before p50 / p95 / max | After p50 / p95 / max |
| --- | --- | --- |
| Send acceptance | 420 / 953 / 1734 ms | 100 / 175 / 258 ms |
| Sent message reaches windows | 392 / 864 / 1713 ms | 53 / 142 / 151 ms |
| Thread list | 1162 / 2417 ms | 111 / 508 ms |
| Backend CPU | 93 % | 49 % |

In a real browser against the loaded harness, Enter → own message visible was
3–11 ms across 15 sends (live before: p50 1.39 s).

Live, after installing interface build 15 with the build 10 backend still
active (12 sends, same probe conversation): Enter → own message visible p50
1.9 ms, max 7 ms; acceptance p50 0.96 s, max 3.5 s, shown as "Sending…" until
the staged backend activates. Reopening a cached conversation in another
project painted in 122–303 ms (before: p50 0.74 s, max 2.2 s in the harness).

The running backend serves only the web files its own `ui.py` lists, so a new
script file stays unavailable until a backend handover. Keep interface changes
that must ship without one inside files the running backend already serves.

Budget gate on the committed code (realistic profile, `--check`): send
acceptance p50 23 ms / p95 230 ms, delivery to other windows p50 7 ms, list
p50 3 ms, conversation read p50 35 ms, thread detail p50 8 ms, search p50
93 ms. Only search p95 failed, from the first search on a fresh database
building its context cache once (1.17 s); later searches took 77–131 ms.

Follow-up, build 17 (interface live, backend staged): opening a cached
conversation in another project now paints at p50 68 ms, max 114 ms on the
live app, because the overview list is redrawn after the conversation's frame
instead of before it (it took 70–310 ms). A first, uncached open of a 75-turn,
1.5 MB conversation enables Send with its history shown at p50 530 ms, max
703 ms on the new backend under the realistic profile; the remainder is
transferring and rendering the whole history.

Validating startup on a fresh snapshot found that one interrupted conversation
creation ("database is locked" after its browser session row was saved) left
a row without a registered session, and every runtime start, including the
pending handover, would have failed on it. Startup now skips such a row, and
creation removes it when registration fails.

Live, after the idle handover activated build 17's backend (payload
`46cfe859`, new runtime instance; startup logged and skipped the orphaned
conversation row), same probes as before:

| Interaction | Before (build 10/11) | After (build 17) |
| --- | --- | --- |
| Enter → own message visible | p50 1.39 s, max 2.77 s | p50 1.6 ms, max 2.7 ms |
| Send acceptance | p50 1.80 s, max 3.13 s | p50 26 ms, max 59 ms |
| Search (30 queries) | 0.9–4.3 s | p50 83 ms, p95 93 ms, max 100 ms |
| Conversation read | 100–200 ms | 15–40 ms |
| Reopen in another project | p50 0.74 s, max 2.2 s | p50 17 ms, max 77 ms |
| First open, provider resumed | up to 1.5 s | Send enabled p50 251 ms, max 1.23 s |

The first search after the upgrade built its context cache once (2.1 s).

## Resource and request bounds (October 29 campaign, I15–I19)

Measured on the same day, build 17:

- **Idle conversations retained processes.** Runtime 98754 grew from 136
  processes and 1.86 GB RSS (16:55Z) to 161 and 2.89 GB (18:09Z), mostly one
  agent-coord MCP, mcp-grafana and node_repl server per loaded conversation.
  Nothing unloaded a conversation. `conversation_residency.py` now keeps busy
  conversations and the six most recently used idle ones; others are released
  after 10 idle minutes (checked every minute). Busy means a running or
  starting turn, pending input, unsaved history, queued or due inbox work, an
  active review, an active delegation child, or a declared write scope.
  Releasing a Claude conversation stops its CLI and MCP servers; opening it
  stays offline, and typing in it starts Claude Code before the message is
  sent. Releasing a Codex conversation unsubscribes it: an isolated Codex
  0.165 app-server closed an unsubscribed idle thread and its MCP servers
  after about 30 minutes (`thread/closed`; one probe, so not a guaranteed
  deadline). A persisted conversation reopened with its history intact after
  unsubscribe and immediate resume, and after a cold app-server restart. A
  released conversation shows as saved, as after a runtime restart, and
  resumes on open or send. Eligibility is checked again under the
  conversation's lock just before release, so a conversation opened or given
  work after selection stays loaded.
- **What is bounded.** The app enforces a retention policy, not a process or
  memory limit. It keeps loaded every protected conversation and six warm
  idle ones, and releases any other idle conversation within about 11 minutes
  of its last use or of becoming eligible (10-minute idle period and a
  1-minute sweep). A released Claude conversation's CLI and MCP servers stop
  at release. A released Codex conversation stays loaded in the app-server
  until Codex closes it; that took about 30 minutes in the one probe, and the
  app does not enforce a deadline. Active work and per-server MCP memory are
  not limited.
- **Requests per window.** One live build-17 window made 131 requests/min on
  the overview and 188 with a conversation open (about 146 MB/min of thread
  lists): every list refresh fetched groups, saved views, decisions and
  schedules too, about 25 times a minute. WebKit retains memory per request
  (about 5.5 KB in the lab); the live Networking process grew about 4 MB/min
  while several windows were open. The list is now sent as cards without
  detail-only fields (5.96 MB to 2.42 MB live) with an entity tag; a window
  keeps its list on 304; groups and views are read at most once a minute and
  decisions and schedules every 15 s in the background; event-driven list
  reads are at most every 2 s. Matched snapshot measurement, one real window:
  overview 64 to 24.7 requests/min, open conversation 100 to 60.7; list
  transfer about 69 to 29 MB/min. This slows WebKit's growth; it does not
  bound it. Reopening the interface (ordinary Quit; agents keep running)
  starts a new Networking process.
- **Opening a conversation** transfers its recent view: the latest 40 turns,
  with tool fields in completed turns shortened to 512 characters and lists
  to 10 entries (marked `abridged`). Messages, reasoning, plans and a running
  turn stay complete. The view is built per response from a copy; the saved
  history is unchanged. Earlier turns, "Show full output", search results and
  message links read the complete history, which the conversation then keeps
  while cached. Older backends ignore the request header and send everything.
  The timeline renders its latest 40 turns either way. Saved histories of 549
  open conversations (real snapshot, the view computed offline; each
  percentile of its own distribution): p50 0.74 to 0.14 MB, p90 3.9 to
  0.83 MB, p99 10.7 to 3.0 MB, largest 37 to 4.8 MB. In a real
  browser, 7 opens per view, same source, alternating order, measured while
  the live runtime and an unrelated job loaded the machine (load average
  about 13), median first open to Send enabled, recent against complete:

  | Conversation | Bytes | Send enabled, median (max) |
  | --- | --- | --- |
  | 1 turn | 0.10 vs 0.84 MB | 189 (439) vs 191 (241) ms |
  | 38 turns | 3.0 vs 4.6 MB | 268 (581) vs 335 (446) ms |
  | 93 turns | 2.4 vs 8.2 MB | 411 (507) vs 525 (664) ms |
  | 67 turns | 3.7 vs 11.9 MB | 652 (850) vs 787 (883) ms |

  The server still reads and copies the complete history for each read, and
  conversations with thousands of tool calls remain megabytes.
- **Saturation at twice the realistic load** was not reproduced on build 17:
  send acceptance p50 35 ms, p95 182 ms; reads p50 50 ms, p95 170 ms; backend
  CPU 56%. Only the first search after a backend start exceeds a budget.

## Remaining bottlenecks

- **One Python process runs every agent and every request.** Provider stream
  decoding, persistence, event streams and HTTP handlers share one GIL. At
  twice the realistic load build 17 did not saturate (send acceptance p95
  182 ms); heavier loads were not measured. The durable fix is to move provider adapters and their persistence
  out of the request-serving process (or onto a free-threaded interpreter),
  not further per-endpoint tuning.
- The list is about 2.4 MB live; a changed list is still sent and parsed in
  full. A revisioned or incremental inventory would remove that cost.
- WebKit's per-request retention is slowed, not bounded, within one
  interface session.
- The first search after a backend start exceeds the search budget (0.8 to
  1.5 s measured). It synchronously indexes histories changed since the last
  search (297 ms for 20 histories, 17.6 MB, in the snapshot) and runs with
  cold caches. On one cloned snapshot, successive fresh processes took 1,087,
  643 and 87 ms; later searches take 68–137 ms. Indexing changed histories
  at idle, outside the search request, would remove the first part. That is
  a new background writer that holds the GIL, so it needs its own
  measurement.
- Conversations with thousands of tool calls still open with a few megabytes
  of shortened tool rows; loading a completed turn's tool calls on expansion
  would remove most of that.
- Backend changes activate only at an idle runtime handover; see
  [runtime updates](runtime-updates.md). Interface changes apply on reopen.

## October 30 contention repair

The October 30 investigation reproduced a shared session lock held for SQLite's
five-second busy timeout. Independent observer CLI processes repeatedly ran
initialization and no-op updates, while completion persistence and conversation
refreshes depended on those writes. A global save barrier then blocked unrelated
conversations.

### Ownership and initialization

An authenticated runtime advertising `client_requests: 1` executes finite CLI,
hook, MCP and supervisor metadata mutations. Requests carry a bounded, explicit
context; they never mutate process-global environment or working directory.
Only registered high-level operations are callable. SQL text is not a transport
operation. Uncertain mutation responses never fall back to local execution.

Without a runtime, a finite local mutation holds the runtime ownership lock.
Concurrent local threads serialize ownership acquisition; a zero-timeout
background write yields on contention. Schema initialization uses the same
ownership boundary plus its migration lock. A database marker records completed
initialization, so reopening a current store does not rerun migrations or
backfills. Matching live owners may add optional feature tables without causing
their observers to initialize again.

### Reads and saves

Thread inventory, detail, saved history and batch metadata reads enter SQLite
query-only boundaries. Overdue snoozes and missing legacy organization links are
projected in read results; explicit lifecycle work persists their repair.
Opening recent or complete saved history does not resume the provider or acquire
the conversation operation lock. It may briefly show the latest saved snapshot
until live events arrive.

Provider events enqueue persistence outside the shared session lock. Each
conversation has ordered required lifecycle writes and coalesced optional
snapshots. A blocked conversation yields to other keys. Barriers wait only for
their conversation; failures remain visible to that barrier rather than being
silently discarded. Optional snapshots cannot coalesce across required lifecycle
boundaries. Queue limits prevent unbounded accumulation.

Saved history can recover a terminal turn after a restart only when its exact
turn ID matches the durable current turn. Recovery runs in background, verifies
the history revision and durable identity again, and never contacts the provider.
Unknown or still-running saved outcomes remain unknown.

### Submission receipts

Composer submissions carry a stable request ID through retries. Before invoking
the provider, the runtime commits an acceptance receipt. Completed retries return
the saved result. Reuse with different content is rejected. A crash or uncertain
provider response retains an unconfirmed receipt and never replays the effect.
Only a proven pre-submission rejection permits another attempt with that ID.
The provider call runs outside the database transaction. The first message in a
new conversation uses the same receipt path.

This provides duplicate detection, not an exactly-once guarantee from an external
provider. Unconfirmed outcomes require inspecting the conversation; the runtime
does not guess that a timeout means the message was not received.

### Observer contract and validation

`agent-coord thread metadata --thread-id ID` accepts repeated IDs, with at most
200 per request. The HTTP equivalent is
`GET /api/browser/threads/metadata?id=ID&id=ID`. Both preserve input order,
deduplicate IDs, enforce the workspace boundary, and return metadata from one
read snapshot. Activity readers should retain cursors instead of re-reading
complete history. Background analysis should never spawn a CLI process for each
thread.

Regression coverage includes held-writer HTTP requests, cold full/recent history,
query-only mutation rejection, per-conversation barriers, stale completion events,
saved-terminal restart recovery, durable receipt retries, concurrent ownership,
and supervisor process independence. `scripts/measure_backend_latency.py` runs
the real HTTP implementation against an isolated database snapshot with synthetic
providers, streaming turns, multiple windows and independent writer pressure.

Installations still obey the runtime update contract: new backend code remains
pending while the previous runtime owns active work. Do not stop agents to force
activation or describe a staged backend as active.

The repaired implementation was measured against a 1.99 GB snapshot with six
synthetic active conversations, three windows, six deltas per second per agent,
and three independent SQLite writes per second. Across 20 sends and 20 read
cycles there were no request errors. Median / p95 latency was 63 / 304 ms for
send acceptance, 72 / 269 ms for conversation reads, 29 / 107 ms for thread
detail, and 2 / 474 ms for inventory. Search remained above its separate budget
(373 / 1,207 ms); its index maintenance and context caching remain outside this
contention repair. These are isolated backend measurements, not evidence that
the installed runtime has activated the update.

## November 2 writer admission and incremental persistence

The history audit distinguishes the earlier repairs: `1f949aa` reduced connection
and event-stream overhead, `31fda65` kept Claude turns alive during bookkeeping
contention, and `c1255ef` routed mutations into the runtime owner. These repairs
did not serialize that owner's writer threads or bound full-history rewrites
and dirty search projection transactions. The runtime-owner fast path could
still admit competing SQLite writers. Retries improved recovery without removing
those causes. The exact holder of the original reported lock was not recorded;
the reproduced structural failures and new per-operation metrics support this
repair without claiming that missing incident detail.

Finite writable store scopes now share one process-wide admission queue keyed
by the canonical database path, including the runtime-owner fast path. Reads
explicitly enter `store.reading()` and never join that queue. Nested independent
connections are query-only, so they cannot wait for a write lock retained by
an outer scope. Commit, rollback and cursor cleanup finish before admission is
released. Forked processes must construct a new store and cannot inherit runtime
ownership. Foreground work gets preference; waiting background work gets a turn
after at most eight foreground admissions.
Schema initialization distinguishes an authenticated runtime from a finite
standalone writer holding the same kernel lock. A short write can no longer
hide a required migration by suppressing the schema-cookie check.

History snapshots larger than the inline threshold are represented by immutable
SQLite chunks and a small published manifest. Preparation and hashing occur
outside write scopes, unchanged chunks are reused, staging commits are bounded,
and the root is published last. SQLite backups retain the complete snapshot.
An item preparation cache avoids serializing and hashing unchanged items on
every retry or save; it retains at most 16 conversations and 64 MiB of logical
input, and is released when a conversation is evicted. Background persistence
waits up to 100 ms for admission instead of repeatedly forfeiting its queue turn.
Legacy inline histories remain readable. Schema epoch 2 prevents older code from
silently treating a manifest as an empty history. The candidate reads epochs 1
and 2 and migrates at a safe runtime handover. Immutable history chunks are
retained; collection requires a separate reader/staging retention contract.

Search prepares changed histories outside writable scopes and stages bounded
generations before a revision-checked publication. An intervening history update
cannot have its invalidation cleared by older prepared results. Generation
staging also bounds transactions for one oversized conversation.
Clean legacy projections remain available until their conversation receives a
new generation. The migration therefore prepared only seven dirty histories on
the 1,305-history snapshot, rather than rebuilding all 1.77 GB of saved JSON.
Creator metadata uses an ordered covering expression index and one aggregated
SQLite result, avoiding repeated payload parsing, transfers of original prompts,
and per-row interpreter waits. The query explicitly selects that index when
available: retained snapshot statistics otherwise selected the broad status
index and sorted completed request bodies on each refresh. Legacy initialization
continues to work before the optional metadata index has been created.
Sender- and thread-specific request indexes keep conversation creation receipts
and supervision checks from scanning unrelated completed requests. Timeline,
message cursor and stream-change reads use query-only connections, so watching
events does not join the writer queue.
The reminder deadline poll uses a covering partial index rather than scanning
large thread rows every 250 ms. Metrics exposed a 1,306 ms writer scope in that
poll under mixed load; on the retained clone, its isolated cold query fell from
135 ms to 0.03 ms. Inbox-recipient discovery also uses a read-only snapshot;
the existing atomic claim still revalidates eligibility before dispatch.
Read-only and writable connections have separate per-thread reuse slots. This
preserves SQLite schema/page caches across successive reads without allowing a
read-only handle to become a writer; cursors and snapshots still end at each
scope boundary, and thread exit closes the retained handles.
Stores bind their canonical database path once. A verified local runtime skips
repeated filesystem ownership-path resolution, then enters the same shared
writer gate; this optimization does not bypass admission or authorize another
process. Inherited stores remain unusable after a fork.
The HTTP server caps Python's interpreter thread-switch interval at 1 ms, while
preserving a caller's shorter setting. A matched 5 ms/1 ms stored-history
experiment reduced read p50 from 342 to 175 ms and search p50 from 471 to 254 ms
at similar CPU utilization; this adjustment complements the indexed queries and
bounded scopes. The interval is reported in runtime metrics and benchmark output.
It is an interpreter timeslice target, not a hard latency guarantee; see
[Python's scheduling documentation](https://docs.python.org/3.12/library/sys.html#sys.setswitchinterval).
The harness's `--thread-switch-interval` override is diagnostic; acceptance uses
the server's normal setting without an override.

### Measurements agents must use

Authenticated `GET /api/browser/performance` and the normal runtime status expose
bounded, in-memory diagnostics. Collection adds no database writes and retains
operation names and numeric measurements, never SQL, parameters or message text:

- writer admission wait and scope duration: p50, p95, p99 and maximum;
- queue depth, maximum queue depth, active operation and its elapsed time;
- completed/failed scopes and admission timeouts;
- per-operation counts, errors and recent duration samples;
- request counts, errors and latency for sending, history, detail, list and search;
- send execution, persistence-barrier and activity-recording phase latency;
- history source/staged/reused bytes, chunks and publication totals;
- search preparation, staging, publication, refresh and query durations, rows and bytes;
- persistence backlog, retries, oldest pending age and failure count.

Counters are lifetime-to-runtime; distributions use the most recent 2,048 samples
(256 per database operation). Explicit operation names and inferred caller names
each have 64 reserved categories, plus one shared overflow category, so startup
constructors cannot crowd history and search measurements out of the report. HTTP measurements
end before response-body transmission and therefore measure server processing;
use the external harness for client-observed latency and cross-window delivery.

Run the generated-state admission gate on every database-sensitive change:

```bash
python3 scripts/measure_database_contention.py --seconds 15 --writers 6 \
  --check --output build/database-performance.json
```

CI runs this workload with six foreground writers, a bounded background writer
and two independent readers. It fails on overlapping writable scopes, any error
or admission timeout, missing durable receipts, inadequate foreground/background
progress, foreground p95 above 100 ms or writer-scope p95 above 25 ms. Reports
include the source fingerprint and remain downloadable as CI artifacts for 30
days on both tested platforms. These admission budgets are separate from the
HTTP acceptance budgets at the top of this
document. The final local gate completed 3,394 foreground writes (226/second),
1,063 background commits and 2,203 reads in 15.029 seconds, with no errors or
timeouts and exactly one concurrent writable scope. Foreground p95 was 0.514 ms
and writer-scope p95 was 0.391 ms. It used Python 3.12.7 / SQLite 3.51.0 and the
same backend fingerprint as the final HTTP acceptance below. This is generated
load, not a production HTTP result.

For storage and indexing changes, also run `measure_backend_latency.py` on an
isolated representative snapshot, with `--stored-history`, mixed active agents,
concurrent windows, sustained sends and searches. It saves the runtime performance
snapshot in its report. Its raw independent hook writer is deliberate external
SQLite pressure, distinct from the production high-level runtime routing. State
which pressure profile was used. Acceptance also requires available diagnostics,
zero writer-admission timeouts, history failures, persistence retries or permanent
failures, and a successfully drained save queue. Each requested streaming agent
must have one observed worker and sustain at least 90% of its configured event
rate. Missing workload observations, interrupted diagnostics or failed child
cleanup invalidate the run. Compare the same data, history sizes, duration
and machine load, include cold paths and errors, and do not increase thresholds
or discard slow samples to make a regression pass.

The current acceptance harness also requires supported substring indexing,
complete source coverage, a completed backfill, and zero index failures after
the persistence drain. Short queries and unindexable text still use exact
search; index coverage is an acceleration invariant, not permission to omit
messages. Missing coverage diagnostics fail acceptance rather than treating an
unfinished migration as a successful performance result.

Run acceptance without `--profile`. That optional diagnostic mode samples every
Python thread's stack and can disturb the workload it observes. It now samples
code metadata at 50 Hz; the former 200 Hz source-line extraction was particularly
expensive. Reports record `profile_enabled`, Python and SQLite versions, and whether
stored histories were used. Preserve diagnostic failures for attribution; measure
acceptance with the normal runtime metrics enabled and stack sampling disabled.

### November 2 recorded result (superseded)

The November 3 reproduction invalidated this run as acceptance for the live
problem: it omitted independent sidecar polls, did not verify actual stream
workers, and used older, smaller histories. The numbers below describe that
earlier workload only. Backend activation also did not establish healthy live
operation; the activation receipt already contained admission timeouts.

The recorded stored-history run used the same 1.9 GB snapshot, its original
saved transcripts, six requested streaming agents at six deltas/second, three windows,
and three independent hook writes/second. It ran on Python 3.12.7 / SQLite
3.51.0 with the normal 1 ms server thread interval, no diagnostic override,
and no sampler. All 30 sends and 30 read/search cycles succeeded, and every
existing budget passed, including the initial search migration:

| Operation | p50 | p95 | Maximum |
| --- | ---: | ---: | ---: |
| Send acceptance | 28.7 ms | 122.4 ms | 141.6 ms |
| Message visible in another window | 8.0 ms | 106.9 ms | 129.8 ms |
| Conversation read | 13.5 ms | 31.1 ms | 39.9 ms |
| Thread detail | 8.2 ms | 10.8 ms | 13.0 ms |
| Thread list | 1.2 ms | 84.9 ms | 184.2 ms |
| Search, including initial migration | 79.5 ms | 294.5 ms | 1,638.6 ms |

Writer wait p95 was 0.793 ms and writable-scope p95 was 0.898 ms. Admission
timeouts, persistence retries, permanent failures, and final backlog were all
zero. The 98 history snapshots represented 174.0 MB of logical input, staged
29.7 MB of blocks, reused 147.2 MB, and published 12.0 KB of roots. Backend
CPU averaged 35.0% of one core during measurement. These are isolated backend
results, not evidence that a pending installed payload has activated.

The final backend source fingerprint was
`f6caca164243d9bce72445817d8278ab65b1e1726a8b20d9115010a4788f6837`.
The complete report is retained locally as
`~/.local/state/agent-coord/diagnostics/database_contention_20261102/stored_history_final_acceptance.json`.
Earlier failed runs remain beside it; no thresholds or cold samples were removed.
Final regression validation passed 2,203 Python tests with seven optional skips,
all 1,176 JavaScript tests, plugin layout validation, and Python compilation.
The complete Python run used the same frozen backend as the performance reports.

### Earlier generated-history comparison

The November 2 run used the existing 1.9 GB snapshot with generated 1,500 KiB
provider transcripts, six streaming agents at six deltas/second, three windows,
and three independent
SQLite hook writes/second. All 30 sends and 30 read/search cycles completed with
zero request or load-start errors. Every existing latency budget passed:

| Operation | p50 | p95 | Maximum |
| --- | ---: | ---: | ---: |
| Send acceptance | 60.6 ms | 256.6 ms | 319.8 ms |
| Message visible in another window | 8.5 ms | 29.6 ms | 277.9 ms |
| Conversation read | 88.6 ms | 248.6 ms | 266.4 ms |
| Thread detail | 43.7 ms | 103.2 ms | 105.7 ms |
| Thread list | 1.5 ms | 260.4 ms | 283.1 ms |
| Search, including initial migration | 286.9 ms | 770.9 ms | 1,951.0 ms |

Writer admission wait p95 was 2.573 ms; writable-scope p95 was 2.354 ms.
Six background admission timeouts recovered through persistence retries; the
final backlog and permanent failure count were both zero. These are distinct
from user request failures. Across 162 history snapshots, 249.7 MB of logical
input produced 23.8 MB of staged blocks and 19.8 KB of published roots. Retrying
or resaving immutable blocks uses a bounded cache of confirmed digests.

The backend source fingerprint was
`ecdb9c1b3abc83e0b293fa2e212db51da443759944b36104c4ef98167e2932e8`.
These are isolated backend measurements; runtime status must separately prove
activation in an installed application.

## November 3 recurrence and repair

The newly active build still accumulated admission timeouts and rejected sends.
Reproduction now uses the 2.8 GB November 3 snapshot, including saved histories
of 15–29 MB, independently timed sidecar polling, and observed stream-worker
counts and delta rates. Requested concurrency is insufficient evidence of load:
restored in-progress synthetic turns previously had no worker until explicitly
started. The harness now verifies every requested worker and its offered rate.
Background list and detail polls use the same budgets as explicit requests.

Measured causes and implementation changes:

- Sidecar polling scanned all control-request payloads while holding writer
  admission. An indexed target lookup now runs in a query-only scope.
- History block existence checks used the payload-bearing primary B-tree. A
  digest-only covering index reduced a matched 50-query microbenchmark from
  approximately 95 ms warm to 9 ms; the initial unindexed pass took 1.63 s.
- Full history saves monopolized the persistence worker and its preparation
  cache retained only three of seven large conversations. Cooperative steps
  release every connection and lock between batches. Committed preparation
  retains fingerprints and references rather than duplicate values/payloads;
  its existing 64 MiB limit held seven histories in approximately 34 MiB.
  Canonical encoding still runs to detect changes, reported separately as
  `history.encoded_document_bytes`; lower cache memory is not zero CPU cost.
- Normalized provider resume/read ignored metadata-only flags, reloaded whole
  histories and waited for a redundant save. Providers now honor those flags
  and reconcile a caller-owned mirror against runtime execution state. Revision
  checks protect newer events; mutation/lifecycle persistence barriers remain.
- Search re-extracted and republished whole archives after small changes.
  Policy-versioned immutable document results now share text across index
  generations. Compact covering metadata reads avoid old excerpts; routing
  fetches only its existing final-200-message window. All searchable prose
  remains complete. Index publication checks source revision and retries
  conflicts instead of silently returning an older head after a failed publish.
- Search preparation is derived work. Browser saves publish their durable root
  before queuing it under a separate persistence key. Search-cache failures
  cannot turn a committed conversation into a rejected save; search can rebuild
  missing entries from immutable history. Cache transactions allow 512 rows
  within the existing 256 KiB bound, with SQL statements below 999 parameters.
- Diagnostics captured a 1.93 s commit inside a history staging scope. A
  runtime-owned PASSIVE checkpoint worker moves checkpoint I/O outside writer
  admission while retaining FULL durability. Writable cached handles restore
  automatic checkpointing when maintenance stops or fails. Pure review and
  dispatcher reads also no longer reserve the writer.
- Later measurements captured a 5.04 s connection open inside writer admission,
  with only 5.6 ms of thread CPU across that scope. Connection acquisition now
  precedes admission; commits, rollbacks, statement finalization and disposal
  of unfinished handles remain inside it. Thread detail, list and domain reads
  use query-only scopes; their existing snooze and organization projections
  remain read-only, while explicit maintenance performs durable repairs.

Concurrent checkpoint maintenance requires SQLite 3.51.3 or newer, or the
documented 3.50.7/3.44.6 backports. Older libraries retain automatic checkpointing
because of SQLite's [WAL-reset race](https://sqlite.org/wal.html#walreset).
The locally available Python 3.13.5 uses SQLite 3.53.4; Python 3.12.7 uses the
unpatched system SQLite 3.51.0. Record these versions and use matched runtimes
for comparisons. [PASSIVE checkpoint semantics](https://sqlite.org/c3ref/wal_checkpoint_v2.html)
permit reader/writer progress but do not remove disk or scheduling contention.

### Diagnostics and acceptance

The authenticated performance response retains the latest 32 slow database
operations and the 16 longest writable scopes. Statement, transaction and scope
records use bounded operation names and numeric identifiers, never SQL,
parameters or transcript text. Phase durations are wall time and include
waiting to resume Python; recorded phases cover calls of at least 50 ms and
are not a complete timing breakdown. History preparation/staging durations
include cooperative queue suspension and must not be called CPU time.
Slow writable scopes also report thread CPU time and the remaining wall time;
the remainder combines I/O, GIL and scheduling waits without attributing them.
Connection opening is measured separately. Host load, logical CPU count and
process peak resident memory provide context for interpreting each run.

Checkpoint diagnostics include errors, duration, WAL frames/bytes, remaining
backlog and last progress. Search reports decoded/reused documents, bounded
staging bytes and excerpt retrieval. A small pending WAL suffix differs from
the physical WAL file size, which SQLite can reuse without truncating.

Acceptance requires every HTTP budget, zero request errors, zero admission
timeouts or persistence failures/retries, successful derived-work scheduling,
healthy checkpoint maintenance where supported, and a drained save queue.
Optional persistence failures remain visible in `optional_failure_count` even
though they do not reject a committed conversation save. Both this counter and
search operation failures must remain zero in acceptance workloads.
Retain cold samples and failed reports. Freeze source only after all writers
confirm completion; one diagnostic copy taken during an edit contained an
undefined helper and is explicitly invalid as search acceptance.
The measurement harness removes its generated database and runtime files after
owned children exit, including failed and interrupted runs. JSON reports and
server logs remain. An unconfirmed child exit retains the directory and fails
acceptance explicitly. Earlier failure retention left eight database copies
accounting for about 18 GiB; all 31 inactive temporary directories were removed
on November 3 after preserving their small diagnostics. The observed free-space
increase was 644 MiB, distinct from the files' reported allocated size.

The first expanded runs remain failed evidence, not deployment approval:
`sidecar_poll_before_complete.json` reproduced HTTP 503; the digest-index-only
run accepted all 30 sends but missed latency budgets, including search p95
24.56 s. The initial integrated run failed during setup with save backlog.
Host load subsequently exceeded 100 with more than 11 GiB swapped, so further
performance measurements require a coordinated quiet interval. These reports
are retained in the local `database_contention_20261102` diagnostics directory.
Full regression, matched workload acceptance, installation and live health
verification are separate outcomes; no activation receipt alone proves repair.

Immutable history and search blocks are retained. Collection needs a separate
contract for published roots, captured readers and in-progress staging; this
repair does not delete conversation data to achieve a latency result.

### November 8 completion validation

The resumed investigation found remaining contention in ordinary read paths.
A bounded admission-timeout record identified `history.publish` waiting behind
`browser_wake._dispatch` for 135.7 ms. The holder occupied admission for 140.1 ms
but used 1.17 ms of thread CPU. This establishes unnecessary scope occupancy;
the remaining wall time includes interpreter scheduling and I/O and does not
identify a slow SQL statement by itself. Timeout records now retain the waiting
operation, priority, active operation/scope, elapsed times and queue depth, with
a limit of 16 records and no statement text or parameters.

Wake eligibility previews now use query-only connections when no pending
sidecar intervention needs cleanup. Interventions retain durable suppression
even while delivery is paused; final claims and submission checks still clean
up and revalidate under writer admission. Review/list projections, schedule
reads and empty recurring-schedule polls also avoid reserving the writer.

SQLite result transfer was another source of interpreter contention. Native
JSON aggregation transfers stored usage, message and checkpoint rows in batches
while preserving scalar types and complete history. A matched 210-checkpoint
experiment at the production 1 ms interpreter interval measured ordinary versus
batched transfer at 36.7 versus 3.1 ms median with one CPU-bound Python thread
(idle: 0.70 versus 1.71 ms). SQLite versions before the verified 3.53.4 conversion
path retain ordinary transfer so extreme floating-point values stay exact;
real zero also uses that path to preserve its sign.

Substring search uses bounded, overlapping trigram candidates followed by the
original exact matching and ranking. Incomplete coverage, short queries,
unsupported tokenizers and unindexable text retain complete fallback search.
Every nonempty indexing invocation performs a bounded merge, including small
updates; otherwise disabling SQLite's automatic merges allowed segments to
accumulate. Candidate folded text is materialized once for scoring. Search
document staging targets 64 KiB while retaining the 512-row bound for compact
headers. Existing immutable text parts keep their numbering and may individually
exceed the smaller target, within the original transaction bound.

Claude and Antigravity keep a completion snapshot until the same terminal turn
ID and status are durably published, then release it. This preserves the race
between provider completion and history persistence without retaining every
conversation's last large turn after residency release.

The final frozen workload used `live_state_20261103_complete.sqlite3`, all saved
histories, six verified streams at six deltas/second, three polling windows and
three independent hook writes/second. It ran on Python 3.13.5 / SQLite 3.53.4
with the normal 1 ms interval and no profiler. All 30 sends and 30 read/search
cycles completed. Request errors, admission timeouts, history failures, required
and optional persistence failures, and retries were zero; the queue drained.
All 47,061 legacy messages, 3,916 searchable documents and 2,173 generation
messages had complete substring coverage. Generated database files were removed
after every owned process exited; small reports and logs remain.

| Operation | p50 | p95 | Maximum |
| --- | ---: | ---: | ---: |
| Send acceptance | 21.3 ms | 92.4 ms | 461.2 ms |
| Message visible in another window | 8.3 ms | 51.4 ms | 429.5 ms |
| Complete conversation read | 270.6 ms | 560.0 ms | 591.7 ms |
| Thread detail | 46.7 ms | 284.7 ms | 321.3 ms |
| Thread list | 3.8 ms | 444.1 ms | 483.3 ms |
| Sidecar status | 9.8 ms | 43.7 ms | 94.8 ms |
| Search | 270.7 ms | 627.6 ms | 4,431.1 ms |

All foreground and background percentile budgets passed. The initial search
refresh remains visible in the recorded 4.43 s maximum; cold samples were kept.
Writer wait p95 was 0.727 ms and writable-scope p95 was 4.092 ms. The separate
15-second contention gate completed 3,340 durable foreground writes
(222/second), 1,031 background commits and 2,133 reads with no errors or
timeouts. Foreground p95 was 1.584 ms and writer-scope p95 was 0.841 ms.

Both gates used backend fingerprint
`48b3fa81474ee5a0efdd6c7408655731775017c9231ca7ea8a1923db73e38617`.
Reports are `materialized_search_acceptance_20261108.json` and
`resumed_contention_gate_20261108.json` in the retained local diagnostics
directory. Earlier November 8 failures remain alongside them, including the
timeout attribution that exposed wake polling. These are isolated workload
results; backend installation and activation require separate verification.

Final regression validation completed 2,319 Python tests with seven optional
skips, all 1,176 JavaScript tests, plugin layout validation and Python
compilation. The complete Python run took 390.369 seconds, and all 404 reserved
source inputs remained unchanged. One test fixture was updated to compare schema
and physical backing tables because Python's SQL dump cannot scan a contentless
FTS table; its assertion that evaluation performs no persisted writes remains.
The backend fingerprint stayed identical to both passing performance gates.
