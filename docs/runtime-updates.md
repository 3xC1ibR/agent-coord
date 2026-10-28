# Updates that preserve running work

Ribbon Field updates must preserve active agent work whenever the new desktop
interface is compatible with the running runtime. This removes a barrier to
receiving fixes and continuing to use the installed app.

This document records the implementation contract and initial rollout procedure.
The runtime and updater are implemented, with focused checks, the full regression
suite, and independent signed replacement acceptance complete. Final notarized
packaging and rollout are still pending; the existing installed app still shuts
down its backend on quit.
Do not use this document as evidence that the installed version already supports
uninterrupted updates.

## Product contract

- Discover and download signed updates while work continues.
- Replacing or restarting a compatible desktop interface must leave active
  provider processes, tool executions, approvals, output, and queued work intact.
- Retain the running runtime's executable, Python dependencies, backend modules,
  and required resources across replacement of the application bundle.
- Keep runtime and database upgrades pending until a safe transition is possible.
  Do not interpret saved conversations as uninterrupted execution.
- A failure to prove compatibility or readiness defers activation. It must never
  silently fall back to killing agents or starting a competing database owner.
- Preserve an explicit way for the user to stop work and quit completely.
- Preserve macOS 12 support and both distributed architectures.

## Components and ownership

The desktop application owns windows, native integration, and the updater. A
separately running user process owns agent sessions and their provider clients.
The desktop can disconnect and reconnect without becoming that process's lifetime
owner. This requires no root daemon or automatic launch at login.

Runtime payloads are immutable and identified by version/content. A live payload
cannot be removed by app replacement or cleanup. Discovery and attachment must
verify the database identity, runtime protocol, and live instance; a PID alone
is insufficient. Runtime control must be restricted to the local user and
authenticated separately from untrusted web content.

The first release uses one active agent runtime for a database. Concurrent old
and new agent runtimes are outside this change's scope. An updated desktop may
attach to a compatible older runtime while its replacement remains staged.
Pending upgrades must not start a second scheduler, inbox dispatcher, or queue
consumer against the same live ownership domain.

This ownership boundary does not prohibit normal CLI and hook database writes.
Independently hosted terminal agents and managed job supervisors need not finish
before a runtime upgrade if their lifetime and state do not depend on that
runtime. In-flight routing, reviews, sidecars, control operations, and provider
work owned by the runtime do participate in the handover boundary.

## Compatibility and activation

The desktop/runtime handshake must state a protocol version and enough capability
information to reject unsupported combinations. Updated web assets must also be
matched to that contract: reconnecting a new native wrapper to old assets alone
does not demonstrate that a web interface update took effect.

Runtime replacement requires a coordinated admission boundary, not a single
unchecked snapshot of an empty list. User messages, queued follow-ups, schedules,
inbox wakes, and other automatic starts must not race with shutdown. Pending
approvals and executing tools count as active work. Preserve accepted pending
work durably and resume its dispatch after successful replacement. If the
candidate cannot start, retain a recoverable installation and report the failure.

Incompatible protocol or database migrations defer activation. Reversible bundle
replacement does not imply that a database downgrade is safe. Do not run
destructive migrations while an older runtime is using the same database.
This applies to candidate probes and newly bundled CLI/hooks as well as the main
server: opening a store must not bypass the compatibility boundary. If installation
rolls back to an older interface, that interface must not activate a staged newer
runtime merely because its payload is present.

The first implementation conservatively fingerprints backend Python modules.
A newly bundled CLI or hook with different backend code refuses database access
while the older runtime owns it. Running providers use their retained matching
tools; new CLI versions wait for the idle runtime upgrade. This is stricter than
the interface protocol handshake and avoids accidental schema changes.

## Runtime maintenance controls

Runtime state lives in a private directory beside the canonical database:
`.state.sqlite3.runtime` for a database named `state.sqlite3`. It contains retained
payloads, authenticated discovery, ownership and schema locks, and `runtime.log`.
Do not remove that directory while work is running, expose its control token, or
signal a PID copied from an old discovery file.

For a standalone candidate, the coordinator can start the runtime with admission
already closed, before any queue, scheduler, or inbox worker may dispatch:

```bash
candidate_resources='/absolute/path/Ribbon Field.app/Contents/Resources'
coord_database='/absolute/path/state.sqlite3'
env -u PYTHONHOME PYTHONPATH="$candidate_resources/backend" PYTHONNOUSERSITE=1 \
"$candidate_resources/runtime/bin/python3" -B "$candidate_resources/backend.py" \
  --db "$coord_database" --runtime-bootstrap --resources "$candidate_resources" \
  --runtime-start-paused
```

This attaches to an already paused owner without resuming it and refuses an
already admitting owner. It cannot pause the old app-owned backend retroactively;
the initial rollout coordinator must stop that backend before bootstrapping the
new one. Ordinary app launch then attaches while maintenance remains in force.

