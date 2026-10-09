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

Do useful work yourself when that moves the objective forward.
Delegation is a means of completing the task, not a requirement
to distribute every step.

## Assign work and keep the collaboration open

Reuse app agents with relevant context unless the user requests
fresh agents or a separate conversation would be more appropriate.
Use known identities directly; search only when needed.

Give each assignment its objective, necessary context, expected
result, constraints, dependencies, and a clear reporting destination.

An assignment is the current plan, not a permanent boundary.
Share new evidence or changed requirements when they affect an
agent's work, including after it has returned a result. Reuse the
same conversation for questions, revisions, and further contributions
when its retained context is useful.

App agents can also surface discoveries, challenge assumptions,
ask questions, and coordinate directly with relevant peers.
Keep the orchestrator informed of decisions or dependencies that
affect the overall result. Communicate when it advances the work;
do not impose recurring check-ins or unnecessary approval steps.

Keep handoffs concise. Reuse established settings and permissions;
change them only when the assignment warrants it.

Make ownership of shared artifacts explicit. Avoid assigning
concurrent edits to the same artifact. Have agents refresh current
source assumptions before relying on retained context.

## Keep results and decisions flowing

Use the coordination request/reply mechanism when you need an
answer or result. Request a reply and have the recipient answer
the original message using `agent-coord reply --message-id <id>`.
Use `thread create --reply-required` for a new agent
and `send --reply-required` for an existing one.

Apply the chosen reporting contract to initial creation assignments
as well as later messages. Follow-up communication can start a new
request when needed; a previous completion does not end collaboration.

Ask agents to return useful results, artifact references, relevant
validation, and unresolved limitations. Intermediate findings are
worth sharing when they change another agent's work. A final response
in an agent's own conversation does not automatically deliver it to you.

Specialists should raise blockers and questions with you. Resolve
them within the user's instructions; bring genuine user decisions
back to this conversation. Material changes from direct user
follow-ups should also reach you so integration stays current.

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
Continue independent work where possible. Do not require the user
to prompt you again just to collect results or resume coordination.

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
