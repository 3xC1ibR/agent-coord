# Provider usage

Open **Usage** from the workspace sidebar to see account quota windows and their
reset times. The page also works at `/usage`, including on paired remote devices.
It opens over the workspace, preserving active conversations. Conversation token
totals and context capacity remain in each thread's **Token usage** control.

## Coordination measurements

Choose **Usage → Coordination** to compare Agent Coord tool payloads over 7, 30,
or 90 days with the preceding equal-length period. The page separates generated
arguments/commands, returned tool payloads, and the lifecycle-report portion of
checkpoint arguments. It shows daily totals, calls, observed turns, operation
breakdowns and argument-size median/p95. A turn here means a native turn with a
measured coordination call, not a completed user task or an independently judged
outcome. Changes between periods are descriptive, not causal savings estimates.

The first version indexes retained Codex `McpToolCall` records and literal,
standalone `agent-coord` CLI commands. Other providers, compound shell commands,
and inherited events from another native thread are excluded. Coverage reports
missing, malformed, oversized, and unreadable records. No prompt or tool-result
bodies are copied into the derived metrics database. Native histories and the
coordination database are read-only sources.

MCP arguments are serialized as compact, sorted JSON; CLI commands use their
captured text, and responses count text content blocks. This provides a stable
comparison measure rather than reconstructing the model's exact output format.
Payload sizes are not billed input/output tokens: programmatic calls can filter
or discard tool results, and execution-program wrappers, schemas, replayed
context, cache behavior, and hidden reasoning have separate costs. Optional
`tiktoken` measurements use a fixed
`o200k_base` encoding; UTF-8 bytes remain available without that dependency.
Unavailable token counts stay unknown. Changing the tokenizer identity rebuilds
derived measurements, so byte and token methods are never silently mixed.

Opening the page reads a bounded cached snapshot. Its initial empty state or an
explicit Refresh starts one low-priority child process, with resumable byte/time
budgets and a three-minute refresh limit. Repeated tabs share that work. The
collector sleeps between work slices, stores offsets in a separate derived
database, and never runs on provider-event or HTTP threads. GET requests do not
start workers; the dashboard polls only while collection is running and the page
is visible. A partial refresh can be continued with Refresh. Collection stops
when the backend closes. It does not create agent turns or make model requests.
On macOS, the worker also uses the system background scheduling and throttled
disk-I/O policy so interactive app work takes precedence over history reads.

`GET /api/browser/coordination-metrics?days=30` returns the cached report;
authenticated `POST /api/browser/coordination-metrics/refresh` with `{}` requests
a refresh. Both inherit the backend's workspace boundary. No request can choose
an interpreter, database, or transcript path. Derived state lives beside the
coordination database in `.<database-name>.coordination-metrics/` and can be
recreated from retained source histories.

The core runtime remains dependency-free. To enable reference-tokenizer counts
for the default local database, an administrator can prepare the optional worker
environment once:

```bash
metrics_dir="$HOME/.local/state/agent-coord/.state.sqlite3.coordination-metrics"
python3 -m venv "$metrics_dir/python"
"$metrics_dir/python/bin/python" -m pip install tiktoken==0.14.0
TIKTOKEN_CACHE_DIR="$metrics_dir/tokenizer-cache" \
  "$metrics_dir/python/bin/python" -c 'import tiktoken; tiktoken.get_encoding("o200k_base")'
```

The worker never installs packages or downloads missing tokenizer tables.
`AGENT_COORD_METRICS_PYTHON` can select an existing absolute Python executable;
`TIKTOKEN_CACHE_DIR` can select a prewarmed local vocabulary cache. Missing setup
falls back to clearly labelled byte measurements. For a custom coordination
database, use its adjacent derived directory instead of the default path above.

For regression validation, `scripts/measure_coordination_metrics.py --snapshot
<retained-database> --output <report.json> --check` runs the existing foreground
latency workload with collection disabled/enabled against a disposable native
history fixture. Use the same Python/SQLite version and frozen source for both
runs, and keep failed baseline reports. `--workload database --fixture-mib 256
--seconds 20` also compares the unchanged writer-admission gate with collection
off/on and 20 cached snapshot reads per second. This isolated result does not
replace large-history HTTP acceptance. The existing database contention gate remains required. Compare
matched task sets and checkpoint usefulness separately when evaluating an actual
token-reduction change.

