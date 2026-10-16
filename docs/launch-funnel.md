# Ribbon Field launch funnel

This experiment measures whether macOS visitors try the beta, reach a coordination
milestone, and return. Its output supports launch learning; the quoted conversion
targets are hypotheses, not sourced benchmarks or automatic kill/pivot rules.
Inspect counts, acquisition hooks, feedback, and sample size together.

Published on 2026-10-16 UTC: the website and tracked redirects serve the signed,
notarized 0.1.1 [Apple Silicon installer](https://downloads.ribbonfield.com/releases/0.1.1/Ribbon-Field-0.1.1-arm64.dmg)
and [Intel installer](https://downloads.ribbonfield.com/releases/0.1.1/Ribbon-Field-0.1.1-x86_64.dmg).
Hosted bytes matched the local SHA-256 manifests. Both website hostnames were
byte-verified after CloudFront invalidation. This Mac's application resolver still
cached a negative DNS answer; release HEAD checks used public DNS and file downloads
used the authoritative address with normal hostname/certificate verification.
The signed artifacts and evidence are under `build/macos/launch-funnel-0.1.1/final/`.

## Event contract

| Event | Meaning | Counting rule |
| --- | --- | --- |
| `lp_view` | Production landing-page view from a desktop Macintosh UA; excludes mobile and obvious bots | Unique browser ID per hook in the report window; IDs expire after 30 days |
| `dmg_download_started` | Download endpoint accepted a valid request and issued a redirect to a configured release DMG | Unique download token; **not** completed bytes or a successful installation |
| `app_first_launch` | A standalone build's backend ran from `/Applications` or `~/Applications` | Persistent random install ID; excludes dev builds, mounted volumes, and relocated smoke builds |
| `first_coordinated_action` | Within 24 hours of first launch, two live Codex/Claude sessions were registered, a write scope succeeded, or a checkpoint was saved | Once per install, with `criterion` = `two_sessions`, `write_scope`, or `checkpoint` |
| `d3_active_session` | Another qualifying action occurred in `[72h, 120h)` after first launch, on a later UTC calendar date than activation | Once per activated install |
| `install_attributed` | The user opened the download page's optional app link | First token wins; report joins earlier milestones by install ID |

The activation definition follows the proposed experiment. It is a **setup/use
proxy**: hooks and agents can register sessions or save checkpoints automatically.
Reports preserve the first criterion so that this proxy can be evaluated against
observed useful work. None of these events proves that a user accepted a result.
Passive backend heartbeats do not produce return events. The 24-hour and 72–120-hour
clocks start at observed first launch, since a DMG's copy/install time is unknown.

Running an installed binary is observable. Calling this `app_gatekeeper_passed`
would imply a security assessment we do not perform. Gatekeeper can be bypassed;
apps can also legitimately launch outside Applications. This definition measures
the supported installation path, not every possible launch.

## Attribution

The website accepts `?hook_id=demo-01` or `?utm_content=demo-01` (hook_id wins).
Campaign values must be short slugs; do not put personal information into them.
The current valid hook is retained with the browser ID; absent attribution uses
`direct`. A UUID download token is minted on each click and logged by the download
redirect alongside the browser ID, hook and architecture.

After downloading, the page offers:

```text
agentcoord://attribution?download_token=<uuid>
```

The user drags the app to Applications and clicks **Open Ribbon Field**. The native
handler validates the UUID, then submits it to the local backend with its CSRF
token. The next background flush links the install to its download. No account
is required. Normal app launches still work and remain explicitly unattributed.

Renaming a DMG does not pass its name into the copied app. Apple documents
[quarantine origin URLs](https://developer.apple.com/documentation/coreservices/klsquarantineoriginurlkey)
and [quarantine properties](https://developer.apple.com/documentation/foundation/urlresourcekey/quarantinepropertieskey),
but these do not establish an end-to-end token handoff through every browser,
DMG mount and Finder copy. We do not rely on that propagation, scan Downloads,
or read the clipboard. Apple also documents
[pasteboard permission prompts](https://developer.apple.com/documentation/appkit/nspasteboard/accessbehavior-swift.enum/default).

One optional click provides deterministic attribution when used, with incomplete
coverage. Shared download links, blockers and opt-outs prevent interpreting the
attributed launch rate as the true overall installation rate. Unmatched installs
are reported separately; no IP fingerprinting or probabilistic matching is used.

## Collection and controls

Shared implementation: `plugins/agent-coord/scripts/agent_coord/launch_metrics.py`.
Only an installed standalone native build initializes telemetry. CLI/plugin-only
users never start sending telemetry. Existing databases with sessions are marked
`existing_install` and excluded from new-install funnel conversion.
Native smoke runs explicitly disable the sender, including when testing a bundle
inside Applications. The focused handoff check uses only a disposable local DB:

```sh
python3 desktop/macos/smoke_test.py --app '/path/to/Ribbon Field.app' --attribution-only
```

Anonymous milestone sharing defaults on. **Settings → Anonymous usage measurement**
can disable it, stop future capture, and clear the local queue. Already delivered
events are not deleted by this control. Re-enabling does not replay missed actions.
The website has an independent funnel toggle and honors DNT/GPC for this funnel;
its existing Umami traffic tracker remains separate and is disclosed alongside it.

Payloads contain event UUIDs, install/browser/download UUIDs, milestone timestamps,
the existing-install flag, and a fixed criterion or short hook/architecture value.
They never contain prompts, source code, filenames, paths, session IDs, titles,
checkpoint text, credentials, or transcript contents. The collector rejects extra
fields. HTTPS infrastructure sees network addresses during transport, but neither
the event table nor configured API access logs store IPs or full user agents.

Coordination paths only write a tiny local SQLite outbox. The native backend sends
it on a daemon thread, with a four-second request timeout and a sixty-second retry
interval. Stable event IDs deduplicate retries on the server. Unsent records expire
after seven days. Remote reports ignore records after ninety days; DynamoDB TTL
physically removes them asynchronously, as described in
[AWS's TTL documentation](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/ttl-expired-items.html).

## Collector and reporting

Website/service source is the neighboring repository `/opt/projects/agent-coord-web`:

- `dist/funnel.js`: page events, tokens, optional app link, privacy control.
- `api/billing/launch_metrics.py`: public bounded ingestion and download redirect.
- `infra-api/launch-metrics.tf`: separate expiring event table and scoped write IAM.
- `api/billing/funnel_report.py`: pure cohort report logic.
- `scripts/launch-funnel-report`: read-only operator report with normal AWS credentials.

No billing/customer records or new analytics vendor are required. Ingestion accepts
only allowed events and fields, bounded timestamps, UUIDs, and allowed website
origins. Existing API Gateway throttling bounds request volume. Native events have
no client secret: these are unverified public reports, suitable for directional
learning rather than billing or fraud-resistant counts.

Run in the website repository:

```sh
uv run --project api python scripts/launch-funnel-report --days 30
```

The report deduplicates visitors per hook and downloads per token, joins installs
only to measured visits/downloads, and allows one install per download token.
Visit→download uses unique downloading visitors, not repeated clicks. The download
launch rate includes only download attempts at least 24 hours old. Activation
includes only launches at least 24 hours old; return includes only activated
launches at least 120 hours old. Every ratio includes numerator and denominator;
an empty denominator produces `null`, never a misleading zero-percent verdict.
Older/unattributed installs and incomplete windows cannot silently become failures.

## Release wiring

The collector was deployed and verified over HTTPS on 2026-10-16 UTC, including
idempotent retries, persisted event fields, and website CORS. Test events were
removed. Signed/notarized 0.1.1 builds and publication evidence are recorded under
`build/macos/launch-funnel-0.1.1/`. Old app builds cannot emit these milestones.

1. Publish signed/notarized, release-tested DMGs for both architectures.
2. In the website repo, set Terraform `launch_downloads` and `dist/config.js`
   `downloads` to the **same** two versioned URLs. Keep `funnelDownloads: false`
   until the download endpoint has been deployed and smoke-tested.
3. Run the website/API tests, `scripts/build-api`, then review/apply the API
   Terraform plan using `scripts/terraform-api`. This provisions the event table,
   IAM policy, routes and Lambda environment/code. Billing remains independently
   configured.
4. Test `/v1/download` against both real DMGs, including its direct-link fallback.
   Enable `funnelDownloads`, run `npm run check:release`, and deploy the website
   through its existing guarded release workflow.
5. Build and distribute a fresh standalone app. On a separate clean Mac, exercise
   visit → download → drag to Applications → optional Open link → coordination.
   Verify opt-out stops delivery and inspect the resulting report. Real retention
   evidence must wait for elapsed time; tests use a controlled clock.

No production publication or replacement of the running local app is implied by
source validation. Release checks remain in `desktop/macos/README.md`.

The broad native smoke run exposed stale unscoped view fixtures; these were
updated for current folder and group navigation. Broad runs also encountered
focus/timing failures in Send work and pane Back, so no all-green broad native
claim is made. The focused attribution check and files/settings checks passed.
Relocated runtime/CLI checks passed for both architectures; Intel execution here
uses Rosetta, not physical Intel hardware. A separate clean-Mac trial remains
valuable evidence beyond this machine's checks.
