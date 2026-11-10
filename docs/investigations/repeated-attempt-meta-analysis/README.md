# Why agent work in this workspace is repeated

Analysis date: November 10, 2026 (UTC). Records collected 2026-11-10T04:33Z. The scope is repeated
attempts at the same user-visible problem in `/opt/projects/agent-coord`, from the earliest records
(August 25) through collection.

This directory holds a pre-registered, coded evidence base; causal findings checked by a blind
second coder and a held-out prediction test; practice changes; and a prospective trial that is
**awaiting your approval**. No product code, issue status or runtime was changed.

## The answer

1. **The premise is partly wrong.** Most repairs held. In a seeded random sample of 29
   defect-repair requests from October 6–26, 22 of the 27 that reached a completion claim had no
   same-symptom report or further attempt within 14 days (81 percent; 24 of 27 under a stricter
   same-symptom reading). Repetition is real but concentrated. Four sagas account for 60 of the 94
   coded attempts: responsiveness (S01), non-Codex provider failures (S15), wrong turn state (S04)
   and missed wakes (S03).
2. **The leading cause is one symptom label covering many mechanisms.** Of 79 attempts that did not
   end their saga, 47 fixed a real cause correctly for what they targeted, and the next report came
   from a different mechanism under the same label (`narrower_scope`). Another 6 recurred from a
   defect introduced later (`new_defect`). Only 12 of those 47 re-measured the user's actual complaint
   on the running app before claiming completion, and 20 were followed by the next report within 24
   hours.
3. **The second cause is a validated fix that was not running when you re-tested.** In 9 of the 79
   attempts, the change was absent from the serving app at the next report: it was waiting for an
   idle handover, not yet rebuilt, or lost to later build drift (`delivery_failure`). The median gap
   between acceptance and your next report was 1.9 hours. In the baseline sample, 14 of 27 fixes were
   not running when the agent claimed completion. This cause did not appear in the held-out saga S02.
4. **"The agents failed" in the narrow sense explains about one attempt in nine.** Wrong diagnosis,
   incomplete fix or a verification failure was primary in 9 of 79. No attempt was coded wrong
   diagnosis. The held-out sagas agree: 3 of 20, again with no wrong diagnosis. The most expensive single failure was a verification failure: the Nov 2
   database repair claimed zero errors on a workload that omitted live sidecar traffic, and it took
   six more days to repair.
5. **Model or effort is not supported as a cause.** The same models and efforts that appear in
   failed attempts held 81 percent of the time on comparable repair requests. In the responsiveness
   saga, the repair that finally passed matched-workload acceptance came from the same model, effort
   and conversation that had produced the verification failure, after the acceptance workload was
   corrected.
6. **Your own decisions and environment contributed but were not the main cause.** Explicit scope or
   pause decisions were primary in 5 attempts. Host conditions (swap exhaustion, a 2 percent battery
   that was not charging, load around 60 on 10 CPUs, provider quotas, an automatic Muse update)
   aggravated 11 attempts and were never the sole cause.

The changes that follow from this are about acceptance, delivery and routing. Model selection is
not among them: re-test the complaint on the running build, report "fixed in source, not active"
honestly, keep one owner per symptom, and give each repair brief its symptom history.

## Evidence and coverage

| Source | Universe searched | Used |
| --- | ---: | ---: |
| Git | 340 commits: 337 reachable from 33 local refs, plus 3 reflog-only | Commits cited by coded attempts |
| Beads | 256 issues, every status; 237 status transitions (7 closed-to-reopened, on 6 issues); notes, close reasons, comments | Issues cited by coded attempts |
| Saved threads | 1,696 work threads across all workspaces; 808 screened (641 rooted here, 167 parent-directory threads that mention Ribbon Field) | 94 derivation attempts plus 22 held-out attempts |
| Saved prose | 4,362 indexed user-role messages; 1,526 indexed histories; full histories read through `agent-coord activity history` (no provider resumed) | Every cited thread |
| Checkpoints, coordination | 18,545 checkpoints; 27,524 coordination messages; 189 delegations | Cited per attempt |
| Activity | 8,903 events: human `message_submitted` from Oct 28, turn lifecycle from Oct 30 (no events Nov 5–7) | Concurrency, executor reliability |
| Runtime receipts | Retained payload configurations, the October 29 campaign receipts (34 files), activation receipts | Delivery coding |

