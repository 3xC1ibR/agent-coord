---
name: manage-threads
description: Review, organize, and navigate Agent Coord threads across workspaces. Use when the user asks to manage open threads, see what needs attention, choose what to resume, park or close, open or link to a project, repository, saved view or thread in the app, or dedicate a session to thread management.
---

# Thread Manager

Help the user review and organize their Agent Coord threads from any working
directory. Use the installed plugin's CLI and shared database; the source
repository and Beads are not required. Use `agent-coord` on PATH; normal
commands inherit the database through `AGENT_COORD_DB` or the default state
location. The session-start hook supplies an absolute executable/database
fallback when needed. See the [coordination skill](../agent-coordination/SKILL.md)
for launcher setup and caller identity resolution.

Use a known session ID directly for a specific action. For an explicit request
to close your own thread, save its checkpoint and use `thread close --session-id
<your-session-id> --after-turn`. Report the returned status accurately: queued
means pending, not closed. The shared workflow covers cancellation and failures.

Read the [thread-management workflow](../agent-coordination/references/thread-management.md)
for global discovery, interpreting saved progress, organization requests, and
opening filtered views in the macOS app. Keep this role focused on managing threads unless the user also asks
you to take on their underlying work.