## Account limit sources

Account limits are read from Codex, Claude, and Antigravity. Kimi and Muse explicitly
show unavailable account limits. A provider can omit a window or its percentage;
missing values never become zero or an unlimited allowance. A passed reset time
does not imply the account has reset until the provider reports a new reading.

Agents can query the same runtime cache with `agent-coord thread provider-limits
--client codex --wait 30` (also `claude`, `antigravity`, `kimi`, and `muse`).
The command refreshes subject to the same minimum interval and returns the
provider's state, windows, observation time, and stale flag. Use `thread models`
to discover advertised models separately: shared quota windows are not proof
of individual model availability. Unknown or stale readings and passed reset
times do not establish availability. The running app executes the request; queued
receipts can be checked with `thread request-status`.

## Collection

- **Codex:** existing app-server connection, `account/read` followed by
  `account/rateLimits/read`. Supports multiple metered buckets and uses returned
  window durations. API-key accounts do not expose subscription quotas.
- **Claude:** current Claude Code access token from `.credentials.json` under
  `CLAUDE_CONFIG_DIR` or `~/.claude`; default macOS installations use the
  `Claude Code-credentials` Keychain item. `CLAUDE_CODE_OAUTH_TOKEN` is honored,
  although some tokens cannot read usage. Reads `/api/oauth/usage`; no model
  request or token rotation. An expired sign-in must be refreshed by Claude Code.
  Like RF conversations, the collector ignores an inherited `ANTHROPIC_API_KEY`
  in favor of the Claude Code sign-in. Custom API-base and cloud provider
  configurations show unavailable subscription quotas.
- **Antigravity:** RF's existing ACP profile `acp_token.json`, an in-memory Google
  access token, and `retrieveUserQuotaSummary` with the profile's project ID.
  `loadCodeAssist` supplies the project when missing. Gemini and Claude/GPT pools
  stay separate. The collector never changes the runtime's credential file.

Claude and Antigravity reads use private provider endpoints and can change.
Responses are bounded, requests time out, redirects are refused, and raw errors
and credentials never appear in usage responses or logs. This feature does not
buy credits, redeem resets, change provider accounts, or start model turns.

## Refresh and cache

`GET /api/browser/provider-usage` reads the in-memory snapshot without network
activity. `POST /api/browser/provider-usage/refresh` with `{"provider":"codex"}`
refreshes one provider and requires the usual authenticated CSRF token. The page
refreshes every two minutes while visible. Concurrent tabs share one cache and
provider locks. Manual refresh respects the same minimum interval; errors back
off and rate-limit responses honor bounded `Retry-After` seconds.

Transient failures retain the last successful reading, visibly marked as old.
Sign-out, unsupported connections, and changed credential identities clear old
readings to avoid presenting another account's usage. Cached snapshots and
access tokens are process-local and disappear when the app backend restarts.
Agent command receipts persist their returned quota snapshot in the local request
journal, without the account identity; their timestamps describe the original
reading and must not be interpreted as a fresh query later.

## References and validation

Headroom's provider dossiers and connector shapes informed this implementation;
its TypeScript runtime is not a dependency. Reference revision:
[`7d514f6`](https://github.com/amitray007/headroom/tree/7d514f6ba44ef110e080183234b861e06fc5d86a).
The RF implementation and synthetic tests were written independently.

- [Claude research](https://github.com/amitray007/headroom/blob/7d514f6ba44ef110e080183234b861e06fc5d86a/docs/providers/claude.md)
- [Antigravity research](https://github.com/amitray007/headroom/blob/7d514f6ba44ef110e080183234b861e06fc5d86a/docs/providers/antigravity.md)
- [Codex app-server](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt)

Focused tests cover normalization, absent and malformed values, independent quota
pools, refresh coalescing, stale readings, credential changes, fixed error
messages, redirect refusal, HTTP authentication, and page navigation/countdowns.
