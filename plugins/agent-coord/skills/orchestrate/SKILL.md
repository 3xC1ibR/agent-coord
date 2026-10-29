---
name: orchestrate
description: Own a complete user-requested outcome by coordinating persistent Ribbon Field app agents across providers. Use when the user asks you to manage a task, delegate work as needed, and deliver the integrated result in this conversation, or when a complex task benefits from context isolation, parallel disjoint scopes, cross-provider ensembles, or independent verification.
---

# Orchestrate

You own the user's complete objective. Plan the work, coordinate
app agents, review their contributions, and deliver the finished
result in this conversation.

The user can inspect or speak to any participating agent, but
should not need to manage those agents or collect their results.

Use the [coordination skill](../agent-coordination/SKILL.md) for
app-agent creation, reuse, messaging, permissions, and file
coordination. Use [dispatch](../dispatch/SKILL.md) when the user wants requests routed
to specialists for direct follow-up instead. Read
[references/orchestration-playbook.md](references/orchestration-playbook.md)
for empirical multi-agent failure modes, cross-provider ensemble topologies,
and handoff templates.

## Why and when to orchestrate

Models have two failure-prone defaults when working on complex tasks:
1. **The Hero Soloist (under-delegation):** Doing noisy exploration, multi-module implementation, and self-review inside one context window—accumulating context pollution and grading their own work with the same blind spots that wrote it.
2. **Theatrical Roleplay (bad delegation):** Splitting small, tightly coupled tasks across chatty personas ("PM", "Architect", "QA") that relay messages through a central bottleneck. Empirical studies of multi-agent systems (such as MAST and ACL Diversity Collapse analyses) show that unconstrained roleplay and premature group debate fail 41–86% of the time due to specification drift, inter-agent misalignment, and sycophantic consensus collapse.

Ribbon Field app agents are **not** ephemeral provider-native subagents. They are persistent, independently addressable conversations that can run across different provider families (`--client codex`, `claude`, `antigravity`, `kimi`, `muse`), retain long-lived domain memory, transfer write/validation leases atomically (`agent-coord handoff`), and report structured `context_lifecycle` state.

Reach for orchestration when it provides genuine structural leverage:

| High-Leverage Orchestration Trigger | Why Multi-Agent Wins Here |
| :--- | :--- |
| **Context Isolation (High-Noise $\rightarrow$ Compact Output)** | A specialist reads 40 files, logs, or failing traces and returns a 15-line root-cause proof or localized fix, keeping your orchestrator context clean. |
| **Disjoint Write Scopes (True Parallelism)** | Independent subsystems (`--scope 'backend/**'` vs. `--scope 'desktop/**'`) have a clear interface contract and zero write-lease contention. |
| **Epistemic Independence (Blind Verification)** | A fresh reviewer (ideally a different `--client` provider) checks the spec, diff, and test suite without seeing the author's chain-of-thought rationalizations. |
| **Parallel Hypothesis Search (Nominal Group Technique)** | For hard, ambiguous bugs or architectural tradeoffs, two independent agents (e.g., `codex` and `claude`) investigate in isolated contexts before you compare their evidence. |
| **Persistent Domain Ownership** | Recurring subsystems (release/deployment, UI, migrations) reuse a long-lived app specialist that remembers prior constraints and `tried_and_failed` approaches. |

## Establish the outcome

Identify the requested deliverable, constraints, and what counts
as complete. Ask only for information or decisions that materially
affect the work and cannot reasonably be inferred.

Break the objective into useful assignments. Parallelize independent
work and make dependencies explicit. Use the smallest useful team;
respect any requested agent count or requirement for fresh agents.
Do not split tightly coupled edits to the same file across multiple workers.

Start useful assignments as soon as their objectives, boundaries,
and completion criteria are clear. Delegate further discovery when
needed; do not finish the investigation yourself before involving workers.

Delegate substantial research, implementation, and detailed verification.
Focus your own effort on decomposition, shared decisions, dependencies,
resolving disagreements, evaluating evidence, and delivering the combined result.

Perform bounded execution when it directly enables a decision or is clearly
cheaper than handing it off. Avoid lengthy work that delays coordination or
duplicates an active assignment. Retain responsibility for checking that the
combined result meets the user's objective.

## Choose mechanisms and providers deliberately

Match each unit of work to the right Agent Coord mechanism and provider:

- **Persistent App Agent (`thread create --mode orchestration` / `send`):** Default for substantial research, subsystem implementation, cross-provider ensembles, independent code/security review, and release ownership. Supports `--client codex | claude | antigravity | kimi | muse`.
- **Managed Terminal Delegate (`agent-coord delegate`):** Best for a single bounded, scoped terminal task (`--lease-mode write` or `--lease-mode validation`) where the worker should report once and shut down.
- **Sidecar Watcher (`agent-coord sidecar attach`):** Best when you want a dedicated observer (`--mode notify` or bounded `--mode message`) to audit whether a target app agent actually meets acceptance criteria with evidence.
- **Provider-Native Subagent:** Use only for quick, read-only context offloading inside a single turn when persistent identity, user visibility, and cross-provider diversity are unnecessary.

When authorized or appropriate, exploit **cross-provider diversity**:
- **Cross-family verification:** Pair an implementation specialist on one provider (e.g., `codex`) with an independent reviewer or sidecar on another (`claude` or `antigravity`) so correlated model blind spots do not pass subtle regressions.
- **Isolated NGT exploration:** When a bug or design is non-obvious, ask two different providers to diagnose or prototype independently. Never let them read each other's raw reasoning before they commit to an initial finding—early cross-talk causes sycophantic convergence.

## Assign work and keep the collaboration open

Reuse app agents with relevant context unless the user requests
fresh agents or a separate conversation would be more appropriate.
Use known identities directly; search only when needed.

When creating an app agent, always pass `thread create --mode orchestration`
to record that it was created to contribute to this coordinated outcome.
This is creation intent, independent of the request's reply requirement.
Reuse existing conversations with `send`; reuse does not change their creation mode.

Choose context for the next assignment by asking whether it depends on the
worker's accumulated trajectory or only on its delivered artifacts. Use a
matching `context_lifecycle` report from the worker's result when available:

- Prefer reuse for the same implementation's write/test/fix loop, dialogue-based
  refinements, and questions about earlier evidence, constraints, or failed approaches.
- Prefer fresh context for synthesis from completed verified research and for
  independent review when the handover and artifacts contain the needed state.
- Use separate workers for independent items in a map/reduce workload when useful.
- Treat a domain, toolset, or phase change as a reason to assess relevance, not
  an automatic reset. Repeated failures can make retained negative memory valuable.

Match `reuse_for`, `fresh_for`, and the recommendation's concrete reason to the
actual next task; the report is advice, not a permanent worker classification.
`reuse`, `fresh`, and `either` express the worker's recommendation. Missing,
assignment-mismatched, or outdated advice means unknown. Do not wake a worker
solely to ask whether to reuse it, or invent token/noise thresholds from its
self-assessment. A fresh conversation receives the verified handover, relevant
artifacts, constraints, failed approaches, and open questions. Copying the full
conversation does not provide fresh context. Creating a replacement does not
close the previous conversation or prevent useful peer coordination.

Every assignment handoff should be a **compact contract** (not a transcript dump):
1. **Objective & context:** What to achieve and why, plus verified `handover` facts (`confirmed_facts`, `constraints`, `tried_and_failed`).
2. **Explicit boundaries:** Owned write scope (`--scope` paths) and what must not be changed.
3. **Verification gate:** Exact commands, tests, or acceptance criteria the worker must run before reporting completion.
4. **Peer & reporting contract:** Collaborating agent IDs, who owns the next handoff (including `agent-coord handoff --to-session <peer> --mode validation` when transferring a lease), and where to report.

An assignment is the current plan, not a permanent boundary.
Share new evidence or changed requirements when they affect an
agent's work, including after it has returned a result. Reuse the
same conversation for questions, revisions, and further contributions
when its retained context is useful.

Make direct peer coordination the default for local questions, findings,
and corrections. Do not make yourself a relay or require approval for routine
handoffs within the agreed scope.

Authors and reviewers exchange findings, corrections, and recheck requests
directly. For independent reviews, reviewers record their initial findings
before discussing them with authors. Reviewers recheck corrections and report
the outcome; receive completion summaries and escalations that need your judgment.

Agents may surface discoveries and challenge assumptions. Keep the orchestrator
informed of decisions or dependencies that affect other branches or the overall
result, without recurring check-ins or unnecessary approval steps.

An agent that delegates owns combining and checking its branch's results.
Receive the branch outcome, supporting artifact references, and unresolved
issues; do not reconstruct every descendant's activity.

Keep handoffs concise. Reuse established settings and permissions;
change them only when the assignment warrants it.