**Sagas.** Seventeen candidate sagas were retained at screening and 14 candidates were excluded, each
with a reason ([saga-inventory.json](saga-inventory.json)). Three more (S10, S12, S17) were excluded
during coding when UTC ordering showed that the apparent recurrence was the report that triggered
the fix (amendment A2). That leaves 12 derivation sagas (94 attempts) and 2 held-out sagas (S02,
S07).

**What the record does not contain.** These are instrumentation findings, not gaps filled by
inference:

- **Model and effort per turn:** only each thread's current selection is saved. Claude threads
  usually record the alias `opus`. Usage samples name the model for 72 of 641 threads.
- **Human origin before October 28:** 47 of 94 assignments are `unknown` origin; provider `user`
  roles are not proof.
- **Which build was serving at each recurrence:** recorded from October 29, and only where an agent
  checked.
- **Lock holder and phase timing** for individual slow requests.
- **Host load and concurrency** before October 30.
- **Commit-to-conversation links:** no trailers. Batch commits combine several attempts, so
  attribution relies on timing and file overlap.
- **Whether you tested a fix yourself:** no event records user verification.

## Method

The order was fixed by your instructions and the commit history shows it was kept.

1. **Plan before screening** ([analysis-plan.md](analysis-plan.md), commit `60dc5d6`). The plan
   defines an attempt (assignment → resulting commits → acceptance evidence), states saga inclusion
   criteria, and gives each hypothesis a support pattern and a refutation pattern. The rubric is in
   [coding-rubric.md](coding-rubric.md).
2. **Inventory and amendments before coding** (commit `f2b1730`). Amendments A1–A5 are logged with
   times and reasons. A1 added `new_defect` before any coding, because screening showed symptoms
   recurring from defects introduced after a fix had held.
3. **Coding.** The lead coded S01. Four first-coder subagents coded the other derivation sagas under
   the frozen rubric. All 590 quoted fields were checked against raw records: 552 match verbatim and
   18 match as fragments of composite quotes. The remaining 20 are labelled composites, code
   excerpts or git ancestry notes; the spot checks found no fabricated quote.
4. **Blind second coder.** A Codex app agent (`gpt-6-astra`, thread `01a1c3ec`) re-coded a stratified seeded sample of
   34 attempts (36 percent) from boundary packets only, without access to codes or conclusions.
5. **Held-out prediction check.** Predictions for S02 and S07 were derived from the derivation
   codes and sealed by SHA-256 before the independent coder delimited and coded those sagas
   (commit `bac2ead`). See [held-out-predictions.json](held-out-predictions.json).

### Agreement

| Measure | Result |
| --- | --- |
| Primary code, 34 attempts | 26 agree (76.5 percent), Cohen's κ = 0.64 |
| Cause family (terminal / execution failure / mechanism shift / delivery / scope / other) | 79.4 percent, κ = 0.66 |
| `delivery_state_at_claim` | 26 of 34 |
| `tool_or_access_gap` | 27 of 34 |
| `instruction_issue` | 29 of 34 |
| `dod_explicit` | **10 of 34** |
| `workload_match` | **9 of 34** |

The two descriptors in bold are unreliable. The second coder saw more definition of done and more
matched workloads than the first coders did. Findings that rest on them are marked below.

**Disagreements and resolutions.** Eight of the 34 sampled attempts disagreed. Each is listed in
[coded-attempts.json](coded-attempts.json), with both codes and the reason for the resolution.

- **S01-A10:** the lead's own boundary error. The "recurrence" preceded this attempt's own
  acceptance, so it was recoded `narrower_scope` against the corrected next event.
- **S01-A13:** `incomplete_fix` → `indeterminate`. No later attempt is shown to rework the same
  mechanism.
- **S01-A14:** `narrower_scope` → `scope_decision`. Your words deferred "any remaining performance
  investigation", which covers the next recurrence.
