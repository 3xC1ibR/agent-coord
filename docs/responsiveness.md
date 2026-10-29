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

## Remaining bottlenecks

- **One Python process runs every agent and every request.** Provider stream
  decoding, persistence, event streams and HTTP handlers share one GIL. At twice
  the realistic load the backend still saturates (send acceptance p95 about
  0.7 s). The durable fix is to move provider adapters and their persistence
  out of the request-serving process (or onto a free-threaded interpreter),
  not further per-endpoint tuning.
- Each list refresh still transfers and parses about 5 MB; a revisioned or
  incremental inventory would remove that cost from the WebView.
- Opening a large conversation for the first time transfers and renders its
  whole history before Send is enabled (about 0.5 s for 1.5 MB under load);
  loading the latest turns first would remove most of that.
- Backend changes activate only at an idle runtime handover; see
  [runtime updates](runtime-updates.md). Interface changes apply on reopen.
