# Multi-Agent Orchestration Playbook

This playbook calibrates **when** and **how** to orchestrate persistent, cross-provider Ribbon Field app agents (`codex`, `claude`, `antigravity`, `kimi`, `muse`), terminal delegates, and sidecars.

---

## 1. Empirical Scaling Limits & Failure Modes (2025–2026 Evidence)

Controlled evaluations across frontier models converge on one core rule: **parallelize reading and verification; serialize coupled writes.**

- **Capability saturation & sequential degradation (Google DeepMind / MIT, *Nature Machine Intelligence* 2026):** Across 260 configurations (including SWE-bench Verified and Terminal-Bench), centralized multi-agent setups gained up to **+80.8%** on truly parallelizable tasks, but **degraded sequential, stateful tasks by −39% to −70%** across every multi-agent topology. Once a single-agent baseline exceeds ~45% accuracy, adding multi-agent coordination yields diminishing or negative returns, with tool-heavy coding loops paying 263–285% coordination overhead.
- **Conway's Law / spec-heading slicing (Anthropic Applied AI, Oct 2026; Cognition, Apr 2026):** Assigning one persistent writer per plan section or module in a shared runtime fragments implicit architectural decisions ("actions carry implicit decisions") and forces lossy summary-of-summary handoffs. Folding coupled writes back into a single main loop with progressive skills consistently beats domain-sliced writer swarms.
- **Overloaded hybrids & lateral merge thrash (Cursor, 2026):** Combining planning, coding, worker supervision, merging, and judging inside one long-running context causes drift and premature completion. Likewise, parallel writers resolving their own merge conflicts overwrite or abandon work; parallel writes only scale when subsystems are genuinely decoupled, shared contracts live on disk, and integration/verification is isolated.
- **Multi-coordinator & shared-resource collapse (Anthropic *Worse Together*, Nov 2026; UC Berkeley MAST):** Independent coordinators racing against the same repository or merge queue underperform a single sequenced coordinator; uncoordinated independent agents amplify errors **17.2×** vs. **4.4×** under centralized verification.
- **Homogeneous clone parity (Stanford, Apr 2026; Anthropic Red Team, Aug 2026):** Under matched thinking-token budgets, splitting reasoning across identical clones does not outperform a single strong model and introduces correlated conformity errors. Multi-agent gains come from **context hygiene**, **parallel read-only wall-clock**, or **provider heterogeneity** (cross-family blind verification).

### Topology Selection Matrix

| Workload Shape | Recommended Topology | Avoid |
| :--- | :--- | :--- |
| **Coupled feature, refactor, or runtime migration** | **Single Builder** (writes code + local tests) + **Fresh Cross-Provider Blind Verifier** | Mapping plan headings 1-to-1 to parallel writer worktrees |
| **Noisy exploration, log triage, broad codebase/security audit** | **Main Loop + Parallel Read-Only Subagents** returning compact findings | Dumping 40 files/logs into the implementation context |
| **Hard, ambiguous bug or high-stakes architecture choice** | **Cross-Provider NGT** (2 independent read-only investigators → compare → 1 builder) | Shared-thread group debate before independent artifacts |
| **Truly decoupled workstreams** (separate repos, independent services, bulk migrations) | **Pure Coordinator + Isolated Workers** (contracts on disk) + **Blind Verifier** | Hybrid agent coding while supervising 5+ writers; lateral merge fights |
| **Sequential rollout stages or shared-repo foundation rewrite** | **Sequence upstream contracts first** (or combine coupled stages into one candidate) | Parallel downstream swarms racing against a moving foundation |

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
