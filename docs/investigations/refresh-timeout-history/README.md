# Conversation refresh and responsiveness history

Audit date: November 9, 2026, America/Chicago. Historical timestamps and chronology dates below use UTC unless explicitly marked otherwise. This report addresses the repeated responsiveness problem and its consequences, rather than treating every occurrence of “refresh” as the same defect.

The record shows repeated, real repairs to individual causes, an explicitly unfinished core of expensive history reads and shared-process work, shortcomings in earlier acceptance workloads, and a later loss of two performance capabilities through the selected Python/SQLite runtime. It does **not** establish a single blocking operation for the latest reported timeout.

The earlier diagnosis emphasized current machine contention. This sweep materially changes that interpretation: expensive full-history processing and wider responsiveness ownership were already documented weeks earlier, and the previously validated runtime configuration was subsequently replaced. Host load is an aggravating condition; it is not a sufficient historical explanation or a complete resolution.

The saved **Ribbon Field busy error** conversation already contains almost this exact assignment on November 2: inspect commit history, explain why repeated fixes did not work, and determine a robust solution. Its next instruction asks to “fully and robustly fix it” and add performance/latency metrics. On November 3 the same conversation again asks why it is not fully fixed. This retrospective therefore records a repeated unfulfilled outcome, not a newly discovered requirement. The [conversation inventory](conversations.md) retains the exact message identities, timestamps and origin qualifications.

## Evidence and coverage

The machine-readable inventories preserve inclusion decisions, exact identities, evidence quotations and qualification of claims. The readable inventories provide navigation:

- [Commit inventory and screening](commits.md), with [structured records](commits.json).
- [Beads inventory and issue histories](issues.md), with [structured records](beads.json).
- [Conversation inventory and evidence](conversations.md), with [structured records](conversations.json).
- [Runtime configuration and validation receipts](runtime-evidence.json).
- [Coverage and verification results](verification.json).

| Source | Searched universe | Retained for this report |
| --- | ---: | ---: |
| Git | 332 initially reachable commits across 33 refs, plus one concurrent commit; two additional reflog-only commits reviewed | 70 repair, recovery, validation, activation and provenance records |
| Beads | All 255 issues, all statuses; all 10 comments; complete histories of 14 central issues | 42 direct/consequence/delivery/adjacent/context issues; 58 distinct historical revisions |
| Saved conversations | All 1,688 work threads; 48,560 indexed prose messages; 18,320 checkpoints; 26,912 coordination records | 83 conversations: 29 direct investigations, 44 repair/delivery/process and 10 collateral/cross-project |
| Runtime evidence | 39 retained payload configurations, installed interface identity, authenticated serving status and 11 primary validation/activation reports | Captured identities, hashes, capability comparisons, metrics and limitations |

The Git closing cutoff is November 10 at 02:01:18 UTC; the source and data collection occurred while other sessions continued working. Conversation indexes covered 1,552 of 1,558 saved histories. Five relevant unindexed current histories were decoded directly; the remaining unindexed history was unrelated. The 130 work threads without saved histories can contribute requests, checkpoints or coordination records, but not a reconstructed full transcript. Index lag in active conversations remains a coverage qualification.

Git coverage includes every locally reachable branch, remote-tracking reference, preserved build source and checkpoint reference at collection, plus the identified reflog-only commits and the closing concurrent-change delta. It does not mean every remote-only branch was fetched. Beads coverage includes closed issues, all current descriptions and notes, all comments, and full Dolt histories for selected central issues. Conversation coverage is the locally retained saved prose, checkpoints, coordination records and indexed document generations across placements and workspaces; the conversation inventory states its missing-history limits. These are three views of overlapping evidence, not three independent confirmations of every claim.

No provider was resumed to inspect a conversation. No installed app, backend, plugin or active work was changed. This assignment produced evidence and documentation, not another implementation attempt. Existing unrelated worktree edits were preserved.

## The findings that explain the repetition

### 1. The core read cost was recognized and left open

On October 8, **agent-coord-zrc** measured a 7.2 MB conversation with 1,275 items and attributed approximately 170 ms of a 209 ms read to remembering history. Its repair reused cached conversations and suppressed unchanged saves. Those are specific improvements; they do not bound the cost of every future conversation read.

The October 29 app-wide assignment explicitly asked for systemic responsiveness, realistic existing data and concurrent-agent validation. The resulting work fixed local message visibility, list storms, provider cold starts and query overhead. Its completion evidence explicitly retained per-read full-history copying, multi-megabyte tool rows, WebKit retention, cold search and separation of provider work from the request process as further work.

