---
name: agent-coordination
description: Create and reuse persistent Ribbon Field app agents for direct user conversations and later requests. Coordinate Codex and Claude sessions through messaging, conflict detection, and scoped managed terminal delegates. Use when creating or routing work to an app agent, sending or receiving agent messages, checking file conflicts, before implementation when another session may be active, or delegating scoped work to a terminal worker. Beads is optional.
---

# Agent coordination

Use `agent-coord` from the current tool shell. It resolves the caller from
`AGENT_COORD_SESSION_ID` or `CODEX_THREAD_ID` and the database from
`AGENT_COORD_DB`. When a hook supplies an explicit command, use that command.
A fork uses its own identity. Name the recipient or management target explicitly.
A standalone terminal can install the launcher with the plugin's
`scripts/agent-coord install-cli`.

## Shared files

Inspect `agent-coord status` and `agent-coord list --relevant --cwd "$PWD"`.
A sole active writer can proceed. When another session is active, reserve each
path with a separate `--scope`. A validation lease protects a scope from edits.

```sh
agent-coord begin-work --scope 'src/parser.py' --scope 'tests/test_parser.py'
agent-coord conflicts
agent-coord end-work
```

Stop on a conflict. Coordinate with the owner, narrow the declaration, or
transfer the whole declaration with `agent-coord handoff`. Use `list --cwd "$PWD"`
to find idle recipients and `status --session-id <id>` to confirm the recipient is ready. Release it when
finished. If using Beads, claim the issue first and add `--bead <id>`.
[Scope changes, atomic handoffs, thread closure and recovery](references/recovery.md)

## Messages and decisions

Use `agent-coord send --session <session-id> --reply-required --request-id <key> 'message'`.
The receiver sees unread messages at supported turn or tool boundaries.
Use `inbox --unread` to retrieve your own messages and `inbox --peek` when
inspecting another session. Reply to requests marked `reply_required` with
`reply --message-id <id> --request-id <key> 'response'`. Reuse the key and
arguments after an uncertain receipt. Delivery, a requested reply and work
completion each have separate evidence.

Use `ask_human` for a decision, approval, resource or review that requires the
user. Give the question, concrete options when applicable and the affected
work. Resolution arrives in the owner's inbox. [Asks and resolution](references/asks.md)

## Durable work and agents

Runtime completion and attention updates are automatic. Use `update_checkpoint`
when publishing a durable decision, link, blocker or handoff. The CLI fallback
is `agent-coord checkpoint --json '<object>'`. The first checkpoint requires
`phase` and `summary`. Further updates preserve omitted fields and user-owned
titles, domains and thread placement. [Checkpoints and context for handoff](references/checkpoints.md)

Search with `thread search --app-only` before creating a persistent app agent.
Use `thread create` for a conversation the user can address again and `send`
to route work to an existing agent. Use scoped terminal delegates for bounded
background work. [Delegation and supervision](references/delegation.md)

- For recurring or future prompts, read [schedules](references/schedules.md).
- For user-requested thread organization, use [manage-threads](../manage-threads/SKILL.md).
- For ongoing specialist routing, use [dispatch](../dispatch/SKILL.md).
- To supervise a delegated outcome, use [orchestrate](../orchestrate/SKILL.md).
- For retrospective work analysis, read [activity evidence](references/activity.md).
- For interactive answers, read the [OpenUI authoring guide](references/openui.md).