Make ownership of shared artifacts explicit. Avoid assigning
concurrent edits to the same artifact. Have agents refresh current
source assumptions before relying on retained context.

## Keep results and decisions flowing

App-agent creation automatically subscribes you to completion of the created
agent's initial assignment turn. Its completion event carries the saved
checkpoint and a matching context lifecycle envelope when available. Use
`thread create --mode orchestration` without adding a result-reply obligation
solely to collect that outcome. A turn ending is evidence to review, not proof
that the assignment or overall objective is complete.
Use `--no-subscribe` only when that automatic completion delivery is unwanted;
the creation receipt confirms `result.subscribe` and `result.subscription_id`.

For later messages that need an answer, use `send --reply-required` and have
the recipient answer the original message with
`agent-coord reply --message-id <id>`. Explicit requested replies also carry a
matching saved lifecycle snapshot. Completion events need no conversational
reply; acknowledge handled events silently. Preserve any explicit reply
contract already chosen for an assignment.

Apply the chosen reporting contract to initial creation assignments
as well as later messages. Follow-up communication can start a new
request when needed; a previous completion does not end collaboration.

Ask agents to return useful results, artifact references, relevant
validation, and unresolved limitations. All app agents also save
the [context lifecycle report](../agent-coordination/SKILL.md#context-lifecycle-for-app-agents)
in their normal result checkpoint. Intermediate findings are worth sharing
when they change another agent's work. For an explicit reply-required request,
a final response in the agent's own conversation does not replace its requested reply.

Specialists should resolve local questions directly with relevant peers and
escalate unresolved blockers, disagreements, scope changes, and decisions
affecting other branches to you. Resolve them within the user's instructions;
bring genuine user decisions back to this conversation. Material changes from
direct user follow-ups should also reach you so integration stays current.

When direction changes, identify what changed and which earlier
assumptions or outputs need revision. Account for late results
against the current plan.

Avoid acknowledgement chatter and repetitive progress messages.
Use `--no-reply-required` for updates that need no conversational answer;
it does not remove any requested action.
For constraints that should apply when an agent next works, use
`--classification deferred --no-reply-required`. This delivers context without
waking an idle agent; `--no-reply-required` alone still permits wake.
Notify only agents whose work must change, and omit reminders to agents already
waiting under an equivalent constraint. Bound temporary holds by affected work
and an absolute expiry, and request reports only for actual conflicts.

## Follow through

Keep a compact durable record using the workspace's existing task
tracking and checkpoints: objective, assignments, agent/message
identities, artifacts, dependencies, and outstanding work.

Distinguish message acceptance, execution, completed assignments,
and completion of the overall objective.

Use supported reply delivery and wake mechanisms when waiting.
When no useful coordination or evaluation remains, save state and wait for
completion events. Do not manufacture work, duplicate assignments, or repeatedly
poll to stay busy. Do not require the user to prompt you again just to collect
results or resume coordination.

Review returned work against the requested outcome. Check evidence
to the depth the task warrants, reconcile inconsistent assumptions,
and request targeted revisions where needed.

If an agent fails or stalls, inspect the cause and recover, reassign,
or report the actual blocker. Do not silently omit unfinished work
or blindly duplicate an assignment that may still be running.

Keep the user informed of meaningful progress and decisions here.
Links to specialist conversations provide visibility, not a
requirement for the user to supervise them.

## Deliver the complete result

Integrate contributions into one coherent deliverable. Verify the
assembled result, including behavior or assumptions that individual
assignments could not validate independently.

When deployment is part of the authorized objective, coordinate
with the release agent and obtain verification of the running result.

Deliver the outcome in this conversation with relevant artifacts,
validation, and material limitations. Sending assignments or receiving
individual reports does not by itself complete the user's task.

Use the orchestrating checkpoint phase while coordinating work.
Keep the actual remaining work and its owner clear. Mark finished
only when the requested outcome has been delivered.

## Optional workflow findings

Workflow findings collection is disabled by default. Starting
orchestration does not enable it.

Enable it only when the user explicitly requests it. Preserve
that choice for the current conversation unless the user specifies
another scope or changes their preference. Do not repeatedly ask.

When disabled, omit findings instructions from handoffs and do not
create findings files or maintain an experiment journal. Normal
task records, checkpoints, and specialist result messages continue.

When enabled, give each agent its own findings file and reuse it
across assignments. Record meaningful observations during normal
work and consolidate them during reviews or when asked.

Do not start agent turns solely for findings bookkeeping. If the
user disables collection, stop assigning findings work and preserve
existing notes.
