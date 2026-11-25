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

Run the full Python test suite in parallel with:

```bash
python3 -W error::ResourceWarning scripts/run_tests.py --report build/test-report.json
```

During iteration, run focused `python3 -W error::ResourceWarning -m unittest tests/test_<area>.py -v` targets first.
From an agent shell, prefix test commands with
`env -u AGENT_COORD_SESSION_ID -u CODEX_THREAD_ID` so fixture callers do not
inherit the executing agent's identity.

## Verification and release tiers

Scale verification to the blast radius of the change. Do not run a full-release
pipeline for ordinary bugfixes or local development installs.

- **Tier 1 — Normal changes, bugfixes, UI/CLI edits, and local Mac installs (default):**
  Run focused tests for the changed area, plus the parallel Python suite
  (`scripts/run_tests.py`) and JS tests (`node --test tests/*.js`) when relevant.
  For local testing on this Mac, build and install a local `arm64` Developer ID
  app directly. Do **not** trigger GitHub notarization, Intel (`x86_64`) builds,
  full-size database upgrade rehearsals, historical signed-binary proofs, or
  incident-record updates unless explicitly requested.
- **Tier 2 — Storage, schema, search indexing, or runtime-update edge changes:**
  In addition to Tier 1, run `python3 scripts/measure_database_contention.py --check --output build/database-performance.json`
  and focused schema/runtime-update tests. Prefer **deterministic work-count
  regressions** (SQLite read/write admissions, decoded turns, HTTP fetches per
  action) over noisy wall-clock millisecond assertions. Local installs still use
  a local `arm64` build unless a signed release artifact is explicitly requested.
- **Tier 3 — Public releases (`publish: true`) and explicitly scoped production-incident closures:**
  Own the user-experienced outcome end to end: turn the production failure into a
  permanent regression test, validate the signed candidate on a representative
  workload (failing on unreachability beyond allowance, lost/duplicated work, or
  unexplained latency regressions), and verify the installed serving backend.
  Keep the private incident record at
  `/opt/projects/agent-coord/docs/rf-performance-reliability-and-operational-excellence.md`
  (outside Git) updated **only** for explicitly tracked production incidents and
  public-release closures—not for routine bugfixes or normal development. Use the
  budgets in [Responsiveness](docs/responsiveness.md) and allowances in the
  [runtime update contract](docs/runtime-updates.md).

**Anti-loop rule:** Fixing an unrelated flaky test or harness check does **not**
invalidate an already-built app binary or require re-signing or re-rehearsing it.

## Database correctness and latency

For database, persistence, search, provider-event or request-path changes, run
`python3 scripts/measure_database_contention.py --check --output build/database-performance.json`.
CI enforces this gate. Report errors, throughput, foreground p95, writer hold p95
and admission timeouts with the workload and revision. Never raise budgets merely
to make a change pass. In hosted CI, packaged Playwright wall-clock latency
measurements are advisory (shared runners are noisy), while correctness,
continuity, and exact-once delivery remain non-negotiable. On signed upgrade
rehearsals, compare candidate latency against an unmodified baseline on the same
host before rejecting for modest latency noise.
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
  builds disable automatic updates. Use the existing release CI (`release-macos.yml`)
  for notarized, update-enabled builds; its Apple credentials and updater signing
  identity are already configured. Private candidate runs (`publish: false`) build
  `arm64` only and support `skip_tests: true` when CI already passed on that commit;
  public releases (`publish: true` or `v*` tags) build and notarize both `arm64`
  and `x86_64`. Do not require local notarization setup or regenerate signing keys
  for that CI workflow.
- Local installation, successful CI, and public publication are separate
  outcomes. Customer update notifications require published signed feeds and
  archives. Preserve existing authorization and report which outcome occurred.
- App installation does not replace the plugin refresh procedure above.

Use non-interactive options for unattended file operations, SSH and package installation.

## Interface changes

Read DESIGN.md and follow the interface-design skill for contracts, tokens and screenshots.
Never use UI eyebrow labels.
