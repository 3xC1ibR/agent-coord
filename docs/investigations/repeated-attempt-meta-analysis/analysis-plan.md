# Analysis plan: repeated attempts at the same problem

Version 1.0, written 2026-11-10 UTC (2026-11-09 America/Chicago) before any
saga screening or attempt coding. The rubric is in
[coding-rubric.md](coding-rubric.md). After the freeze commit, every change is
appended to the amendment log at the end with its time and reason. Earlier text
is not rewritten.

## Questions

1. When agent work in `/opt/projects/agent-coord` is attempted repeatedly for
   what looks like one problem, what proximate causes account for each further
   attempt?
2. Is the premise "agents repeatedly failed" correct, partly correct, or wrong?
3. Which changes to assignment and acceptance practice would measurably reduce
   further attempts, and how will a prospective trial test them?

## Sources and universe

The analysis uses raw records. The
[refresh-timeout history](../refresh-timeout-history/README.md) is an index and
hypothesis source; its findings are not adopted as codes.

| Source | Universe | Access |
| --- | --- | --- |
| Git | All commits reachable from all local refs (33 refs) plus reflog-only commits | `git log --all`, `git reflog --all`, `git show` |
| Beads | All issues, every status; notes, close reasons, comments; status transitions from `.beads/interactions.jsonl`; `bd history` for central issues | `bd list --all`, `bd show --json --include-comments`, `bd history` |
| Saved conversations | All work threads in all workspaces; original requests; indexed user-role prose; checkpoints; coordination messages; delegations; activity events | Read-only SQLite (`mode=ro`, `query_only`) and `agent-coord activity history`; no provider is resumed |
| Runtime receipts | Retained payload configurations, validation and activation receipts referenced by the inventories | Read-only file inspection |
| Prior inventories | `commits.json`, `beads.json`, `conversations.json`, `runtime-evidence.json`, `verification.json` | Read-only; used as pointers to raw records |

Collection code is kept in this directory. Bulk extracts are written to a
scratch directory outside the repository and are not committed.

## Saga inclusion criteria

A candidate symptom family is **retained** when all of the following hold.

- **I1 Workspace.** The work changed this repository, its Beads database or
  its delivered runtime, or was conducted in a conversation rooted in this
  workspace.
- **I2 Same symptom or issue family.** The attempts share one user-visible
  symptom statement, or are linked as one issue family by parent, dependency,
  reopen or explicit cross-reference.
- **I3 Repetition.** Either three or more distinct attempts address the
  symptom, or at least one recurrence follows a claimed fix: a Beads
  closed-to-open transition, a report of the same symptom after a completion
  claim, or a commit that re-fixes or reverts a claimed fix.
- **I4 Evidence.** At least one attempt has an identifiable assignment and an
  identifiable outcome record (commit, close or checkpoint).

A candidate is **excluded** and logged with the reason when:

- **E1 Lexical coincidence.** Records share a word but not a symptom.
- **E2 Planned decomposition.** Sequential pre-planned milestones or subtasks
  were each delivered as scoped without reopen or recurrence. Feature work in
  phases is not repetition.
- **E3 Other workspace.** The work belongs to another repository. It is counted
  in the coverage table.
- **E4 Insufficient record.** I4 fails.

Screening signals, merged into candidate families before I1–I4 are applied:

- Beads closed-to-open transitions; issues whose title or description names
  the same symptom; parent and dependency families; recurrence wording
  ("again", "still", "regress", "recur", "reopen").
- Git subjects and bodies with repair wording (fix, repair, restore, recover,
  prevent, preserve, again, regress, revert), repeated subjects, and file
  hotspots repaired three or more times.
- Human-origin messages with recurrence wording ("again", "still", "keeps",
  "over and over", "not fixed", "same issue", "why did you fail", "broke",
  "back"), and thread titles or original requests naming a symptom already
  reported in an earlier thread.

Each retained saga receives a symptom statement quoted from a user report or
issue, and a list of attempts with record identifiers.

## Attempt coding

Every attempt in every retained saga is coded under
[coding-rubric.md](coding-rubric.md). Subagents may code independent sagas
only under the frozen rubric and must return the same machine-readable fields.
The lead checks each subagent-coded attempt against its cited records before
accepting it.

## Hypotheses and pre-stated tests

Patterns are stated over non-terminal attempts (attempts followed by a further
attempt or recurrence) unless noted. "Most" means more than half of the
relevant denominator. Every finding reports supporting and contradicting
attempt counts.