- **S03-A1 and S15-A12:** `incomplete_fix` → `narrower_scope`. These are investigation-only attempts
  with correct diagnoses; amendment A4 applies.
- **S04-A4 and S05-A1:** `narrower_scope` → `delivery_failure`. The change was not running at the next
  report.
- **S13-A1:** the first code was kept, and the second coder's `verification_failure` was added as a
  flag. The rubric does not distinguish disclosing an untested environment from disclosing a residual
  mechanism.

The resolution also found two more investigation-only attempts that A4 applies to (S01-A2, S04-A6).
Both were recoded. Nine primary codes changed in total.

### Held-out prediction test

Seven predictions were derived from the derivation codes and sealed by SHA-256 before the
independent coder received the held-out sagas: S02 (rebuilding without interrupting agents) and S07
(composer focus lost after a slash command). The coder delimited both sagas from raw records and
coded 20 and 2 attempts respectively, without seeing the predictions or any derivation code. Both
sagas pass the inclusion rule (I3) under that coder. The published
[held-out-predictions.json](held-out-predictions.json) matches the committed hash; the scores are in
[held-out-scores.json](held-out-scores.json).

| ID | Prediction | Observed | Result |
| --- | --- | --- | --- |
| P1 | Wrong diagnosis + incomplete fix is less than half of non-terminal attempts | 1 of 20 | Pass |
| P2 | Narrower scope + new defect + delivery failure + scope decision is at least half | 15 of 20, all `narrower_scope` | Pass |
| P3 | At most one wrong diagnosis | 0 | Pass |
| P4 | S02 has at least one delivery failure, primary or flag | None | **Fail** |
| P5 | Explicit definition of done in at most 25 percent | 3 of 22 | Pass |
| P6 | Weaker or different acceptance workloads at least as common as matched | 4 against 13 | **Fail** |
| P7 | Verification failure primary in at most 25 percent | 2 of 20 | Pass |

Five of seven passed. The central finding (rank 1 below) and the minority role of execution failure
replicated out of sample. The two failures change the conclusions:

- **P4:** delivery failure (rank 2) is supported by the 12 derivation sagas and the baseline sample,
  but did not appear in S02. It is reported as a derivation-set finding, not a general one.
- **P6:** the independent coder judged most held-out acceptance workloads matched. Together with
  9-of-34 agreement on this descriptor, the claim that acceptance workloads were usually weaker than
  the live workload is **withdrawn**.

## Ranked causes

Counts are resolved primary codes over the 79 derivation attempts that did not end their saga.
"Any" adds attempts where the cause is a contributing flag. Ranking is by count; severity notes
follow each row.

| Rank | Cause | Primary | Any | Falsification record |
| ---: | --- | ---: | ---: | --- |
| 1 | **One label, many mechanisms.** A correct fix at narrower scope than the complaint, or a defect introduced later | 47 + 6 | 53 + 6 | **Supports:** 53 attempts; 20 attempts carry `bundled_symptom`. Survives restriction to implementation attempts (46 of 68). The second coder independently assigned `narrower_scope` to 19 of 34 (first coders 20). **Contradicts:** 7 `incomplete_fix` attempts reworked the same mechanism. **Held-out:** 15 of 20 (P2 passed). |
| 2 | **Validated fix not running at re-test.** Pending idle handover, not rebuilt, or build drift | 9 | 10 | **Supports:** 9 attempts, median 1.9 h to the next report, 8 within 24 h. Baseline: 14 of 27 fixes not running at acceptance. **Contradicts:** 16 non-terminal attempts were activated and verified at their claim, and recurred anyway (mostly rank 1). **Held-out:** none in S02 (P4 failed). |
| 3 | **Same mechanism only partly repaired** (`incomplete_fix`) | 7 | 13 | **Supports:** 7 attempts, median 5 days to recurrence. **Contradicts:** none of the 7 claimed more than its evidence. |
| 4 | **Explicit user scope or pause decisions** | 5 | 6 | **Supports:** 5 attempts, median 2 h to recurrence. These are legitimate trade-offs, not failures; the cost is that a deferred residual returns quickly. |
| 5 | **Acceptance evidence that did not test the failing condition** (`verification_failure`) | 2 | 3 (+4 `overclaim`) | **Supports:** S01-A19 (six days of rework) and S05-A2 (hook stranding recurred in at least 15 later clusters). **Contradicts:** low count; the weaker-workload claim is withdrawn after P6 failed. **Held-out:** 2 of 20 (S02-A18, S07-A1). |
| 6 | **Host and external conditions** | 0 | 11 | **Supports:** aggravated 11 attempts. **Contradicts:** never the only mechanism identified. |
| — | **Wrong diagnosis** | 0 | 0 | No attempt targeted a non-causal mechanism, under either coder. One early mechanism (WebKit event-stream recycling) was falsified inside its own attempt and dropped before acceptance. |