Use the matching retained payload's Python and helper for an existing runtime.
Before its bundle is replaced, these commands can also use the candidate's
matching resources:

```bash
env -u PYTHONHOME PYTHONPATH="$candidate_resources/backend" PYTHONNOUSERSITE=1 \
"$candidate_resources/runtime/bin/python3" -B "$candidate_resources/backend.py" \
  --db "$coord_database" --runtime-command status
env -u PYTHONHOME PYTHONPATH="$candidate_resources/backend" PYTHONNOUSERSITE=1 \
"$candidate_resources/runtime/bin/python3" -B "$candidate_resources/backend.py" \
  --db "$coord_database" --runtime-command pause
env -u PYTHONHOME PYTHONPATH="$candidate_resources/backend" PYTHONNOUSERSITE=1 \
"$candidate_resources/runtime/bin/python3" -B "$candidate_resources/backend.py" \
  --db "$coord_database" --runtime-command resume
env -u PYTHONHOME PYTHONPATH="$candidate_resources/backend" PYTHONNOUSERSITE=1 \
"$candidate_resources/runtime/bin/python3" -B "$candidate_resources/backend.py" \
  --db "$coord_database" --runtime-command stop
```

Pause closes new admissions without stopping work already accepted. Resume
reopens dispatch. Stop explicitly stops runtime-owned work and waits for confirmed
ownership release; use it only at the intended maintenance boundary. Its durable
proof binds the exact database and stopped instance and is invalidated on the
next startup. Missing or stale discovery is not proof that stopping succeeded.

For rollback, stop the candidate under maintenance before restoring the retained
prior bundle. Never downgrade across a newer database schema. The initial
handoff must supply exact paths and identities for both bundles and preserve
the saved conversations; restarting a conversation is not permission to repeat
an interrupted tool action.

## Signed update delivery

Use Sparkle's normal app update flow and native controls. Publish its appcast
alongside the existing versioned release artifacts. The CI release identity,
architectures, artifact URLs, sizes, archive signatures, and appcast versions must
agree. Missing production signing configuration must fail a release build rather
than silently produce an apparently update-enabled app.

The existing Developer ID signature and notarization remain required. Sparkle
archive signing is a separate trust mechanism. Keep signing keys out of source,
logs, update feeds, and the application. A development build may explicitly omit
automatic updates; it must not contact production with a placeholder key.

Automatic checks/downloads respect the user's updater preferences. Installation
must consult the runtime compatibility decision before allowing an interface
relaunch. An unavailable decision is a deferral, not permission to interrupt work.

### Signing configuration

The updater implementation pins Sparkle 2.10.1 and verifies the framework archive
before extracting it. `python3 desktop/macos/sparkle.py --fetch` prints the verified
distribution directory containing its tools. The production feed is separate for
each channel and architecture:

```text
https://downloads.ribbonfield.com/updates/alpha/arm64.xml
https://downloads.ribbonfield.com/updates/alpha/x86_64.xml
```

The same layout applies to `beta` and `stable`. Never offer an Apple Silicon-only
archive to an Intel installation.

Bootstrap the production Ed25519 key once using Sparkle's
`bin/generate_keys --account ribbon-field`. Retain the private key in Keychain and
the protected CI secret store; normal builds must never generate a replacement
identity. If exporting with `-x` for CI setup, use a private temporary file with
mode 0600, send its contents through standard input to the secret setter, then
remove it. Do not print the private key or save it in source or build artifacts.

| GitHub environment | Configuration | Purpose |
| --- | --- | --- |
| `macos-signing` | Secret `SPARKLE_PRIVATE_KEY` | Sign release archives and appcasts |
| `macos-signing` | Variable `SPARKLE_PUBLIC_KEY` | Embed and verify the intended identity |
| `macos-release` | Variable `SPARKLE_PUBLIC_KEY` | Independently verify artifacts before publishing |

Both public-key variables must be identical. The existing Apple signing and
notarization secrets remain separate. As of the initial implementation preflight,
the Apple configuration exists and the Sparkle configuration has not been added.

The builder accepts `--update-feed`, `--update-public-key`, `--update-channel`,
`--update-archive`, and `--update-private-key-file`. Update archives require a
standalone Developer ID signed, notarized build. A development build with updates
disabled remains available for ordinary local checks. Test feed configuration
must not create an insecure production override.

The publisher validates exact archive size before promotion. Sparkle verifies
archive signatures but treats an appcast's declared enclosure size as advisory;
size enforcement belongs to the publisher. Feed promotion is atomic per
architecture, not across both feeds and the website manifest. An interrupted
promotion may expose the new version to one architecture first. For the alpha
release, retry publication with the same verified artifacts to finish promotion;
do not rebuild or overwrite immutable release files.

## Acceptance evidence

Validation uses isolated databases, runtime directories, and fake providers before
any test on real conversations. Tests must demonstrate:

1. An interface exits and reconnects while the same fake provider and runtime
   processes continue; output produced during disconnection remains available.
