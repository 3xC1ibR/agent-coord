# Provider ownership during coordinator replacement

This document specifies the implemented ownership boundary and acceptance
contract for uninterrupted coordinator upgrades. Isolated validation does not
mean that the installed legacy runtime supports the contract; first adoption
is a separate operation described below.

The November 22 build 60-to-62 update supplied observed continuity evidence for
sampled Codex and Claude work: retained provider PIDs, native execution, long
tools, pending approvals and submission identity. The earlier build 52 adoption
and build 54 reboot recovery interrupted execution. See the
[incident note](runtime-upgrade-incident-2026-11-22.md) and
[current rollout evidence](runtime-updates.md#observed-following-upgrade).

The failed-launch repair was published in build 65 at `9177309`, with independent
old/new-owner and downgrade checks. A separate build 63 replacement retained
observed provider work and cleared the original delivery warning. Further local
deployment was then held; build 65 has no installed-runtime continuity result.
See the [published correction and local boundary](runtime-updates.md#failed-launch-recovery-and-published-correction).

## Execution boundary

A stable, provider-neutral transport owner retains the provider processes and
their standard-input, standard-output, and standard-error pipes. Its work is
limited to process ownership, framing, authenticated ownership fencing, a
bounded event journal, and command delivery records. Provider adapters,
conversation state, scheduling, and coordination logic execute in the
replaceable coordinator.

The supported live adapters are Codex and Claude. An active Kimi, Antigravity,
or Muse workload defers a passive replacement. An explicit update pauses those
turns within a bound and continues each confirmed turn once in its saved native
session; see [explicit updates with coordinator-owned providers](runtime-updates.md#explicit-updates-with-coordinator-owned-providers). Candidate metadata must declare compatible
transport and projection versions. This contract covers replacement of the
coordinator and its adapters; it does not replace the transport owner itself,
upgrade an already running provider executable, or survive a machine restart.
An incompatible transport change requires a separate adoption procedure.

Explicit Stop is a separate operation. It terminates the retained provider
process groups and confirms that both coordinator and transport ownership have
ended. If a process or pipe cannot be accounted for, shutdown remains incomplete
and must not produce a successful stopped-work proof.

Each new transport instance has a separate journal generation. Reopening after
a confirmed Stop starts with a fresh cursor while retaining old command
evidence. A cancelled trial closes its commit pipe before waiting for the
candidate to exit, so the candidate can release its discovery record normally.

An unavailable transport endpoint alone does not prove that its providers have
stopped. Recovery requires exclusive ownership and proof that the record belongs
to an earlier boot, or that the owner never opened a provider. Ambiguous
same-boot failures remain explicit errors with evidence retained; recovery must
not signal reused PIDs or replay commands. Recovery after an earlier boot marks
affected execution interrupted and pauses queued work. It does not claim
execution continuity across a reboot.

Failed explicit Stop operations retain their original exception with the
runtime, database and transport identities. A waiting command reports that
failure only after both ownership locks are free and the retained transport
still matches. This is an unconfirmed shutdown, never a stopped-work proof.

A successful upgrade must execute the candidate coordinator and its adapters.
Changing a reported payload identifier, forwarding application requests to the
old coordinator, or resuming the conversation in a new native turn does not
satisfy the contract. Verification must establish the executing process and
code identity as well as the selected payload.

## Delivery and recovery

Every outbound command has a durable identity bound to its content. Retrying a
known identity returns its recorded outcome; it does not write the command
again. A command whose delivery cannot be established remains explicitly
uncertain. A saved intent alone is insufficient evidence that a provider
received a command.

Provider events have an ordered cursor. Projection effects and the committed
cursor must share a transaction, so recovery cannot retain one without the
other. Versioned JSON state preserves native turn identities, original approval
keys, adapter state, and event-stream continuity. Locks, threads, and Python
future objects are reconstructed rather than serialized.

Only the current fenced coordinator may issue new commands. Replacement closes
new admission and drains accepted application operations without terminating
providers. Candidate failure must preserve a recoverable owner and the event
prefix needed by the retained coordinator. Incompatible state formats defer
activation explicitly. Unsupported active providers defer passive activation;
an explicit update pauses them, never withdraws an approval, and records the
exact interrupted turns before signalling any provider.

Journal retention and replay batches must have byte bounds. A slow provider
must not hold a shared journal or coordination-database lock while writing to
a pipe or waiting for a process. Parsing, network I/O, and process waits remain
outside coordination write transactions.

The transport's `acknowledge_events(cursor, retain_after)` operation separates
committed delivery from permission to discard recovery data. The caller must
retain the oldest cursor that any rollback can restore. Each call removes at
most 256 eligible events; repeated calls finish maintenance without one long
write scope. Requests below the retired cursor fail explicitly. Sequence
numbers remain monotonic after the event journal becomes empty.

Resource limits are explicit: 64 MiB native frames, 512 MiB of retained event
data measured after JSON escaping, 512 concurrent provider channels, and one
million compact command receipts. Receipts retain content hashes and outcomes;
they are never evicted in a way that would permit duplicate execution. Reaching
the receipt limit rejects new commands and requires maintenance. Five-second
pipe-write deadlines mark incomplete delivery as uncertain and refuse later
frames on that channel without terminating the provider. Physical SQLite files
may retain their allocated high-water size after logical event pruning.

## Verification

The existing [`runtime_live_acceptance.py`](../scripts/runtime_live_acceptance.py)
workload remains an unchanged regression gate. It runs the actual backend in
private runtime directories with wire-level fixture providers. The candidate
must activate within 60 seconds while Codex streaming and tool turns and a
Claude approval remain active. Native turn IDs and provider processes must
survive; output, approvals, queued commands, and tool execution must not be
lost or duplicated. Visible output stalls must remain below five seconds.

Independent additive validation must also establish actual candidate execution,
successful tool completion, preserved approval identities, event replay,
commands arriving during replacement, coordinator crashes, and rollback.
Connection gaps must be measured and reported rather than silently ignored.
Synthetic-provider HTTP tests do not measure transport overhead; sustained
wire traffic needs a separate measurement. Database and application latency
must remain within the existing [responsiveness budgets](responsiveness.md).

Run both repeated-upgrade checks from the checkout being evaluated. Select the
interpreter recorded in the installed payload's `backend.json`; do not assume
that the shell's `python3` is the packaged or installed interpreter. The
November 9 validation used `/Users/walle/.pyenv/versions/3.12.7/bin/python3`.

```bash
runtime_python=/Users/walle/.pyenv/versions/3.12.7/bin/python3
env -u AGENT_COORD_SESSION_ID -u CODEX_THREAD_ID -u AGENT_COORD_DB \
  "$runtime_python" scripts/runtime_live_acceptance.py --swaps 50 \
  --report build/uninterrupted-original.json
env -u AGENT_COORD_SESSION_ID -u CODEX_THREAD_ID -u AGENT_COORD_DB \
  "$runtime_python" scripts/runtime_upgrade_validation.py --scenario steady --swaps 50 \
  --report build/uninterrupted-independent.json
```

The additive driver also provides idle-control, rollback, candidate-crash,
coordinator-crash, coordinator-termination, and Stop-during-preparation
scenarios. It records executing payload identity, original-turn event delivery,
connection gaps, command receipts, process lifetime, and tool exit status.
Its reports distinguish output belonging to a queued follow-up turn from the
original turn and drain through the original completion event before comparing
the event stream.

### Repeated-upgrade evidence

On macOS 26.0.1 arm64 with Python 3.12.7, both fifty-upgrade checks passed with
the original provider processes and native turns retained. The unchanged gate
passed 544 assertions; the independent driver passed 1,158. This boundary used
`provider_transport.py` SHA-256 `326da309`, `desktop_runtime.py` `dd51b80d`,
`codex_app_server.py` `b8317798`, and `coordinator_projection.py` `b2f23e29`.

| Measurement | Original gate | Independent driver |
| --- | ---: | ---: |
| Coordinator activation p95 | 2.31 s | 2.93 s |
| Maximum coordinator activation | 2.36 s | 3.82 s |
| Maximum interval between visible stream updates | 1.70 s | 1.75 s |
| Longest measured REST outage | Not asserted by this gate | 0.56 s |
| Maximum event-stream reconnection | Not measured by this gate | 0.42 s |

Providers keep executing while the interface reconnects. These measurements
establish bounded interface gaps, not uninterrupted HTTP availability. The
tests use wire-level fixture providers; they do not establish compatibility
with every future provider binary. Coordinator termination, coordinator crash,
candidate crash, and rollback also passed on this boundary.

The complete Python 3.13.5 suite passed 2,479 tests with seven skips before the
targeted bootstrap and Python 3.12 compatibility corrections. Existing fixture
connection-cleanup warnings also reproduce on the baseline. The supported
Python 3.12 lacks `os.waitid`; the transport uses a verified Darwin 64-bit libc
binding when that native Python API is unavailable. SDK layout checks cover
arm64 and x86_64; execution was tested on arm64. Bundled Python 3.12.15 and an
x86_64 Python runtime were not executed in this validation.

After the restart corrections (`provider_transport.py` `20db8452`,
`desktop_runtime.py` `c1b06c88`), integrated checks on revision `577a1d6` plus
the feature ran on Python 3.12.7: 142 runtime tests passed; 108 transport,
projection and packaging tests passed with seven skips. Regression cases
complete a new turn after both ordinary Stop and cancellation during trial
preparation, verify the newest submitted message, reject stale discovery, and
retain unregistered native-child evidence without treating it as an app agent.
Previous-boot recovery was tested using simulated kernel boot identities; no
actual reboot was performed. Same-boot provider ambiguity is deliberately
refused with evidence retained.

A final diagnostic-only refinement (`desktop_runtime.py` `c8f5ec09`) captures
failure during adapter cleanup before the later Stop handler is reached. Eight
focused Python 3.12 checks and four existing Stop/update process checks passed.
The first Stop after a killed fixture transport returns the original failure,
retains its owner record, produces no stopped proof, and does not replay the
turn. Tests reject mismatched identities and defer while either lock is held.
The integration checkout independently repeated those eight checks successfully.
Final independent Python 3.12 runs passed 113 assertions over three further live
upgrades, 21 for Stop/reopen with a newly completed turn, and 12 for transport
owner failure with retained evidence and no false stopped-work proof. The
reports are `frozen2-312-*` in the independent review artifacts.

The additive observer now starts a fresh sequence after an explicit event-stream
reset, while retaining the reset time for validation. An explicit Stop permits
one later reset; resets during live coordinator replacement still fail. Earlier
Stop runs that compared a new journal's sequence against the stopped journal's
sequence remain recorded as failed observer checks, not production failures.

An earlier HTTP measurement failed its zero-retry health gate with one
background SQLite retry despite passing latency budgets. Four fixed diagnostic
runs, before/after twice, did not reproduce it. The failed run remains evidence;
it is not reclassified as a pass or proof of a corrected database defect.

### Matched performance measurement

The final Python 3.12.7 comparison used baseline revision `577a1d6` and the
integrated candidate. Measured Python source fingerprints were `eca8801e`
(baseline) and `12ed6710` (candidate). Subsequent restart-cleanup changes require
their own lifecycle checks; they are outside the synthetic HTTP workload.

The database workload ran for 15 seconds with six foreground writers, one
background writer, two readers, and 128 KiB background chunks. Both versions
passed the unchanged budgets with zero errors and zero admission timeouts.

| Database measurement | Baseline | Candidate |
| --- | ---: | ---: |
| Foreground receipts per second | 224.56 | 229.72 |
| Foreground latency p95 | 6.405 ms | 4.328 ms |
| Writer hold p95 | 0.913 ms | 0.817 ms |

The same database gate also passed after the restart corrections, source
fingerprint `17843a87`: 230.17 foreground receipts per second, foreground p95
2.468 ms, writer-hold p95 0.868 ms, zero errors and zero admission timeouts
(`uninterrupted-integration-database.json`).
After the diagnostic-only refinement, fingerprint `191f08aa`, it passed again:
243.20 receipts per second, foreground p95 8.122 ms and writer-hold p95 0.824 ms,
with zero errors or admission timeouts (`uninterrupted-final-database.json`).
Run-to-run variation is retained rather than selecting only the fastest run.

The HTTP workload used the same private database snapshot, six streams at
6 Hz, three windows, external writes at 3 Hz, and 1,500 KiB histories. It
included 30 sends and 15 reads of each selected endpoint. Both versions passed
all existing latency and health budgets with zero client errors, persistence
retries, final persistence failures, or admission timeouts.

| HTTP measurement, p95 | Baseline | Candidate |
| --- | ---: | ---: |
| Send accepted | 52.9 ms | 20.4 ms |
| Send event visible | 40.7 ms | 14.7 ms |
| Conversation read | 12.1 ms | 12.7 ms |
| Thread details | 5.0 ms | 6.0 ms |
| Thread list | 0.7 ms | 0.8 ms |
| Search | 7.4 ms | 9.5 ms |

These finite observations establish budget compliance under this workload, not
a causal speedup or zero overhead. The synthetic HTTP provider bypasses the
transport owner; wire-level load and repeated replacement are separate tests.
The retained reports are `uninterrupted-release-python312-{database,http}-`
`{before,after}.json` under the local analysis directory
`~/.local/state/agent-coord/analysis/coordination-token-usage/dashboard/`.

A separate Python 3.12.7 wire test delivered all 3,600 events exactly once from
six channels at 60 Hz for ten seconds. Delivery p95 was 8.44 ms; journaled
request/echo p95 was 2.98 ms versus 0.05 ms through a direct pipe. A blocked
writer did not block another channel, whose command completed in 1.31 ms.
These observations include the transport cost that the synthetic HTTP test
excludes (`uninterrupted-release-transport.json`).

The large-history test retained 150 archival turns, 2,634,000 characters per
conversation, across six simultaneous wire-level streams. Three coordinator
replacements completed within 2.98 seconds each. All 6,184 output events were
preserved exactly in REST and the event stream; no provider was interrupted.
The largest visible event gap was 1.62 seconds, REST p95 was 112.54 ms, and
writer-hold p95 was 1.38 ms, with zero projection errors or admission timeouts.
Expected HTTP/event-stream reconnections during replacement are recorded in
`uninterrupted-release-large-history.json`; this test does not claim continuous
HTTP availability.

## First adoption from a legacy runtime

A legacy coordinator owns provider pipes, pending requests, decoder buffers and
adapter state inside its process. It cannot transfer those resources. When it
advertises `runtime_updates: 1`, one ordinary guarded **Update and Continue** can
adopt the stable-owner backend without waiting for every native turn to finish.
That first transition interrupts and continues eligible saved conversations under
the existing recovery rules; it is never counted as uninterrupted execution.

The candidate retains the legacy ready/commit handshake and establishes its
stable supervisor only after durable commit, before admitting recovery or new
work. An inherited source-exit pipe retains the database owner lock through the
directly adopted coordinator's lifetime. OS pipe EOF proves its exit before a
planned snapshot or crash restart; no reusable source PID establishes that proof.
Subsequent supervised children carry an explicit marker and do not create a
second monitor. The frozen legacy control fixture is revision `6d3a900`.

First-adoption validation must cover both the guarded transition and a following
compatible upgrade with a running native turn. It must also crash the directly
adopted coordinator and prove that provider execution survives that recovery.
Finish pending approvals and explicitly arrange continuation for active Kimi,
Antigravity or Muse work before adoption. The old backend executes the shutdown
drain; an old drain failure can leave the new backend committed and paused for
review. Inspect external outcomes and exact recovery states before authenticated
resume. Do not replay uncertain actions.

Source validation, local installation, backend activation, the following live
upgrade and public publication are separate outcomes. Preserve their exact
payload identities and evidence. The earlier rollout failure and source repair
are documented in [`runtime-bookkeeping-recovery.md`](runtime-bookkeeping-recovery.md).
