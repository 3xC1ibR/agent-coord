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
