# Runtime updates after native child-agent activity

The November 9 local dashboard rollout failed before any backend restart. The
serving runtime rejected its update because `RuntimeRecovery.prepare()` could
not pass the global `BrowserPersistence.flush()` barrier. The prior runtime
remained serving, with admissions open; the candidate never started.

## Cause

Codex's shared app-server connection publishes lifecycle events for both app
conversations and native child agents. The child agents need not have entries
in Ribbon Field's `browser_sessions` or `sessions` tables. The event handler
nevertheless scheduled coordination turn writes for every native thread ID.
That produced missing-session errors; optional execution snapshots also failed
their foreign-key constraint.

A required bookkeeping failure is retained for the lifetime of its persistence
queue. Later writes for that key cannot overtake the failed write, and a global
flush fails even after the native child has completed. The updater correctly
refused to discard unsaved state, but the application had incorrectly treated
provider-owned activity as application-owned state. The generic update error
did not identify the failed keys. Existing recovery tests covered ordinary
continuation, approval exclusions, cancellation, and rollback, but did not
exercise unregistered native child lifecycle events.

## Repair and limits

Lifecycle and usage persistence now check for an app conversation on the
background persistence worker. Provider-owned events remain visible to the
in-memory activity and request tracking; they do not create app conversations
or write app turn state. Optional execution snapshots use an atomic conditional
insert that requires an existing coordination session.

An active provider-owned turn or request still prevents a coordinated restart:
the current recovery implementation cannot independently continue such a
thread. Its diagnostic identifies the thread and asks for natural completion.
Completed child activity no longer poisons future updates. Actual application
save errors remain blocking, with failed-key and pending-write diagnostics;
the repair does not clear failure records or weaken the persistence barrier.

The repair changes source code. It cannot retroactively replace loaded methods
or clear failures inside the already serving old process. That process exposes
no authenticated reconciliation or provider-transfer API. Repeatedly submitting
the same update, installing another interface, or rebuilding the candidate will
not repair its in-memory queue. No code injection or live failure-state editing
is part of this repair.

## Reproduction and validation

`tests/test_runtime_bookkeeping.py` covers:

- Repeated native child lifecycle and usage events without application records,
  foreign-key errors, or failed update barriers.
- Active child work and approvals retained, with restart deferred before any
  interruption or independent continuation attempt.
- Genuine application persistence and registration failures still failing closed.
- Two consecutive guarded updates in separate real runtime processes, following
  child activity, with exact target activation and one parent continuation per
  update. The databases and subprocess providers are disposable test fixtures.

Run the regression with:

```sh
env -u AGENT_COORD_SESSION_ID -u CODEX_THREAD_ID -u AGENT_COORD_DB \
  PYTHONPATH=tests:plugins/agent-coord/scripts \
  python3 -W error::ResourceWarning -m unittest test_runtime_bookkeeping -v
```

## What would establish uninterrupted upgrades

A guarded restart and continuation is not uninterrupted execution. The current
backend owns the provider processes, pipes, pending requests, and decoder state.
Those resources do not survive replacing that owner through the existing
restart path. The [runtime update contract](runtime-updates.md#uninterrupted-backend-rollout-investigation)
records this separate limitation and its first-adoption boundary.

The independent `scripts/runtime_live_acceptance.py` gate must pass before
claiming uninterrupted upgrades. Evidence must include the committed target
and new coordinator code, unchanged provider process identities and native turn
IDs, uninterrupted long-running commands, retained approvals, and no duplicate
submissions or replacement continuation turns. Repeat updates during concurrent
traffic and exercise coordinator crashes, replay boundaries, and rollback.
Passing the bookkeeping regression alone does not meet that acceptance contract.

The architecture requires provider ownership and a recoverable command/event
boundary outside the replaceable coordinator, or an equivalent verified state
transfer mechanism. The old installed process does not implement that transfer
contract, so its first adoption must be reported separately from future upgrades.

## November 9 validation

The isolated five-test repair regression passed, including both consecutive
guarded updates. The complete frozen-source suite ran 2,408 tests in 565.7
seconds with seven skips and one existing failure: the rollback test's injected
candidate opens an unclosed SQLite connection and then asserts that its log
contains no `ResourceWarning`. That fixture failure was also reproduced on
unchanged source during the preceding dashboard validation.

The independent uninterrupted-upgrade gate remained red: the first target was
still pending after 60.009 seconds, with the previous runtime instance active.
Its observer had no errors. This is explicit evidence of a missing upgrade
capability, not evidence that the bookkeeping repair provides live replacement.

Matched database admission checks used Python 3.13.5 / SQLite 3.53.4, 15 seconds,
six foreground writers, one background writer, two readers, and 128 KiB chunks:

| Measurement | Baseline `375fe10` | With repair |
| --- | ---: | ---: |
| Durable receipts/second | 250.166 | 249.900 |
| Foreground p95 | 2.065 ms | 1.899 ms |
| Writer hold p95 | 1.372 ms | 0.562 ms |
| Workload errors | 0 | 0 |
| Admission timeouts | 0 | 0 |

Both passed the unchanged budgets. These individual matched runs establish a
passing gate, not a statistically established latency improvement. The repaired
backend fingerprint is
`c59d3dbfa6c3d64f3242cfa0d56c6af2618b1d456c2ae7aa149182de554b85ae`.

Matched HTTP workloads also passed every unchanged budget with zero errors:
six streaming agents at six deltas/second, three windows, three hook writes/second,
1.5 MB histories, 15 seconds of warmup, 30 sends, and 15 foreground read cycles.
Send acceptance p95 was 58.0 ms before and 87.2 ms with the repair (400 ms budget);
conversation read p95 was 14.1 / 12.5 ms. The measurements show passing budgets;
they do not establish identical latency or an improvement in tail latency.