| ID | Hypothesis | Supported if | Refuted if |
| --- | --- | --- | --- |
| H1 | Agents repeatedly failed at the work they were given | `wrong_diagnosis` plus `incomplete_fix` is the largest primary group, and occurs in attempts with an explicit definition of done and matched acceptance evidence | Most non-terminal attempts are `narrower_scope`, `scope_decision`, `delivery_failure` or `environment`, or failures cluster where the assignment lacked a definition of done or a matched workload |
| H2 | Distinct defects were bundled under one symptom label | Within a saga, consecutive attempts target different confirmed mechanisms (`narrower_scope` chains, `bundled_symptom` flags) | Consecutive attempts rework the same mechanism |
| H3 | Correct scoped fixes were read as whole-system completion | `narrower_scope` attempts with disclosed residuals carry `misread_as_complete` | Residuals were undisclosed (that pattern supports H4/overclaim instead), or no scoped attempt was misread |
| H4 | Acceptance evidence did not match the live workload | `verification_failure` with `workload_match` of `weaker` or `different` is a leading primary or flag among recurrences | Recurrences follow acceptance on matched workloads |
| H5 | Delivery or runtime drift undid validated work | `delivery_failure` occurs, with evidence the validated change was not running at recurrence | The validated change is shown running when the symptom recurred |
| H6 | A known architectural bound defeated local fixes | `architectural_bound` names the same constraint across three or more attempts and the symptom persists after the local fixes | The symptom ends after local fixes without the structural change |
| H7 | A definition of done was missing | Among comparable attempts, those with `dod_explicit = no` recur more often than those with `yes` | Attempts with explicit criteria recur at a similar rate |
| H8 | Concurrency and host load caused recurrences | `environment` is a frequent primary or flag, or recurrences concentrate at recorded high load or concurrency | Recurrences occur with a product mechanism identified and no recorded load association |
| H9 | Instructions were unclear or wrong | `instruction_issue` other than `none` precedes `wrong_diagnosis` or scope gaps | Clear instructions precede the same failure pattern |
| H10 | The model or effort was too weak | Non-terminal attempts concentrate in lower-capability models or efforts, *and* the elicitation test fails (below) | The same model, effort and harness succeed on comparable tasks in the record, or failures are spread across models |
| H11 | Tools or access were missing | `tool_or_access_gap = recorded` precedes `wrong_diagnosis` or `verification_failure` | Needed observations were available and the gap is unrelated to the outcome |

### Model elicitation test

A claim that a model was too weak must show all of the following.

1. **Capability.** On comparable tasks in this record (same workspace, similar
   subsystem and size), the same model, effort and harness did not succeed when
   given a definition of done and matched acceptance evidence.
2. **Unattended reliability is kept separate.** Failures with
   long unattended runs, interruptions or lost context are reported as
   reliability, not capability.
3. **Harness and verification are kept separate.** Failures attributable to
   the acceptance gate, tools, access or delivery are not attributed to the
   model.
4. **External claims** about model capability are searched on the run date,
   dated and sourced. Unsourced capability claims are not used.

Model and effort per attempt are recorded with provenance. Where only the
thread's current selection is recorded, that limitation is stated and the
variable is treated as approximate.

## Validity procedure

### Held-out saga

After screening, the retained sagas other than the refresh-timeout saga are
listed by saga ID, and one saga (two if six or more are retained) is drawn with
`random.Random(20261109).sample`. The draw and its code are recorded. Held-out
sagas are not read beyond screening until predictions are written. After the
findings are derived from the remaining sagas, explicit predictions about the
held-out saga's code distribution are committed. The held-out saga is then coded
by an independent agent that is given the rubric but not the predictions, and the
predictions are scored.

### Blind second coder

A seeded random sample of at least 30 percent of derivation attempts (minimum
12, stratified by saga, seed `20261109`) is re-coded by a fresh agent, preferably
from a different provider. It receives the rubric file and evidence packets
containing record identifiers and quotations, and may consult the raw records.
It does not receive first-coder codes, hypotheses or conclusions, and no
conclusions are written to repository files or checkpoints until its coding is
returned. Agreement is reported as percent agreement and Cohen's kappa on the
primary code and per-flag agreement. Each disagreement is listed with both codes,
the resolution and its reason. First-coder and second-coder codes are both
retained.

### Statistics policy

The expected number of attempts is small. Counts are always reported with
denominators. When a comparison cell holds fewer than five attempts, no
percentages or significance tests are reported; the argument is made by
structured comparison of attempts. Any test reported is labelled exploratory.

### Unanswerable questions

A question the retained record cannot answer is labelled unanswered, with the
smallest instrumentation that would answer it next time. Missing variables are
not imputed.

## Recurrence window and censoring

Recurrence is observed for 14 days after an acceptance claim. Collection ends
at the collection timestamp recorded in the dataset. Attempts accepted within 14
days of collection without recurrence are `terminal_censored`.

## Outputs

- `README.md`: ranked causes with falsification records, practice changes by
  group, a stop-doing list, coverage and unanswered questions.
- `analysis-plan.md` and `coding-rubric.md`: this plan and the rubric.
- `saga-inventory.json`: screening universe, candidates, retained and excluded
  sagas with reasons.
- `coded-attempts.json`: one record per attempt with first-coder codes,
  second-coder codes where sampled, and resolutions.
- `trial-protocol.md` and a scorecard tool: the pre-registered prospective
  trial.

## Constraints

Read-only on product code, issues and runtime. No provider is resumed to
inspect a conversation. No issue status is changed. Unrelated worktree edits are
preserved. Live agent workflow is not changed until the user approves the trial
protocol.

## Amendment log

| Time (UTC) | Change | Reason |
| --- | --- | --- |
| 2026-11-10 05:05 | **A1.** Rubric 1.1 adds primary code `new_defect` (order 2a) and flag `regression_of_fix`. For the hypotheses, `new_defect` counts as support for H2 (distinct defects under one label) and against H1. | During screening, before any attempt was coded, recurring symptoms such as "the app will not start" were found to arise from defects introduced after an earlier fix had held. Version 1.0 had no code for that case and would have forced it into `narrower_scope` or `incomplete_fix`. |
| 2026-11-10 05:05 | **A2.** A saga retained at screening that fails I3 once its attempts are delimited during coding is moved to excluded, with the reason logged in `saga-inventory.json`. | Screening relies on titles, requests and issue records; attempt boundaries are fixed only during coding. |
| 2026-11-10 05:05 | **A3.** Attempt boundaries for orchestrated work: agents spawned by an attempt's executor for that assignment (implementers, reviewers, validators) belong to the same attempt. A new human assignment in the same thread after an acceptance claim starts a new attempt. When the user sends one request to several conversations, each conversation is a parallel attempt and is flagged `parallel_duplicate`. | Clarifies the rubric's boundary rule for app-agent orchestration before coding. |
