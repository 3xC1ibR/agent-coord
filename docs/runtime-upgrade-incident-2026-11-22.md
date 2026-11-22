# November 22 runtime upgrade incident

The build 52 rollout passed the 2,633-test suite (26 skips), repeated fixture
upgrades, failure/recovery workloads, performance budgets and a fresh independent
Claude review. It nevertheless failed during real provider use. The acceptance
decision covered lifecycle continuity but did not cover enough legal protocol
values or retained adapter-state shapes.

## Failure and recovery

Guarded build 51→52 adoption committed at 05:21:25 UTC. The legacy drain could
not prove a clean stop; reviewed resume retained ten uncertain conversations and
two withdrawn approvals without replay. A subsequent native Codex
`item/commandExecution/outputDelta` encountered `aggregatedOutput: null`.
The adapter used `item.get(field, "") + delta`, which does not replace a present
null value. The shared projection retried event 1238 indefinitely, retaining the
journal but blocking every provider channel at cursor 1237.

The machine rebooted at 05:30:42 UTC, ending all pre-reboot processes. Cold-owner
maintenance then exposed a second defect: the codec encoded tuples as arrays
but decoded mapping keys and set members as lists, which are unhashable.
Repair commits `e84f1ee` and `f6e2b32` corrected null normalization and compound
hashable decoding. Build 54 restored projection and 13 explicit conversation
continuations. The reboot and new transport epoch do not prove uninterrupted
execution. The planned following live upgrade never ran.

## Why the gates and review missed the defects

The wire fixture initializes agent text to `""`, omits `aggregatedOutput` from
its command items, and emits agent-text deltas rather than command-output deltas.
Its long tool and approval scenarios therefore never exercised a present null
followed by output. The existing codec test covered integer mapping keys,
string set members and an empty set; it had no tuple key or tuple member.
Repeated replacements of those fixture states did not add the missing cases.

The fresh review checked source/evidence identity, ownership, rollback and the
reported workloads. It relied on that same input corpus. A different reviewer
provider supplied an independent assessment, not independent native-protocol or
state-shape coverage. Passing the suite did not establish either completeness
claim, and the rollout decision should not have treated it as sufficient.

## Gates that would have caught them

- Replay legal nullable `item/started` text and command-output fields through
  the durable projection, followed by multiple deltas, completion and a later
  event on another provider channel. Assert exact persisted output and cursor
  advancement. The repair adds the adapter regression and reproduces retained
  event 1238; a projection-level replay is the stronger release gate.
- Round-trip compound hashable state through JSON and restore a checkpoint in
  ordinary restart and lost-owner maintenance. Include nested tuple mapping
  keys and tuple set members. The repair adds those codec cases and a tuple
  member to the lost-owner test. Fixture state must represent native adapter
  state before repeated-upgrade results are accepted.

The repair ran 127 browser-session and 21 projection/recovery tests plus the
database budget gate; it did not repeat the complete suite or live continuity
acceptance. The original acceptance results remain evidence for `6d636c9`,
not proof of an uninterrupted live rollout or complete repair coverage.

Operator evidence remains local: the integration worktree's
`build/uninterrupted-integration-validation/`,
`build/uninterrupted-integration-review/` and
`build/uninterrupted-runtime-execution/receipts/`; and the main checkout's
`build/runtime-incident-recovery/`. Current status and limits are recorded in
[the update contract](runtime-updates.md#live-adoption-incident-and-reboot-recovery).
