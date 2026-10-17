---
name: orchestrate
description: Own a complete user-requested outcome by coordinating persistent Ribbon Field app agents. Use when the user asks you to manage a task, delegate work as needed, and deliver the integrated result in this conversation.
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
to specialists for direct follow-up instead.

## Establish the outcome

Identify the requested deliverable, constraints, and what counts
as complete. Ask only for information or decisions that materially
affect the work and cannot reasonably be inferred.

Break the objective into useful assignments. Parallelize independent
work and make dependencies explicit. Use the smallest useful team;
respect any requested agent count or requirement for fresh agents.

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

Give each assignment its objective, necessary context, expected
result, constraints, dependencies, and a clear reporting destination.
Give collaborating agents each other's identities and establish who sends
the next handoff, what it contains, and what completes their exchange.

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
validation, and unresolved limitations. Orchestration-created agents also save
the [context lifecycle report](../agent-coordination/SKILL.md#context-lifecycle-for-orchestration-agents)
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
