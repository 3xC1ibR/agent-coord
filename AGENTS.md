# Agent instructions

## Naming

Ribbon Field is the public-facing app and marketing name. Agent Coord is the
historical name of the coordination tooling. Preserve existing internal names,
CLI/plugin/skill identifiers, bundle ID, deep links, and saved-data paths.

## Project workflow

Agent Coord is a Python CLI packaged as one dual-client Codex
and Claude plugin. Keep the two client manifests thin. Put shared behavior in
`plugins/agent-coord/scripts/agent_coord/` and shared agent guidance in
`plugins/agent-coord/skills/agent-coordination/`.

Before implementation, inspect concurrent sessions and declare the smallest
useful file scope with `agent-coord begin-work` when another session is active.
Do not edit when the command reports a conflict. Release the declaration with
`agent-coord end-work` when the session no longer owns the scope.

Use work-thread checkpoints for durable links, decisions and handoffs. Beads is optional.
When selected, run `bd prime`, claim the issue with `bd update <id> --claim` before declaring
`--bead`, and close completed issues with `bd close`. Use `bd remember` for project memory.
Dolt sync uses `bd dolt pull/push` and `refs/dolt/data`; the JSONL export is passive.

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
to make a change pass. Compare matched workloads before and after optimization;
on signed upgrade rehearsals, compare candidate latency against the unmodified
baseline captured in the same run—if the baseline itself breaches its budgets,
diagnose run noise before rejecting a candidate for a modest latency miss while
keeping correctness, continuity, and exact-once delivery non-negotiable.
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
before live testing. Use the documented development links for local edits or the
versioned installer for releases. Verify fresh clients; preserve pinned sessions.
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

Use non-interactive options for unattended file operations, SSH and package installation.

## Interface changes

Read DESIGN.md and follow the interface-design skill for contracts, tokens and screenshots.
Never use UI eyebrow labels.
