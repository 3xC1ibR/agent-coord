# Multi-Agent Orchestration Playbook

This playbook calibrates **when** and **how** to orchestrate persistent, cross-provider Ribbon Field app agents (`codex`, `claude`, `antigravity`, `kimi`, `muse`), terminal delegates, and sidecars.

---

## 1. Empirical Failure Modes of Multi-Agent Systems (Why Naive Swarms Fail)

Large-scale empirical audits of multi-agent LLM systems (such as UC Berkeley's **MAST** taxonomy across 1,642 traces and ACL 2026 **Diversity Collapse** studies) show that naive multi-agent setups fail **41%–86.7%** of the time and frequently underperform a single well-harnessed agent.

Three failure modes account for almost all multi-agent breakdowns:

1. **Specification Drift & Role Theatrics (~42% of failures):** Splitting a single coherent problem across artificial personas ("Product Manager", "Architect", "Junior Coder") causes constraints and edge cases to drop at each boundary.
   - *Fix:* Decompose by **concrete write-scope boundaries**, **context-noise isolation**, or **epistemic independence (author vs. blind verifier)**—never by theatrical job titles.
2. **Inter-Agent Misalignment & Hub Relay Tax (~37% of failures):** Passing verbose conversational transcripts through a central orchestrator causes **context pollution** ("lost-in-the-middle" attention degradation) and delays local fixes.
   - *Fix:* Pass structured state (`confirmed_facts`, `constraints`, `tried_and_failed`, `open_questions`) and let authors and reviewers coordinate directly via peer messages and `agent-coord handoff`.
3. **Premature Termination & Diversity Collapse (~21% of failures):** When multiple agents debate in a shared thread before committing to an independent artifact, weaker or peer models defer sycophantically ("Artificial Hivemind"), and agents accept unverified outputs.
   - *Fix:* Enforce **isolated generation before comparison (Nominal Group Technique)** and require **blind verification** against executable tests.

---

## 2. Trajectory State vs. Artifact State (`context_lifecycle` Decision Matrix)

When deciding whether to reuse an existing app agent (`send`) or create a fresh one (`thread create --mode orchestration`), ask whether the next step depends on the worker's **accumulated trajectory** or only on its **delivered artifacts**:

| Scenario | State Dependency | Action (`reuse` vs. `fresh`) | Why |
| :--- | :--- | :--- | :--- |
| **Iterative write $\rightarrow$ test $\rightarrow$ fix loop on the same module** | **Trajectory State** | **Reuse (`send`)** | The specialist holds live file structure, compiler quirks, and negative memory (`tried_and_failed`) that prevent repeating dead ends. |
| **Follow-up bug or refinement on a recently delivered feature** | **Trajectory State** | **Reuse (`send`)** | Even if the thread was closed, an actionable `send` reopens it in Now with full design rationale intact. |
| **Recurring operational role (Release / Deployment / Migrations)** | **Trajectory State** | **Reuse (`send`)** | Long-lived domain specialists accumulate environment invariants across releases. |
| **Code review, security audit, or acceptance verification** | **Artifact State** | **Fresh (`thread create`) or Sidecar** | An agent reviewing code in the context that wrote it rationalizes its own bugs. A fresh agent (ideally a different `--client` provider) sees only the spec, diff, and test suite. |
| **Implementing a clean spec after a noisy 50-turn research/debug hunt** | **Artifact State** | **Fresh (`thread create`)** | Discard the 80% dead-end exploration noise; seed the fresh agent with the compact `context_lifecycle.handover` and exact target files. |
| **Map/Reduce across $N$ independent packages or migrations** | **Artifact State** | **Fresh parallel workers** | Prevents cross-package context bleed and runs concurrently on disjoint `--scope` paths. |

---

## 3. High-Leverage Cross-Provider Topologies

Ribbon Field allows an orchestrator to mix providers (`--client codex | claude | antigravity | kimi | muse`) within the same workflow. Use provider heterogeneity deliberately:

### Pattern A: Cross-Provider Author $\rightarrow$ Blind Verifier (Atomic Handoff)
Instead of having an implementation agent grade its own work, pair two different model families and use atomic scope handoff so they transfer the lease without orchestrator relay:

1. **Implementation Specialist (`--client codex` or `claude`):** Declares write scope (`begin-work --scope 'src/auth/**'`), implements the feature, runs unit tests, and transfers the scope directly to the verifier:
   ```bash
   agent-coord handoff \
     --to-session <verifier-session-id> \
     --patch-label 'auth-token-refresh' \
     --validation-boundary 'Unit tests in tests/test_auth.py pass' \
     --validation-responsibility 'Verify race conditions, token expiry edge cases, and integration tests' \
     --mode validation
   ```
2. **Blind Verifier (different `--client` provider):** Holds an exclusive `--mode validation` reservation (cannot edit files to mask bugs), records its initial findings independently before discussing with the author, and sends required fixes directly back to the author or reports verification completion to the orchestrator.

### Pattern B: Parallel Nominal Group Technique (NGT) for Hard Bugs or Design
When a root cause is elusive or an architectural tradeoff is high-stakes:

1. Spawn two independent app agents from different providers (`--client codex` and `--client claude` or `antigravity`) with `--mode orchestration`.
2. Give both the exact same symptom, reproduction script, and read-only investigation goal. **Do not give them each other's session IDs during discovery** so they cannot anchor on each other's first guess.
3. Wait on both with one subscription (`subscribe --session <a> --session <b> --mode all`).
4. Compare their independent root-cause proofs and `context_lifecycle` handovers, then assign implementation to the agent with the verified diagnosis.

### Pattern C: Implementation + Continuous Sidecar Acceptance Watcher
When a long-horizon implementation agent tends to stop early or miss subtle prompt constraints:

```bash
agent-coord sidecar attach --target <impl-thread-id> \
  --client <different-provider> \
  --purpose 'Verify every requirement in the user spec is implemented and covered by a non-trivial test.' \
  --mode message --max-interventions 2 --max-reviews 10 \
  --request-id <stable-key> --wait 30
```
The sidecar inspects saved turn evidence asynchronously and nudges the worker only when acceptance criteria are unmet, without the orchestrator polling.

---

## 4. Compact Assignment Contract Template

Avoid both vague one-liners and 2,000-word context dumps. Structure every `thread create` or `send` assignment around four fields:

```markdown
**Objective:** [1–2 sentences on the exact deliverable and why it is needed.]

**Context & Handover:**
- Confirmed facts: [key invariants or root cause]
- Constraints: [performance, compatibility, or dependency rules]
- Do not repeat (tried & failed): [prior dead ends, if any]
- Key files/links: [exact paths]

**Scope & Ownership:**
- Declare write scope: `src/module/**`, `tests/test_module.py`
- Out of scope: [files owned by other active specialists]

**Verification & Reporting:**
- Run: `<exact test command>`
- Peer handoff (if applicable): [peer session ID and when to message/handoff]
- Completion: Save your checkpoint with `context_lifecycle` [and reply to this message / let the initial turn subscription deliver the outcome].
```
