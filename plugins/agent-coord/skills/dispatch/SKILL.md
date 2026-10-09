---
name: dispatch
description: Route ongoing user requests to persistent Ribbon Field app agents for answers and direct follow-ups in their own conversations. Use when the user asks this conversation to act as a dispatcher, or when continuing that established routing workflow.
---

# Dispatch to app agents

Be the user's ongoing point of entry for work. Route requests to
app agents that retain relevant context and can communicate with
the user directly.

Use the [coordination skill](../agent-coordination/SKILL.md) for commands,
delivery semantics, permissions, and file coordination. App agents provide
the persistent conversations used by this workflow. Use the
[orchestrate skill](../orchestrate/SKILL.md) when the user wants you to own
and deliver the integrated result here instead. Provider-native
subagents and managed terminal delegates are separate mechanisms.

## Route with minimal overhead

- Reuse a known suitable app agent directly.
- When the recipient is unclear, search open and closed app threads
  and inspect a small shortlist. Match actual work and artifacts,
  not just titles.
- Create an app agent when no existing conversation has useful
  context, or when the user explicitly requests a fresh agent.
- Let responsibilities emerge from prior work. A specialist is a
  role, not a permanent restriction on what an agent can handle.
- Closing a thread does not disqualify it from reuse. Ordinary
  actionable messages automatically reopen closed app conversations.
- Answer questions about this dispatch workflow here when
  the context is already available. Route domain questions and
  execution to the relevant app agent.

Do not repeat broad inventory, settings, or readiness checks before
every handoff. Let the assigned agent inspect current source and
validate assumptions inherited from earlier work.

## Send a concise assignment

Include the user's request, necessary context, relevant artifacts,
and constraints. Preserve the distinction between a question,
a proposal, implementation, and deployment.

Avoid repeating standing instructions the recipient already has.
Configure model, reasoning, and permissions through the tools;
omit settings-confirmation boilerplate from the assignment.

Reuse known settings unless the task warrants a change. Choose
supported models and reasoning appropriate to the work. Carry
forward established user authorization and preferences; this
skill does not itself grant additional permissions.

Use actionable messages for assignments. Default to
`--no-reply-required` when the dispatcher does not need a response.
This removes the reply obligation, not the request to do the work.

Have the app agent answer the user directly in its own conversation
and accept follow-ups there. Give the user a link to that conversation.
Do not require routine answers to pass back through the dispatcher.

## Coordinate ownership and completion

Let the assigned agent own its investigation, implementation,
task tracking, validation, and result. Involve the dispatcher
when routing, dependencies, conflicting scopes, or material blockers
need coordination. Reuse the same conversation for later discoveries,
questions, and revisions; completing an assignment does not end collaboration.

Keep receipts and outcomes distinct: accepted, running, completed,
and deployed are different states. Investigate delivery problems
when evidence warrants it; avoid routine polling and duplicate
status requests.

When deployment is authorized, reuse a dedicated release app agent.
Provide the release scope and known constraints; let it establish
readiness, coordinate the deployment, and verify the running result.

Use the orchestrating checkpoint phase for this conversation's
ongoing routing work, including while specialists implement or
deploy. Identify the actual next actor without making routine
agent progress require user action.

## Optional workflow findings

Workflow findings collection is disabled by default. Starting
dispatch does not enable it.

Enable it only when the user explicitly requests it, for example:
“Track workflow findings so we can improve this process.”

Keep that choice for the current dispatcher conversation unless
the user specifies another scope or changes their preference.
Do not repeatedly ask whether to enable it.

When disabled:

- Omit findings instructions from agent handoffs.
- Do not create findings files or maintain an experiment journal.
- Continue normal task tracking, checkpoints, and result reporting.

When enabled:

- Give each app agent its own findings file and reuse it across
  assignments.
- Record meaningful observations during normal work.
- Consolidate findings when reviewing results or when asked.
- Do not start agent turns solely for bookkeeping.

If the user disables collection, stop assigning findings work
and preserve existing notes.
