# Provider usage

Open **Usage** from the workspace sidebar to see account quota windows and their
reset times. The page also works at `/usage`, including on paired remote devices.
It opens over the workspace, preserving active conversations. Conversation token
totals and context capacity remain in each thread's **Token usage** control.

The first version reads Codex, Claude, and Antigravity. Kimi and Muse explicitly
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
