# Attempt coding rubric

Version 1.0. Frozen with the [analysis plan](analysis-plan.md) before any saga
is coded. Changes after the freeze are recorded in the plan's amendment log;
codes are never silently re-derived under a changed rubric.

This file is self-contained so that a second coder can apply it without
reading the analysis plan's hypotheses or any first-coder result.

## Units

**Saga.** The attempts addressing one user-visible symptom or one issue
family, identified by a symptom statement in the words of a user report or
issue.

**Attempt.** One assignment, the commits that resulted from it, and the
acceptance evidence offered for it.

- **Assignment.** The event that directs work at the saga's symptom: a
  human message in a conversation, a routed agent message or delegation, or
  a Beads issue created or claimed with a description of the work. Record
  who issued it (`human`, `agent`, `unknown`). A provider `user` role alone
  does not establish human origin.
- **Resulting commits.** Commits attributable to the assignment by explicit
  reference (issue ID, thread, subject), by authorship time inside the
  attempt window together with file overlap, or by the executor's own report.
  Equivalent parallel commits, preserved snapshots and integration merges of
  the same change count once.
- **Acceptance evidence.** The record offered to establish completion: an
  issue close reason, a completion checkpoint, a final report, a commit
  message claim, a measurement or receipt. An attempt without any acceptance
  record is `abandoned` or `investigation_only`.
- **Boundaries.** A new attempt starts when a new assignment is issued after
  an earlier attempt's acceptance claim, abandonment or explicit pause.
  Corrections inside one assignment before any acceptance claim belong to the
  same attempt. Pre-planned subtasks of one decomposition are separate
  attempts only if each has its own acceptance record.

**Recurrence.** After an acceptance claim, a report of the same symptom, a
Beads reopen, or a further attempt on the same symptom. The observation window
is 14 days after the acceptance claim. An attempt accepted less than 14 days
before collection with no recurrence is `censored`, not `resolved`.

## Primary code

Assign exactly one primary code. It answers: *what was the proximate reason
this attempt did not end the saga?* Ask the questions in order and assign the
first that is answered "yes" from the record. Record every other "yes" answer as
a contributing flag.

| Order | Code | Question |
| ---: | --- | --- |
| 0 | `terminal_resolved` | Was there no recurrence or further same-symptom attempt within the window, with the window fully observed? |
| 0 | `terminal_censored` | Was there no recurrence yet, but less than 14 days observed? |
| 0 | `terminal_open` | Is this the latest attempt of a saga still unresolved at collection, without a later attempt to judge it by? |
| 1 | `delivery_failure` | When the symptom was next reported, was this attempt's validated change absent from the running system: never delivered or activated, superseded by a build or configuration without it, or disabled by runtime or plugin drift? |
| 2 | `scope_decision` | Did the user explicitly narrow, defer, pause or redirect the work, in recorded words, so that the next attempt or the recurrence concerns the deferred part? |
| 3 | `wrong_diagnosis` | Did later evidence show that the mechanism this attempt targeted was not a material cause of the reported symptom? |
| 4 | `verification_failure` | Did the attempt's acceptance record claim an outcome that later evidence shows was not met, where the acceptance evidence did not exercise the condition that failed (smaller or different workload, different environment, test not covering the path, measurement defect)? |
| 5 | `incomplete_fix` | Did a later attempt rework the same mechanism or code path because this change was partial or defective, while this attempt did not claim more than its evidence established? |
| 6 | `narrower_scope` | Was this change correct for a stated, narrower target, with the later attempt addressing a different mechanism under the same symptom? |
| 7 | `environment` | Was the recurrence attributable to host or external conditions (machine load, disk, power, operating system, third-party service) with no product defect identified? |
| 8 | `indeterminate` | Does the record not permit any of the above? |

### Discrimination rules

- **Verification failure versus incomplete fix or narrower scope.** Use
  `verification_failure` only when the acceptance record *claimed* the outcome
  that later failed. When the attempt stated a narrower target or disclosed
  the residual, use `incomplete_fix` (same mechanism reworked later) or
  `narrower_scope` (different mechanism addressed later).
- **Narrower scope misread.** For `narrower_scope`, set `misread_as_complete`
  when a user, orchestrator or issue state treated the scoped result as
  resolution of the whole complaint. Quote the record.
- **Delivery failure** requires evidence of what was running, or explicit
  evidence that the change was not installed/activated. A recurrence alone
  does not establish it.
- **Scope decision** requires the user's recorded words. An agent's statement
  that work is "out of scope" is not a user decision; code it under the
  question it actually answers and set `agent_scope_limit`.
- **Environment** is primary only when no product mechanism was identified.
  Otherwise set it as a flag.
- **Wrong diagnosis** requires later evidence that the target was not
  causal. A mechanism that was causal but not sufficient is `narrower_scope`
  or `incomplete_fix`.

## Contributing flags

Set each that the record supports: `delivery_failure`, `scope_decision`,
`wrong_diagnosis`, `verification_failure`, `incomplete_fix`, `narrower_scope`,
`environment`, `misread_as_complete`, `agent_scope_limit`,
`overclaim` (completion claimed beyond the evidence),
`bundled_symptom` (the assignment's label covers two or more mechanisms later
shown distinct), `architectural_bound` (the attempt or its evidence names a
structural limit that a local change could not remove).

## Descriptors

Record from the attempt's own records. Use `unknown` rather than infer.

| Field | Values |
| --- | --- |
| `dod_explicit` | `yes` (measurable acceptance criteria stated in the assignment or issue), `partial` (outcome stated but not checkable), `no` |
| `instruction_issue` | `none`, `ambiguous`, `conflicting`, `embedded_wrong_cause`, `unknown`; quote the instruction |
| `acceptance_evidence` | any of `none`, `unit_tests`, `integration_tests`, `synthetic_workload`, `live_measurement`, `user_confirmation`, `activation_receipt`, `review` |
| `workload_match` | `matched`, `weaker`, `different`, `unknown`, `not_applicable` |
| `delivery_state_at_claim` | `source_only`, `installed`, `activated_verified`, `unknown`, `not_applicable` |
| `tool_or_access_gap` | `none`, `recorded` (quote what could not be observed or done), `unknown` |
| `executor_kind` | `app_agent`, `terminal_delegate`, `provider_subagent`, `direct_session`, `unknown` |
| `client`, `model`, `effort` | Value and provenance: `browser_session_current_selection`, `usage_sample`, `delegation_record`, `self_report`, `unrecorded` |
| `concurrency` | Number of other agent turns active in the workspace during the attempt window and its source, or `unrecorded` |
| `host_condition` | Quoted host observation, or `unrecorded` |

## Evidence citation

Every code cites at least one record identifier: commit hash, Beads ID and
state transition, thread ID with turn or item ID, coordination message ID,
checkpoint ID or receipt path. Quote the words relied on. When records
conflict, cite both and code from the later, more direct evidence.