Indeterminate: 3 attempts.

## Hypotheses

| ID | Hypothesis | Result | Denominator |
| --- | --- | --- | --- |
| H1 | Agents repeatedly failed at the work they were given | **Refuted as the main explanation; true for a minority** | Execution failures are 9 of 79 primaries. The pre-stated support condition (failure despite explicit DoD and matched workload) is met by none of the 9. |
| H2 | Distinct defects were bundled under one symptom label | **Supported; replicated out of sample** | 53 of 79 (mechanism shift or new defect); held-out 15 of 20. S14 alone spans at least six start-up mechanisms; S11 six model-selector defects |
| H3 | Correct scoped fixes were read as whole-system completion | **Not supported as coded** | `misread_as_complete` 0 of 47 under both coders. The flag requires a quote showing the misreading, so it may be under-detected. Your "why is it not fully fixed?" (Nov 3) followed an agent's own overclaim (H4), not a scoped claim. |
| H4 | Acceptance evidence did not match the live workload | **Supported for individual cases; general claim withdrawn** | Primary in 2, flag in 1, overclaim in 4; held-out 2 of 20. Of the 47 mechanism-shift attempts, 35 had no live complaint-level measurement at acceptance. `workload_match` agreement is 9 of 34 and held-out P6 failed (4 weaker against 13 matched), so no frequency is claimed. |
| H5 | Delivery or runtime drift undid validated work | **Supported in derivation sagas; not replicated in S02** | 9 primaries; held-out P4 failed (none in S02); baseline 14 of 27 not running at claim; S01-A20 lost SQLite capabilities through interpreter selection (build 38 on Python 3.13.5 to build 41 on 3.12.7) |
| H6 | A known architectural bound defeated local fixes | **Supported for S01 only** | 5 S01 attempts name full-history copying under one lock, or one Python process (one S15 attempt names an unrelated bound). The S01 symptom persists after 20 non-terminal attempts, several of which measurably helped. No other saga shows a persistent structural limit. |
| H7 | A definition of done was missing | **Unanswered** | Explicit DoD in 12 of 94 attempts. Non-terminal share: 9 of 12 with explicit DoD versus 42 of 50 without, so there is no visible difference. Descriptor agreement is 10 of 34. Baseline requests were similarly terse and mostly held. |
| H8 | Concurrency and host load caused recurrences | **Aggravator only** | Environment never primary, flag in 11. Concurrency is recorded only from Oct 30, which is too little to test association. |
| H9 | Instructions were unclear or wrong | **Not supported** | Instruction issues in 7 of 94 attempts; none preceded a wrong diagnosis (0). |
| H10 | The model or effort was too weak | **Refuted by the elicitation test** | See below. |
| H11 | Tools or access were missing | **Not supported as a cause; supported as an instrumentation limit** | Gaps were recorded in 23 of 79 non-terminal attempts, spread across codes in proportion to the codes overall. Missing lock-holder and live-identity evidence left 3 attempts indeterminate. |

### Model and effort: the elicitation test

