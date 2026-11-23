# Agent Instructions
@AGENTS.md





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

From an agent shell, prefix this command with
`env -u AGENT_COORD_SESSION_ID -u CODEX_THREAD_ID` so fixture callers do not
inherit the executing agent's identity.

## Production performance, reliability, and operational excellence

Own the outcome I experience. Turn each production failure into a permanent release test. Show that the affected build fails it. Upgrade the exact signed candidate on a representative copy of my workload while agents stream large outputs and a client continuously opens threads and sends messages. Fail the release if the app becomes unreachable beyond its stated allowance, misses its latency budgets, or loses or duplicates accepted work. After activation, verify those same outcomes on the backend actually serving my Mac before calling it fixed.

Keep the private incident record at `/opt/projects/agent-coord/docs/rf-performance-reliability-and-operational-excellence.md` up to date in the primary checkout, including when working in another worktree. Read it when investigating a production failure or preparing a release that affects a previously reported interaction. Add a dated section for each new incident of slowness, conversation refresh failure, or other user-visible reliability failure as soon as it is reported or observed. Preserve each repeated user complaint with its timestamp and source, even when it describes an open incident.

Record the interaction, user impact, workload, affected build, evidence, cause or outstanding question, permanent regression test and owner. Append the failing baseline, signed-candidate measurements, release decision and installed-backend verification as they become available. Use the interaction budgets in [Responsiveness](docs/responsiveness.md) and the upgrade allowances in the [runtime update contract](docs/runtime-updates.md). Keep incidents open when verification is pending. Keep the incident record outside Git.

## Database correctness and latency

For database, persistence, search, provider-event or request-path changes, run
`python3 scripts/measure_database_contention.py --check --output build/database-performance.json`.
CI enforces this gate. Report errors, throughput, foreground p95, writer hold p95
and admission timeouts with the workload and revision. Never raise budgets merely
to make a change pass. Compare matched workloads before and after optimization.
The integration harness and HTTP budgets are documented in `docs/responsiveness.md`;
use large histories and sustained mixed traffic when changing storage/indexing.

All coordination writes use `CoordinationStore._connection()`; finite write
scopes are serialized across store instances. Use `store.reading()` for reads,
and keep parsing, hashing, provider/network calls and waits outside write scopes.
Independent nested connections are read-only; pass the existing connection when
an operation must extend a transaction. Name important operations with
`store.database_operation(name, priority="background"|"foreground")`.
Runtime status and authenticated `GET /api/browser/performance` expose bounded,
content-free latency metrics. Use them to identify queue delay versus scope time.
Preserve receipt idempotency, revision checks and restart safety while optimizing.

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
  appear immediately. Backend changes can wait for idle or use **Apply Backend
  Update…** on runtimes advertising `runtime_updates: 1`. That operation owns
  interruption, rollback and guarded continuation; do not manually stop agents
  to force activation. Check the committed payload and recovery outcomes before
  claiming that the backend update is active and work has resumed.
- Local development builds select a unique Developer ID Application certificate
  when available to preserve macOS privacy approvals; ad hoc installation requires
  explicit `--sign-identity -` and cannot replace a certificate-signed app. Local
  builds disable automatic updates. Use the existing release CI for notarized,
  update-enabled builds; its Apple credentials and updater signing identity are already
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
