# Capacity planning for hosted workspaces

This records Ribbon Field's measured resource consumption, to size remote
virtual machines (VMs) for the hosted workspace proposed in
[standalone-product.md](standalone-product.md). Measurements were taken on
2026-10-28 during a slowness investigation. Recheck them after the fixes listed
under [Preconditions](#preconditions-for-a-fixed-size-vm) land, and on Linux,
before publishing plan limits.

## Measurement context

- Host: macOS arm64 laptop with 16 GB RAM, running the installed app bundle,
  about two days older than repository commit `6188823`. The backend had been
  up for 47 hours.
- Workload: one heavy user (the developer) with five providers (Codex, Claude,
  Kimi, Antigravity, Muse) and personal Codex MCP servers (`mcp-grafana`,
  `node_repl`).
- Memory figures are macOS `footprint` values: dirty plus compressed plus
  swapped pages. This is roughly comparable to Linux RSS plus swap. Linux
  binaries and allocators will differ.

| Workload measure | Value |
| --- | --- |
| Turns per day (2026-10-27, 2026-10-28) | 802, 663 |
| Turns in the last 48 hours | 1,584 |
| Concurrent turns, last 48 hours | mean 4.8; hourly peak p50 10, p90 16; maximum 19 |
| Turn duration | median 2.8 minutes; p90 23.2 minutes |
| Unarchived app conversations | 447 (312 Codex, 74 Claude, 34 Antigravity, 18 Kimi, 9 Muse) |
| Codex conversations loaded in the shared app-server | about 155 |
| Open browser windows (each with one event stream) | 3 |

## Summary

1. **Memory currently grows without bound.** Every conversation loaded into
   the Codex app-server keeps its MCP servers running until the app-server
   exits. After 47 hours the backend's process tree used 24.3 GB across 622
   processes. No fixed VM size is safe until idle conversations are unloaded.
2. **With a cap on loaded conversations**, host memory is roughly 2.5 GB fixed
   plus 0.1–0.3 GB per loaded conversation, plus the user's builds, tests and
   browsers (not measured here).
3. **The backend's CPU use is mostly avoidable overhead.** It held 2.2–3.0
   cores for two days, mostly re-reading SQLite pages. That would exhaust a
   2-vCPU burstable instance by itself.
4. **Disk grows about 0.3 GB per day** at 700–800 turns per day, about
   8 GB per month, excluding repositories and build caches.
5. **Browser traffic is dominated by repeated thread-list fetches**: 0.5–1.1
   MB/s while windows are open, which would use most of a 5–6 TB monthly
   transfer allowance.

## Memory

Backend process tree (host side):

| Component | Processes | Footprint | Per process | Scales with |
| --- | --- | --- | --- | --- |
| Codex app-server (shared) | 1 | 4,866 MB | — | Loaded Codex conversations (about 31 MB each at 155 loaded) |
| agent-coord MCP server (Python) | 180 | 4,302 MB | 24 MB | Loaded conversations, all providers |
| Claude CLI | 18 | 3,294 MB | 183 MB | Live Claude conversations |
| ChatGPT `cua_node` (spawned by `node_repl`) | 58 | 2,684 MB | 46 MB | User MCP configuration |
| `mcp-grafana` | 155 | 2,402 MB | 15 MB | User MCP configuration, per Codex conversation |
| Kimi CLI | 5 | 1,555 MB | 311 MB | Live Kimi conversations |
| `node_repl` MCP | 155 | 1,472 MB | 9 MB | User MCP configuration, per Codex conversation |
| Backend (Python) | 1 | 1,288 MB | — | Includes in-memory copies of loaded conversation histories |
| Nested Codex app-server (spawned by `node_repl`) | 30 | 1,102 MB | 37 MB | User MCP configuration |
| Antigravity (`agy_acp_server` + `localharness`) | 10 | 1,452 MB | 291 MB per pair | Live Antigravity conversations |
| Muse | 2 | 300 MB | 150 MB | Live Muse conversations |
| Other | 7 | 158 MB | — | — |
| **Total** | **622** | **24.3 GB** | | |

Estimated cost per loaded conversation:

| Provider | Ribbon Field and provider | With this user's MCP servers |
| --- | --- | --- |
| Codex | about 55 MB (app-server share + agent-coord MCP) | about 105 MB |
| Claude | about 207 MB (CLI + agent-coord MCP) | Depends on Claude MCP configuration |
| Kimi | about 335 MB | — |
| Antigravity | about 315 MB | — |

User-configured MCP servers are multiplied by the number of loaded
conversations. A customer's MCP configuration can therefore dominate memory.

Client side: on the Mac, the WebKit networking process reached 8.7 GB (all
"WebKit malloc") and the page process 1.5 GB. The networking process grows in
step with bytes received on the long-lived event streams: 6 MB in 90 seconds
while the streams delivered 3.4 MB, whereas about 67 MB of completed responses
in the same window were not retained. The cause is suspected but not yet
confirmed. On a hosted VM this cost moves to the customer's browser, including
Safari on iPhone.

## CPU

| Process | Observed CPU |
| --- | --- |
| Backend (Python) | 220–300% sustained over 47 hours |
| Codex app-server | about 7% |
| Claude CLI | about 10% each while streaming; 31% total across 18 processes |
| MCP servers | near 0% |

Backend on-CPU samples were 74% `pread` and 13% `pwrite`. The causes:

- A new SQLite connection for every operation, each re-parsing the schema and
  starting with an empty page cache.
- About 15 polling loops running every 0.25–2 seconds.
- Full scans of the unindexed `app_control_requests` table.
- The whole conversation history being rewritten on every completed item.

Writers waited p90 92 ms and p99 874 ms for the SQLite lock. These are
software defects, not workload. Agent CPU demand on a VM will mostly come from
the builds and tests that agents run, which were not measured here.

## Disk

| Location | Size | Notes |
| --- | --- | --- |
| `~/.local/state/agent-coord/` | 4.6 GB | State directory, in use since 2026-08-25 |
| `state.sqlite3` | 1.5 GB | `browser_history` is 1.42 GB, covering 22 days of browser conversations |
| `providers/` | 1.7 GB | Antigravity 1.3 GB, Muse 350 MB |
| `backups/`, `deployments/`, `claude-forks/` | 763 MB, 249 MB, 194 MB | |
| `~/.codex/sessions/` + `archived_sessions/` | 7.6 GB + 1.4 GB | Codex transcripts |
| `~/.claude/projects/` | 0.9 GB | Claude transcripts |

Per-conversation history size: median 0.59 MB, p90 3.07 MB, maximum 35.5 MB.

Growth over the last seven days:

- About 409 MB of new conversation history, from 312 new conversations.
- Codex transcript files modified in that period total 1.3 GB (an upper bound).
- Claude transcript files modified in that period total 0.2 GB.

That is about 0.3 GB per day, or roughly 8 GB per month, before repositories,
dependencies and build caches. A 160 GB disk covers more than a year of this
state at that rate.

## Network (backend to browser)

| Traffic | Rate |
| --- | --- |
| Event stream, per open window | about 14 KB/s (about 36 GB per month) |
| Total outbound with three windows open | 0.5–1.1 MB/s |
| Thread-list response | 4.5 MB per fetch: full original prompt 37%, links 26%, checkpoint 20% |
| Opening the largest conversation | 36.9 MB per load |

At 0.5–1.1 MB/s, one continuously open client would transfer 1.3–2.9 TB per
month. Most of that comes from refetching the thread list after streamed
events. Inbound traffic is negligible.

## Preconditions for a fixed-size VM

These are required before any plan promises a memory size:

1. **Unload idle conversations.** Cap the number of loaded conversations and
   stop their MCP servers. Without this, memory grows with the number of
   conversations opened since the last restart.
2. **Recycle event streams periodically**, so that browsers cannot accumulate
   stream data.
3. **Refresh the thread list only for list-relevant events, and slim the
   payload**, to cut egress and backend work.
4. **Make backend database access efficient:** reuse connections, add the
   missing index, and coalesce history writes. This brings backend CPU down to
   a level a burstable instance can sustain.

## Sizing estimate (proposal)

With the preconditions in place:

```text
memory ≈ OS and services (~1 GB, assumed)
       + backend (~1.3 GB)
       + Σ loaded conversations × per-conversation cost (table above)
       + user workload (builds, tests, browsers, databases)
```

| VM | Example allocation | Remaining for user workload |
| --- | --- | --- |
| 2 vCPU, 8 GB | 10 loaded Codex conversations (about 1.1 GB) and 3 loaded Claude conversations (about 0.6 GB) | about 4 GB |
| 4 vCPU, 16 GB | 20 loaded Codex conversations and 6 loaded Claude conversations | about 10 GB |

The pilot's target customer has "several active coding threads." That is below
this user's mean of 4.8 concurrent turns, so 8 GB looks plausible once the
preconditions hold. Confirm it by benchmarking representative repositories on
Linux.

## Reproducing the measurements

- Process memory: walk the backend's descendant processes (`ps -Ao
  pid,ppid,rss,command`) and run `footprint -p <pid>` for each.
- CPU attribution: `sample <backend-pid> 5` works without root (py-spy
  requires root on macOS). Separate on-CPU leaf frames from blocking system
  calls.
- Network: `nettop -P -p <backend-pid> -d -L 4 -s 15 -J bytes_in,bytes_out -x`.
  Per-socket totals come from `netstat -anvb -p tcp`.
- Concurrency: read `startedAt` and `completedAt` for turns from
  `browser_history.history_json` through a read-only connection, then sweep
  the intervals.
- Database snapshot for offline profiling: use Python's
  `sqlite3.Connection.backup(target, pages=-1)`. The `sqlite3` shell's
  `.backup` restarts on every write to the live database and does not finish.