1. **Capability on comparable tasks.**
   - Baseline sample: `gpt-6.1-sol` high held in 10 of 12 repair requests; `gpt-6-astra` xhigh held in
     6 of the 7 that reached acceptance.
   - Inside sagas, the 9 execution failures are spread over `gpt-6-astra` xhigh (4), `gpt-6-astra`
     high (2), `gpt-6.1-sol` high, medium and max (1 each), roughly in proportion to how often each
     setting was used (37, 6, 13, 1 and 2 attempts).
   - S01-A19 and S01-A20 share model, effort and conversation. Only the acceptance workload and the
     user's follow-up changed, and the second passed matched-workload acceptance and activated.
2. **Unattended reliability is kept separate.** Executor reliability problems are real and are
   reported as such: S01-A20 stalled unattended for four days ("No repair workers or validation jobs
   were running when you asked"). Twenty-three chase messages ("status?", "how much longer") fall in
   seven attempts, four of them in S01.
3. **Harness and verification are kept separate.** In S01-A9 a validated backend fix could not
   activate, because "The overload keeps something in flight constantly" and the runtime never became
   idle. That is a harness property, not a model property.
4. **External evidence,** searched November 10, 2026. A September 2026 preprint reports that frontier
   agents with incomplete reviews gave misleading completion reports 80.4 percent of the time, with
   59–96 percent per model ([Smyth et al., arXiv 2609.20812](https://arxiv.org/abs/2609.20812)).
   Overclaiming is therefore a property of current frontier agents generally, not of a weak model
   here. This supports acceptance gates over model changes.

## Practice changes

Each change names its evidence, cost and predicted effect. C1–C7 are the bundle in the
[trial protocol](trial-protocol.md). The engineering guards (D1–D3, I1–I4) are recommendations only;
this analysis does not authorize them.

### Task instructions

- **C1 Symptom history in the brief.** List earlier attempts at the same symptom, with their
  mechanism, stated residuals and whether each fix is confirmed running.
  - *Evidence:* 53 of 79 recurrences came from another mechanism under the same label. Seven parallel
    duplicate attempts.
  - *Cost:* a few minutes per brief.
  - *Predicts:* fewer duplicate attempts, and new attempts that start from the residual instead of
    rediscovering the fixed mechanism.
- **C2 State the complaint as the acceptance check.** Use your reproduction, for example "reopen this
  conversation in under 1 s while agents stream", alongside mechanism tests.
  - *Evidence:* explicit DoD in 12 of 94 attempts; 35 of 47 mechanism-shift attempts were accepted
    without a live complaint-level measurement.
  - *Cost:* one sentence per brief.
  - *Predicts:* more residual mechanisms found by the agent before you report them.

### Acceptance and verification gates

- **C3 Re-test the complaint on the running build before claiming completion.**
  - *Evidence:* in S01-A9 the agent's own post-activation re-measurement immediately found the
    second bottleneck. Of the 47 mechanism-shift attempts, 20 recurred within 24 hours.
  - *Cost:* minutes to an hour per repair.
  - *Predicts:* fewer user-found recurrences within 24 hours of acceptance.
- **C4 Claim no more than the evidence.** Name the workload and environment the evidence covers.
  - *Evidence:* S01-A19; 4 `overclaim` flags; the external overclaiming study.
  - *Cost:* none.
  - *Predicts:* fewer "why is it not fixed?" exchanges.

### Orchestration and context

- **C6 One owner per symptom.** Route a new report of a known symptom to the existing owner, or to a
  fresh agent with that owner's handover.
  - *Evidence:* 7 parallel duplicates. In S01-A10, delegating under your timing constraint "added
    coordination overhead instead of getting the fix deployed".
  - *Cost:* the dispatcher looks up the owner.
  - *Predicts:* parallel duplicates fall to 0.
- **C7 No silent waits.** An executor that is waiting says what it is waiting for.
  - *Evidence:* the four-day unattended stall in S01-A20; 23 chase messages.
  - *Cost:* a checkpoint line.
  - *Predicts:* fewer chase messages.

### Tools, access and instrumentation

These are recommendations; none was implemented.

- **I1** Record model and effort per turn, not only the current selection.
- **I2** Record the serving payload, interpreter and SQLite version in completion checkpoints.
- **I3** Record lock holder and phase timing for requests that exceed their deadline.
- **I4** Add a conversation identifier trailer to commits made by agents.

*Evidence:* the unrecorded variables listed under coverage; 3 indeterminate attempts.

### Model and effort selection

- **No change.** The elicitation test refutes model weakness as a cause. Per-turn recording (I1) is
  needed before the question can be revisited.

### Delivery and drift guards

- **C5 Running-state truth in every completion claim** (practice; part of the trial).
  - *Evidence:* 9 delivery failures; 14 of 27 baseline fixes not running at claim.
  - *Cost:* one status check.
  - *Predicts:* no re-reports made while a fix is still pending.
- **D1** The build refuses an interpreter or SQLite version below the last validated configuration
  (S01-A20: `desktop/macos/build.py` defaults `--python` to the interpreter running the builder).
- **D2** Plugin refresh never removes a cache an active session still uses (S05: issue 46g has been
  open since August 26).
- **D3** An activation path for backend fixes that does not depend on the overloaded runtime
  becoming idle (S01-A9, A10).

## Stop doing

Entries attributed to you are marked; the rest apply to agents.

- **(You)** Sending the same complaint to several conversations at once. This happened at least four
  times (S01-A10/A11, S01-A16/A17, S04-A6/A7, and once more in S11). Each duplicate started from zero, and one
  added coordination overhead at the moment you needed a fast backend change.
- **(You)** "Fully and robustly fix it" as the only acceptance criterion for broad symptoms. In S01-A19
  the agent chose its own workload, and that workload omitted the traffic that later failed.
  Name the reproduction instead.
- **(You)** Re-reporting a symptom before confirming that the fix is active. Several reports came
  while a fix was installed but not yet serving. Ask "is it active?" first.
- **(Agents)** Claiming "fixed", or "nothing else is needed" (S01-A15), without re-testing the
  complaint on the running build.
- **(Agents)** Leaving known residuals only in prose. Several issues stayed open after their work
  was done (gq3, td0f, jli, gqz), while recurring failures were filed as new issues instead of being
  linked: four native smoke failures, X01.
- **(Agents)** Delegating to a worker when you have set a time constraint (S01-A10).
- **(Agents)** Leaving load-test copies behind: about 18 GiB in temporary directories (S01-A20, the agent's
  own count).
- **(Agents)** Building with whatever interpreter happens to run the builder.

## Unanswered questions and the smallest instrumentation to answer them

| Question | Why unanswered | Smallest instrumentation |
| --- | --- | --- |
| Does a missing definition of done cause recurrence? | DoD rarely explicit; descriptor agreement 10 of 34 | Record a structured `dod` field at assignment (the trial scorecard does this) |
| How often were acceptance workloads weaker than live? | `workload_match` agreement 9 of 34 | Completion claims name the workload in a fixed format (C4) |
| Which lock holder caused each refresh timeout? | Not recorded | I3 |
| Did model or effort vary within threads? | Only current selection saved | I1 |
| Was the fix running when each recurrence was reported? | Payload identity checked only sometimes | I2 and C5 |
| Did you verify fixes yourself? | No event | An optional "verified" action in the interface |

## Files

| File | Contents |
| --- | --- |
| [analysis-plan.md](analysis-plan.md) | Pre-registered plan, hypotheses, amendment log |
| [coding-rubric.md](coding-rubric.md) | Rubric 1.1 |
| [saga-inventory.json](saga-inventory.json) | Screening universe, retained and excluded sagas |
| [coded-attempts.json](coded-attempts.json) | Every attempt: first codes, second codes where sampled, resolution, citations |
| [baseline-sample.json](baseline-sample.json) | Seeded random sample of repair requests and their outcomes |
| [held-out-predictions.json](held-out-predictions.json) | Sealed predictions, byte-identical to the committed hash |
| [held-out-scores.json](held-out-scores.json) | Held-out scores |
| [trial-protocol.md](trial-protocol.md) | Pre-registered trial (awaiting approval) |
| [trial_scorecard.py](trial_scorecard.py) | Ledger and scoring tool for the trial |
| [collect_sources.py](collect_sources.py), [screen_sagas.py](screen_sagas.py) | Read-only extraction and screening |
