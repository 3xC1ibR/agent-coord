# Agent Instructions
@AGENTS.md

## Purpose
Historically, agent-coord started as a skill to allow agents to communicate with each other (hence the name).
Later, we added `delegation`; giving agents the ability to spin up agents (different than the provider default)"
Most recently (oct 2026) we added a ui; basically an ide.

## Product focus

**Launch mode.** Prioritize getting the useful product into other people's hands
and learning from adoption, repeat use, feedback, and payment. Favor changes that
remove concrete barriers to discovery, installation, first value, or continued
use. Challenge additional features and polish without a clear connection to that
learning.

Before starting or expanding work, identify its concrete contribution to the
project's current objective.

When that connection is weak, push back before implementation: explain the
tradeoff and recommend a smaller change, a more direct experiment, or deferral.
Do not manufacture a launch justification for unrelated work.

Respect an explicit user decision to proceed and existing authorization. Raise
the concern once; do not create repeated confirmation loops.

Prefer the smallest useful change and observable evidence. Feature count,
architectural completeness, and speculative future needs are not sufficient
reasons to expand scope. Necessary reliability, security, and maintenance work
count when their connection to the objective is clear.

## Naming

Ribbon Field is the public-facing app and marketing name. Agent Coord is the
historical name of the coordination tooling. Preserve existing internal names,
CLI/plugin/skill identifiers, bundle ID, deep links, and saved-data paths.

## Project workflow

Agent Coord is a dependency-free Python CLI packaged as one dual-client Codex
and Claude plugin. Keep the two client manifests thin. Put shared behavior in
`plugins/agent-coord/scripts/agent_coord/` and shared agent guidance in
`plugins/agent-coord/skills/agent-coordination/`.

Before implementation, inspect concurrent sessions and declare the smallest
useful file scope with `agent-coord begin-work` when another session is active.
Do not edit when the command reports a conflict. Release the declaration with
`agent-coord end-work` when the session no longer owns the scope.

Beads is optional for direct work, delegation, and handoff. Use Agent Coord
work-thread checkpoints by default. When the user selects Beads or supplies an
issue, follow its workflow and claim the issue before declaring it with `--bead`.
Do not create a Beads issue solely to unlock work, and do not block work without
an issue on an absent or uninitialized Beads installation. The Beads guidance
below applies only when that integration is selected.

Run the test suite with:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

After changing a file under `plugins/agent-coord/`, follow [**Refresh installed
plugins after changes**](docs/reference.md#refresh-installed-plugins-after-changes)
before live testing. Use the documented cachebuster helper and reinstall commands. Do not hand-edit marketplace files
or reuse a session that loaded the previous plugin version.
Commit your changes once you are done.

## Ribbon Field rebuilds and rollouts

Before building or installing Ribbon Field, read
[Build and install locally](desktop/macos/README.md#build-and-install-locally)
and the [runtime update contract](docs/runtime-updates.md).

- For an installation with the independent runtime, ordinary **Command-Q**
  closes the interface and keeps agents running. Use that for a compatible
  replacement; **Stop Work and Quit** stops the work. Do not repeat the legacy
  one-time stop-all rollout for an already migrated installation.
- Build and test before replacing the installed app. Inspect the installed and
  running runtime versions; explicitly choose a suitable newer version/build.
  The builder defaults to `0.1.0`/`1`, which is not an upgrade of newer installs.
- Reopen the interface after installation. Compatible interface/web changes can
  appear immediately; backend changes wait for a safe idle runtime handover.
  Check runtime status before claiming that backend changes are active. Do not
  stop agents just to force a pending backend update through.
- Ordinary local development builds are ad hoc signed and disable automatic
  updates. Use the existing release CI for signed, notarized, update-enabled
  builds; its Apple credentials and updater signing identity are already
  configured. Do not require local notarization setup or regenerate signing keys
  for that CI workflow.
- Local installation, successful CI, and public publication are separate
  outcomes. Customer update notifications require published signed feeds and
  archives. Preserve existing authorization and report which outcome occurred.
- App installation does not replace the plugin refresh procedure above.

This project supports **bd** (beads) as optional issue tracking. When using it, run `bd prime` for full workflow context.

> **Architecture in one line:** Issues live in a local Dolt database
> (`.beads/dolt/`); cross-machine sync uses `bd dolt push/pull` (a
> git-compatible protocol), stored under `refs/dolt/data` on your git
> remote — separate from `refs/heads/*` where your code lives.
> `.beads/issues.jsonl` is a passive export, not the wire protocol.
>
> See [SYNC_CONCEPTS.md](https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md)
> for the one-screen overview and anti-patterns (don't treat JSONL as the
> source of truth; don't `bd import` during normal operation; don't
> reach for third-party Dolt hosting before trying the default).

## Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work atomically
bd close <id>         # Complete work
bd dolt push          # Push beads data to remote
```

## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations to avoid hanging on confirmation prompts.

Shell commands like `cp`, `mv`, and `rm` may be aliased to include `-i` (interactive) mode on some systems, causing the agent to hang indefinitely waiting for y/n input.

**Use these forms instead:**
```bash
# Force overwrite without prompting
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file

# For recursive operations
rm -rf directory            # NOT: rm -r directory
cp -rf source dest          # NOT: cp -r source dest
```

**Other commands that may prompt:**
- `scp` - use `-o BatchMode=yes` for non-interactive
- `ssh` - use `-o BatchMode=yes` to fail instead of prompting
- `apt-get` - use `-y` flag
- `brew` - use `HOMEBREW_NO_AUTO_UPDATE=1` env var

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project supports **bd (beads)** as optional issue tracking. When using it, run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- When Beads is selected, use `bd` for issue tracking. Otherwise use Agent Coord work-thread checkpoints; do not create markdown TODO lists.
- When using Beads, run `bd prime` for detailed command reference and session close protocol.
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Commit completed changes** - Follow the repository's project workflow.
5. **Hand off** - Summarize changes, validation, and issue status.

Beads provides issue tracking. Follow the repository and user instructions for
Git commits, pushes, and synchronization.
<!-- END BEADS INTEGRATION -->

<!-- BEGIN BEADS CODEX SETUP: generated by bd setup codex -->
## Beads Issue Tracker

Use Beads (`bd`) for durable issue tracking when the user selects it or supplies an issue. Use the `beads` skill at `.agents/skills/beads/SKILL.md` (project install) or `~/.agents/skills/beads/SKILL.md` (global install) for Beads workflow guidance, then use the `bd` CLI for issue operations.

### Quick Reference

```bash
bd ready                # Find available work
bd show <id>            # View issue details
bd update <id> --claim  # Claim work
bd close <id>           # Complete work
bd prime                # Refresh Beads context
```

### Rules

- When Beads is selected, use `bd` for issue tracking; otherwise use Agent Coord work-thread checkpoints.
- When using Beads, run `bd prime` when its context is missing or stale. Codex 0.129.0+ can load Beads context automatically through native hooks; use `/hooks` to inspect or toggle them.
- Keep persistent project memory in Beads via `bd remember`; do not create ad hoc memory files.

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.
<!-- END BEADS CODEX SETUP -->

# UI
Before any UI change, read DESIGN.md and follow the interface-design skill (contract, tokens, screenshots).
NEVER use ui eyebrow labels