**agent-coord-udh1.5** closed for its approved source/correctness scope. **agent-coord-czye**, created October 29 and still open at collection, expressly retains “multi-megabyte responses and full-history deep-copy/tool-row loading costs” and states that residency is not a hard RAM/process cap. That separation was made by explicit user instruction; it was not a silently dropped release requirement. Its description also records provider process-split/live-swap research as user-paused. This audit does not authorize resuming that work. **agent-coord-cgkq**, opened October 31 for whole-interface stalls and refresh failures, remains in progress without findings recorded on the issue. These records prevent interpreting the closed subtask as comprehensive responsiveness completion. See the [issue inventory](issues.md) and [conversation evidence](conversations.md).

The serving code still executes `copy.deepcopy(self.histories.get(thread_id))` inside the shared `BrowserSessions.lock`, then applies `recent_view`. A cold saved read decodes the complete history first. Returning the latest 40 turns reduces the response but does not make history acquisition and copying proportional to the displayed portion. The contract itself states this limitation in [Responsiveness](../../responsiveness.md#resource-and-request-bounds-october-29-campaign-i15i19).

### 2. Several earlier repairs fixed survival or one contention layer

These changes must be credited for what they actually changed:

| Change | Verified purpose | What it did not establish |
| --- | --- | --- |
| `de983ec7` | Add the 15-second read deadline, retry/recovery state and draft preservation. This is the origin of the reported error text. | A slow read is surfaced and released; backend latency is not cured. |
| `1f949aa8` | Reuse SQLite connections and throttle event-stream coordination reads, reducing schema parsing and allocator contention. | One owner process can still run competing writer threads and CPU-heavy history work. |
| `097634cf`, `7955304e`, `83e4e64d` | Make own-message display immediate, coalesce inventory reads, reduce provider restarts and make the interface fix available from existing served assets. | Fast local echo does not prove fast acceptance, persistence, or uncached conversation opening. |
| `31fda657` | Keep Claude's reader and completed output alive when bookkeeping encounters database contention. | Retrying bookkeeping does not remove the source of contention. |
| `c1255efe` | Route finite mutations through the runtime owner, introduce query-only observer paths and isolate ordered persistence by conversation. | That owner's writable scopes were not yet serialized process-wide; large history and index work remained. |
| `556baa63` | Add writer admission, bounded immutable history staging, incremental search publication, indexed read paths and metrics. | Its original acceptance workload was later shown insufficient, and a full-history read remains full-history work. |
| `fbcdef79` | Repair further read-side admission, persistence scheduling, search and checkpoint costs, and strengthen measured workload verification. | The one-process/shared-lock architecture remains, and two optimizations require a newer SQLite library. |

Full patches, changed tests, recorded limitations and duplicate/snapshot classification are in the [commit inventory](commits.md). Equivalent parallel commits and preserved snapshots are not counted as independent repair designs.

### 3. Some acceptance evidence was weaker than the live workload

The November 2 stored-history report recorded zero request errors and passing budgets. The later investigation explicitly invalidated it as acceptance for the live problem because it omitted independent sidecar traffic, did not establish that all requested streams actually had workers, and used smaller/older saved histories. That is not an inference from poor production results; it is a documented correction in [Responsiveness](../../responsiveness.md#november-2-recorded-result-superseded).

The primary receipts make the gap concrete:

| Evidence | Recorded result | Correct interpretation |
| --- | --- | --- |
| `stored_history_final_acceptance.json` | Python 3.12.7 / SQLite 3.51.0; six requested agents; send p95 122.4 ms; read p95 31.1 ms; no errors, timeouts or retries. | Passed the earlier workload. Later superseded as evidence of the live problem being repaired. |
| `runtime_activation_result.json` | State `active`, already **36 admission timeouts**. | Correct payload identity was established; healthy live operation was not. |
| November 3 live verification | **803**, then **1,100** lifetime admission timeouts on the same runtime instance. | Recurrence after activation was directly observed. |
| `sidecar_poll_before_complete.json` | HTTP 503 on send, 68 admission timeouts, 44 retries, undrained saves; independent sidecar p95 about 8.53 s. | Adding omitted traffic reproduced a failure the earlier run did not cover. |
| `history_digest_index_acceptance.json` | Index-only repair still missed multiple budgets; search p95 **24.56 s**. | One successful microbenchmark or accepted set of sends did not establish complete repair. |
| `materialized_search_acceptance_20261108.json` | Python 3.13.5 / SQLite 3.53.4; six observed streams sustaining approximately six deltas/s, three windows and three hook writes/s; all budgets passed; no errors/timeouts/retries; drained saves. | Substantially stronger isolated workload evidence, with its exact runtime and data. |

The November 8 report retains the initial 4.43-second search maximum despite passing percentile budgets. Percentile success is not a maximum-latency guarantee. Its 2,319-test regression result and the admission gate are separate checks from HTTP latency.

Beads **agent-coord-wgg7** also preserves two actual reopenings when legacy migration and representative stored-history measurements exposed gaps after narrower checks passed. Its seven distinct issue states are retained in [beads.json](beads.json). A narrow subtask's close reason should be read with its stated integration and deployment owner, not inflated into a whole-system claim.

### 4. A validated runtime capability was subsequently lost

This is a newly reconciled delivery finding, supported by retained payload configuration and live activation receipts:

| Build / state | Selected interpreter and SQLite evidence | Consequence |
| --- | --- | --- |
| Build 38, November 8 | Python 3.13.5 / SQLite 3.53.4; payload `d6966f1f…`; matching acceptance fingerprint `48b3fa81…`. The activation observer recorded `active_and_verified`, seven observations over about 60 seconds, zero admission timeouts/retries/persistence failures, and a running PASSIVE checkpoint worker. | The newer configuration really activated; it was not merely a staged candidate. |
| Build 39 retained payload | `/opt/homebrew/bin/python3.13`. Its observer was stopped for coordinated build 40 rollout. | Configuration remained newer; the retained receipt does not prove build 39 independently activated. |
| Build 40 retained payload | `/Users/walle/.pyenv/versions/3.12.7/bin/python3`. | Later delivery selected the older library again. |
| Build 41, serving during this audit | Runtime `0.1.6+41`, payload `a945946f…`, Python 3.12.7 / SQLite 3.51.0. | Batched SQLite row transfer falls back to ordinary per-row transfer; separate PASSIVE checkpoint maintenance reports `unsupported_sqlite`. |
| Build 42 retained payload | Python 3.12.7. | The older selection persisted in another candidate. |
| Build 43, installed interface / selected candidate | Interface `0.1.7`, build `43`; selected payload `5fe1cc54…`, Python 3.13.5. | Interface installation does not mean the candidate backend is active. The serving backend remains build 41. |

The conversation **Canvas Browser wake failure** connects build 40 to a separate repair: the agent reported that Homebrew Python 3.14 failed importing XML and switched the suite to the repository's documented Python 3.12.7 interpreter, then rebuilt build 40 with commit `76f08df`. This explains a plausible route to the configuration change; the payload metadata independently proves the selected interpreter. The record does not establish an intentional decision to discard the newer performance configuration.

`desktop/macos/build.py` defaults `--python` to the interpreter running the builder and accepts Python 3.10+. A backend source fingerprint hashes Python source files, not the selected interpreter or linked SQLite version. Source matching alone therefore cannot establish capability equivalence.

The key algorithms are **present in the serving payload**: `sqlite_rows.py`, `database_checkpoint.py`, `database_writer.py`, `history_persistence.py`, `conversation_substrings.py`, `browser_wake.py` and `store.py` match the inspected source. The issue is the runtime-dependent paths being disabled, not a claim that all November 8 source changes are absent. Relevant file hashes and all 39 retained payload identities are in [runtime-evidence.json](runtime-evidence.json).

The build 38 observer also demonstrates the limits of a brief health check: a later recheck retained zero admission timeouts and no persistence failures, but observed conversation-read p95 about 1.95 s over 14 reads and send p95 about 1.02 s over two sends. Its initial metrics also retained 64 failed database scopes, rising to 313 in that later recheck. Those scope counts are not independently classified as user-visible failures and were not among the observer's failure gates. These are small, uncontrolled live samples, not a matched benchmark; they show why identity and selected clean counters alone should not be called sustained latency acceptance.

### 5. Repair delivery can itself be blocked by a different defect

The selected build 43 backend is pending. The most recent explicit update transaction targeted another retained build 43 payload, `fe78d46a…`, and failed with **“Conversation bookkeeping could not be saved; update deferred.”** The old runtime stayed serving.

The separate native-child defect is reproduced and documented in [Runtime updates after native child-agent activity](../../runtime-bookkeeping-recovery.md): provider-native child events were scheduled as if they belonged to registered Ribbon Field app conversations. Missing-session and foreign-key errors entered the required persistence queue, whose failure records correctly remain blocking. That poisoned the global update flush even after a child completed.

`eca41335` repairs event ownership in source; **agent-coord-jylh.1** is closed for that repair. The already running old process cannot receive the fix through another interface install or have its in-memory failure queue repaired merely by submitting the same update again. **agent-coord-jylh** remains an activation task. **agent-coord-oklw** and its implementation/validation work track the stronger uninterrupted-upgrade contract; the independent live gate remains failing evidence, not completed rollout.

This is a delivery obstacle related to getting performance repairs into service. It is not evidence that native-child bookkeeping caused every slow read, nor that all foreign-key errors are SQLite writer contention.

## Chronology of the main attempts

| Period | Request or observed symptom | Response and evidence | Remaining boundary |
| --- | --- | --- | --- |
| October 6–8 | Saved history problems, overlapping monitor refreshes, slow reopening. | Browser sessions/persistent history introduced; monitor requests serialized; zrc adds cached conversation reuse and unchanged-save suppression. | Full read and persistence costs remain size-dependent. |
| October 12–15 | Busy database responses, slow inventory, close/render/project restoration delays. | Read-side writer reduction (`7622cffe`), batched inventory (`b15796c7`), streamed render batching and cached navigation. | Later paths still open writable scopes; uncached reads and provider/shared-process work remain. |
| October 26–28 | Invalid sidecar polls, unreadable workspace responses, job contention, hung refresh/Send, memory growth. | Draft poll guard, transport recovery, job survival, the 15-second refresh deadline (`de983ec7`) and reduced request count (`4b43720c`). | These include guardrails and partial cost reductions, not a universal latency bound. |
| October 29 | Repeated opening/search/send/refresh slowness; a systemic ownership request. | Connection reuse (`1f949aa8`), local echo, inventory/event reductions, search/query changes, provider reuse; live build 17 activation and large measured improvements. Reliability/resource work is integrated and independently reviewed. | Per-read copying, tool-row payloads, cold search, WebKit retention and provider/request-process separation are expressly deferred. |
| October 29–31 | Turns falsely failed, missed follow-ups, interface stalls, telemetry/hook errors. | Claude reader survives bookkeeping contention (`31fda657`), telemetry isolation, runtime-owned mutation/read-only/persistence isolation (`c1255efe`), inventory coalescing correction (`fd2a9546`). cgkq opened for ongoing stalls. | Retrying and owner routing do not serialize all owner writers or bound archive work. |
| November 2 | Recurrent SQLite contention and large persistence/index work. | Writer admission and bounded history/search publication (`556baa63`); multiple issue reopenings; passing old workload; build 37 installed and later activated. | Activation receipt already unhealthy; original acceptance later superseded. |
| November 3–8 | Newly active backend still times out/rejects sends. | Larger actual histories, independent polls and verified streams expose further causes; repair scope expands through fbcdef7 and corrected acceptance. | Some optimizations depend on SQLite version; full-history copying and shared-process work remain. |
| November 8 | Need to deliver validated repair. | Build 38 activates with newer SQLite and an initial clean observation interval. | Later live latency samples remain limited; subsequent unrelated rollout selects older interpreter again. |
| November 8–10 | Canvas wake/reconciliation and later interface/dashboard delivery. | Builds 40–42 use Python 3.12.7; build 41 serves. Build 43 selects Python 3.13 but update is blocked by retained child bookkeeping failures. | Current execution is not equivalent to the November 8 passing runtime. |
| Latest report, November 9 local | “Refreshing this conversation timed out.” | Client deadline verified at 15 s; backend recorded conversation reads up to 33.3 s and metadata up to 24.6 s. Host load around 60 on 10 logical CPUs. | Exact failing request and time spent in locks, scheduling, decoding and copying are not captured. |

## Related consequences and distinct neighboring defects

| Symptom or work stream | Relationship established by the record |
| --- | --- |
| Send/input lag | Directly linked to waiting for backend acceptance and whole-history/provider work; optimistic display improved visibility while acceptance is measured separately. |
| False failed/disconnected Claude turns and missed inbox follow-up | A bookkeeping database error escaping the provider reader was reproduced in ct5c; reader survival is a correctness repair. |
| Managed supervisor/job failures | Contention handling was repaired independently (`ddf99682`); it does not prove the holder of the user's latest read delay. |
| Provider telemetry/heartbeat failures | Antigravity and turn telemetry gained exception isolation; these prevent metadata failures from becoming lifecycle failures. |
| Runtime startup/handover failure after interrupted creation | An orphan browser row caused startup to fail; `35d9a72d` adds recovery. Contention can cause the interrupted creation, but every orphan is not proven to have that cause. |
| WebKit memory growth | Per-request retention was experimentally measured; reducing polls slowed it. No hard memory bound was established. It adds a resource concern alongside backend processing. |
| Native full-smoke pane-toggle timeout | Retained as NOT_PASS / unknown cause with follow-up 0jba. No evidence here establishes the same root cause. |
| October 31 low-power/throttling report | The saved conversation also reports a laptop at 3% charge and trouble charging. That is a possible environmental aggravator in that incident, not proof that every recorded stall has a power-management cause. |
| Plugin cache deletion and marketplace/source drift | Can strand loaded hooks/assets or install a snapshot instead of current source. Delivery provenance is relevant; these are distinct from the measured history/SQLite mechanisms. |
| Stale running state and lost inbox wake after restart | Reconciliation repairs (`76f08df`, concurrent `7ddcebc`) concern lifecycle truth. They are not proof of a performance cure. |
| Native-child bookkeeping poisoning updates | Separately reproduced event-ownership defect; blocks delivery of a newer backend. |

The memory investigation also contains a useful correction: its first explanation blamed long-lived event streams and proposed recycling them. The user's observation that window refresh did not reclaim memory prompted an isolated experiment, which attributed retention to request count instead. The agent explicitly abandoned stream recycling. That is a falsified earlier mechanism, not another independent proven cause; the [conversation evidence](conversations.md) retains both the original claim and its correction.

## What the historical record still does not establish

1. **The exact cause of the latest individual request.** The error establishes a deadline breach. Aggregate latency and host observations support contention, but do not identify the particular request, lock holder, thread or phase. Earlier investigators also explicitly acknowledged missing original lock-holder evidence.
2. **A bounded conversation-open path independent of total history size.** Recent-view trimming happens after complete history acquisition/copying. The remaining issue named this explicitly; it is still represented in serving source.
3. **Sustained whole-app responsiveness at the real workload and retained-history scale.** Isolated corrected workloads and brief live observations are evidence with a defined scope. They do not cover arbitrary future agent counts, tool-output size, number of windows, uptime and competing machine load.
4. **A hard process or memory limit.** Residency eligibility and warm-count policies are not such a limit, and WebKit request retention remains a separate concern.
5. **Preservation of the validated execution configuration across later builds.** The 3.53.4-to-3.51.0 transition occurred despite matching relevant source. The newer build 43 configuration is pending, and this audit did not activate it.
6. **Uninterrupted replacement of the current coordinator.** Source-level guarded recovery and a passed bookkeeping test do not satisfy the independent live-upgrade contract.

These are gaps demonstrated by the record, not newly created implementation tasks or approvals. Existing issue status has not been rewritten to imply that this audit fixed them.

## How to use and verify the inventory

Commit records distinguish changed code from measurements, snapshots, integration commits and plugin cache metadata. Issue records preserve the approved scope, exact closure/reopen history and outcome qualifiers. Conversation excerpts retain thread/turn/item identifiers and role/source attribution; provider “user” messages can be routed instructions rather than human speech. Current audit records are marked as self-references, not independent corroboration.

The report's validation checks identifiers, Git object existence, inventory counts, source hashes and the runtime-version predicates. Executing the retained source predicates under the two actual selected interpreters confirmed **both optimizations disabled for Python 3.12.7 / SQLite 3.51.0, and both enabled for Python 3.13.5 / SQLite 3.53.4**. It does not rerun the application workload or manufacture new performance acceptance. Use `python3 docs/investigations/refresh-timeout-history/verify_inventory.py` to check the retained records against this checkout.

An independent reviewer checked the retrospective against the issue and runtime receipts. Its three corrections—persistence-specific failure language, preservation of user-directed scope/pause decisions and one UTC date—were applied. The [review and resolutions](review.json) remain with the report.

No fresh performance experiment was run during this sweep: concurrent test/build activity was itself part of the observed contention, and the assignment was to reconstruct the historical evidence. The exact commands and query substrate used by each reviewer are preserved in the structured coverage records.