2. Replacement of the source app bundle does not invalidate files still required
   by the active runtime.
3. Pending approvals and tools survive reconnect; queued work is neither lost
   nor submitted twice.
4. Concurrent launches, stale discovery, and failed candidate startup do not
   create two owners or terminate an unrelated process.
5. Runtime replacement defers when busy and excludes new admissions during the
   final idle transition.
6. Incompatible runtime/UI combinations and invalid update metadata fail safely.
7. Both standalone packaging paths retain signature/runtime integrity; ordinary
   development builds and native smoke tests still work and clean up owned tests.
8. An independent agent reviews the resulting diff and runs its own checks.

The final release gate is an installed, signed old-to-new app update with active
test work. Unit tests or a successful build alone do not prove that gate passed.

Current evidence (October 28, 2026): 12 focused lifecycle, three schema, and
14 desktop checks pass. Native smoke confirms two successive interfaces attach
to the same live runtime, followed by explicit cleanup. A standalone arm64 test
retains the same runtime after its source app is removed and verifies retained
Python, cryptography, and Web Push imports. Independent updater
review and native compilation for both macOS 12 architectures pass. The real
Sparkle harness covers discovery, signature rejection, and installation gates,
but deliberately stops before bundle replacement.

The separate independent signed replacement run passed all 38 checks. A
Developer ID signed v1-to-v2 update preserved the runtime, fake provider, pending
approval, and queued message through actual bundle replacement. The new
interface attached to the same runtime and served its new web assets while the
backend payload remained pending. After approval, the runtime upgraded at idle
and dispatched the queued message exactly once. A subsequent v3 update verified
the real Stop Work and Quit alert, stop-before-decision helper order, exact
stopped-instance proof, rejection of missing or wrong proof, and proof removal
on fresh startup. The [independent report](../build/macos/update-validation-recovery/independent_findings.md)
records the results and earlier harness defects.

This isolated acceptance used a test bundle ID, a loopback signed feed, a public
test key, and a driver built with the testing flag. Its bundles were Developer ID
signed but not notarized or stapled. The production builder does not enable those
test paths; its signed/notarized update-archive packaging is a separate staging
gate. No real agent or live database was used.

Two observed lifecycle details remain explicit: Stop Work and Quit honors the
stop before checking a staged update, so a later refusal can leave the interface
open with work already stopped and a restart message. Also, Sparkle can install
an already prepared update after an unexpected host exit without the orderly
quit callback; the independent runtime stays alive, and the new interface must
still pass attachment compatibility checks.

The final assembled Python suite passed: 1,671 tests in 821.276 seconds, with
seven optional skips (six real Sparkle tests requiring an isolated framework
cache and one Web Push encryption test requiring optional packages). All 1,018
JavaScript tests passed without skips; plugin packaging validation and Python
compilation passed. All 430 reserved source inputs matched before and after the
passing run. Test-only corrections removed a throughput assumption from the
noisy-job test and supplied the real admission gate to the provider-usage HTTP
fixture; neither required a product fallback. The dedicated Sparkle checks and
signed replacement acceptance remain separate from the general suite.

## First rollout

1. Finish implementation, focused checks, the repository suite, and independent
   review. Resolve blocking findings and stage a separate app bundle.
2. Record the candidate identity and validation evidence. Complete signing/feed
   setup before describing customer automatic updates as operational.
   The readiness handoff includes the candidate's absolute path, version,
   architecture, content hash, completed review evidence, exact installation and
   relaunch commands, rollback procedure, and unresolved limitations.
3. Assign one coordinator to the maintenance window. Refresh the active-agent
   roster and preserve continuation context before stopping affected work. Use
   a detached one-shot controller that survives the old application's exit;
   conversations hosted by that application cannot supervise their own restart.
   A launchd controller must use an explicit plist with `RunAtLoad=true`,
   `KeepAlive=false`, and a durable completion guard so it cannot repeat cutover.
   Let the old app's active agents finish, or explicitly stop them. Prevent new
   scheduled/queued work from starting during this one-time maintenance window.
   Independently hosted terminal agents do not need to stop merely because the
   desktop is being replaced. Verify the supported admission pause and restart
   mechanism before cutover; an idle snapshot alone is insufficient.
4. Quit the old app and install the staged candidate. Reopen it and verify that
   saved conversations and settings are available.
5. Verify runtime ownership and interface reconnect behavior in the installed
   version. Resume intended work; do not blindly replay uncertain tool actions.

The first transition cannot inherit the new lifetime model retroactively. Future
compatible interface updates should preserve active work; runtime changes may
still wait for that work to finish.

## References

- [macOS distribution and release workflow](../desktop/macos/README.md)
- [Sparkle setup and archive signing](https://sparkle-project.org/documentation/)
- [Sparkle installation coordination](https://sparkle-project.org/documentation/api-reference/Protocols/SPUUpdaterDelegate.html)
