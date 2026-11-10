# Pre-registered trial: assignment and acceptance practice for repairs

Status: **proposed; awaiting user approval.** No live workflow changes until the user approves this
protocol. Registration opens at approval; the protocol text is frozen at the commit that records
approval, and later changes go to the amendment log with time and reason.

## Question

Do the practice changes in [README § Practice changes](README.md#practice-changes) raise the share of
repair assignments that are accepted once and stay fixed?

## Eligible assignments

An assignment is eligible when all of the following hold.

1. A human asks, in a Ribbon Field conversation rooted in `/opt/projects/agent-coord`, for an
   investigation or repair of a user-visible defect: an error, broken behaviour, slowness or a wrong
   state. Features, design discussion, experiments, routine deploys and commits are not eligible.
   This is the eligibility rule used for the historical baseline.
2. It is registered with `trial_scorecard.py register` before the executor's first acceptance claim.
3. It is the first assignment for that report. A follow-up in the same conversation after an
   acceptance claim is a recurrence of the registered assignment, not a new one.

Each eligible assignment is classified at registration, from the record and before work starts:

- **Recurring (stratum R):** the same symptom was reported within the previous 30 days and an
  earlier attempt had claimed completion. The registration names the earlier saga or thread.
- **New (stratum N):** otherwise.

The trial ends at 10 registered stratum-R assignments or 30 days after approval, whichever comes
first. All stratum-N assignments registered in that period are also scored.

## Changes applied to every registered assignment

The bundle is applied in full. The trial does not separate the effects of its parts.

| ID | Change | What the executor or dispatcher does |
| --- | --- | --- |
| C1 | Symptom history in the brief | Before work starts, the brief lists earlier attempts at the same symptom: their mechanism, stated residuals, and whether each fix is confirmed running. `agent-coord thread search` and Beads are the sources. |
| C2 | Complaint-level check | The brief states the user's reproduction as the acceptance check, for example "reopen this conversation in under 1 s while agents stream". Mechanism tests are still required but are not sufficient on their own. |
| C3 | Re-test on the running build | Before any completion claim, the executor re-runs the C2 check against the build that is actually serving and reports what remains. |
| C4 | Claim no more than the evidence | The completion message names the workload and environment the evidence covers. "Fixed" means C3 passed. Anything else is stated as "fixed for X; not yet verified for Y". |
| C5 | Running-state truth | A completion claim states the serving payload identity and whether the change is active. When activation is pending, the claim says so and names what activation needs. The scorecard records `--running`. |
| C6 | One owner per symptom | A new report of a registered symptom is routed to the existing owner, or to a fresh agent with that owner's handover. The same complaint is not sent to several conversations. |
| C7 | No silent waits | An executor whose work is waiting (on idle handover, a reviewer, the user) says so in its checkpoint, with the condition, rather than going quiet. |

## Measures

**Primary:** an assignment *succeeds* when it has exactly one acceptance claim and no same-symptom
recurrence, further attempt or reopen within 14 days after that claim. Recurrences are found with
`trial_scorecard.py scan` and confirmed by a judge (below) before `recur` is recorded. Assignments
still inside their window at analysis are `censored` and reported separately.

**Secondary (process fidelity and cost):**

1. Claims made while the change was not active, without saying so. Target: 0.
2. Acceptance claims that include a C3 re-test result. Target: at least 9 in 10.
3. User chase messages ("status?", "how much longer") per assignment. Historical saga attempts: see
   the README.
4. Hours from assignment to acceptance claim. This guards against the bundle making repairs slower.
5. Parallel duplicate conversations per symptom. Target: 0.

## Decision rules (pre-registered)

- **Stratum R.** Historical rate: 4–13 percent of continuation attempts ended their saga within 14
  days. The trial uses a conservative benchmark of 25 percent. **Supported** if at least 5 of 10
  decided stratum-R assignments succeed (one-sided exact binomial p = 0.078 at 25 percent; 0.006 at
  13 percent). **Not supported** if 2 or fewer succeed. Between those values, the result is inconclusive.
- **Stratum N.** Historical rate: 22 of 27 (81 percent) in the baseline sample. Ten assignments cannot
  demonstrate an improvement over 81 percent. Stratum N is used only to detect harm: **harm** if 6
  or fewer of 10 decided assignments succeed (P = 0.10 at 81 percent).
- Any secondary-measure target missed in more than 2 assignments is reported as a fidelity failure.
  A primary result produced under a fidelity failure is reported, but does not count as support.

## Scoring procedure

1. **Registration:** `register --id Tnn --thread <thread> --stratum R|N --symptom "<user words>"
   --dod "<C2 check>" --saga <Snn or earlier thread> --changes C1,...,C7`. `--stratum` is required.
2. **Acceptance:** `accept --id Tnn --claim "<verbatim>" --evidence <types> --running yes|no|unknown
   --payload <hash>` at the first completion claim.
3. **Recurrence check:** at day 14 after acceptance, run `scan --id Tnn --terms "<symptom words>"`.
   A judge who did not execute the assignment (a fresh agent, given this protocol and the scan output)
   decides whether each candidate is the same symptom, and records `recur` or nothing. The judge also
   reads Beads reopens and later threads that mention the symptom.
4. **Analysis:** `score --stratum R --baseline-rate 0.25` and `score --stratum N --baseline-rate 0.81`.
   The same judge procedure applies to every assignment. Disagreements with the executor's view are
   reported, not overwritten.

## Threats to validity

- **Single arm with a historical comparator:** changes in the product (for example a backend that
  finally stops timing out) could raise success independently of the bundle. The analysis reports
  which sagas the assignments belong to.
- **Hawthorne effect:** registration itself may change agent behaviour. That is acceptable: the goal
  is the practice, not the effect of an unannounced intervention.
- **Selection:** the stratum-R benchmark comes from sagas selected for repetition, which biases it
  down. The 25 percent benchmark is set well above the observed 4–13 percent for that reason.
- **Judge error:** the judge rubric is the baseline coder's rule ("same symptom"). The baseline coder
  flagged 2 of 27 broad-match judgements; the trial reports those cases both ways.

## Amendment log

| Time (UTC) | Change | Reason |
| --- | --- | --- |
| — | — | — |
