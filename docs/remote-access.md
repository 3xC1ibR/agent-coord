# Continue from your iPhone

Ribbon Field keeps running on your Mac. Safari connects to that same backend,
so browser conversations, running turns, approvals, and queued messages remain
on the Mac when you switch devices. Keep the Mac awake, connected, and Agent
Coord running.

## Connect

1. Install and sign in to [Tailscale](https://tailscale.com/download) on your Mac
   and iPhone, using the same private network (tailnet).
2. In Ribbon Field on the Mac, choose **Remote access** in the sidebar.
3. Choose **Enable Tailscale HTTPS**. If needed, enable HTTPS certificates and
   Serve in your Tailscale administration settings, then retry. The UI reports
   missing installation, disconnected state, permissions, and port conflicts.
4. Choose **Create pairing link**, then **Copy link**. Open the link in Safari
   on the iPhone and choose **Pair this device**. Each link works once, expires
   after five minutes, and replaces any previous unused invitation.
5. In Safari, tap **Share → Add to Home Screen → Add**. Leave **Open as Web
   App** enabled if shown. Launch **Ribbon Field** from your Home Screen to use
   it in its own window, without Safari's toolbar. The device stays paired for
   30 days. Create a new pairing link after expiry.

If the Home Screen app asks to pair again, create a fresh link under **Remote
access** on your Mac and paste it into **Pairing link** inside the installed
app. This also works after a device grant expires or is revoked. If you added
an older bookmark before Home Screen support was installed, remove that icon,
reload the site in Safari, and add it again.

The app opens the workspace address, never the one-time pairing link. It needs
a live connection to your Mac; it does not cache conversations for offline use.
The manifest and app icons are public to devices that can reach the configured
host; conversations and API requests still require device pairing.

A paired device can read conversations, send instructions, and approve actions
under the conversation's current permissions. Pair only devices you control.
Pairing links contain credentials; keep them private. No cloud relay account,
router forwarding, public port, or separate iPhone app is required.

For a command-line host:

```bash
agent-coord ui --tailscale --no-browser
# Choose a different HTTPS port if another service owns 443:
agent-coord ui --tailscale --tailscale-port 8443
```

The CLI prints the private address. Create a pairing link through the local
browser UI. `--tailscale-serve` is an alias for `--tailscale`.
Without these flags, the local UI still offers the Remote access controls.

## Background phone notifications

Open the installed Home Screen app, then tap **Enable phone notifications** and
allow notifications in the iOS prompt. Use **Send test notification** to check
delivery. iOS 16.4 or later is required; a regular Safari tab cannot enable
iPhone Web Push. Notifications cover finished turns, failed turns, and pending
approval requests. They show generic text, without conversation titles, paths,
prompts, or response content. Tap an alert to open its conversation.

The Mac must be awake, online, and running Ribbon Field to send new alerts.
Apple delivers accepted pushes even while the phone app is closed. Receiving
an alert uses the phone's internet connection; opening its conversation also
needs Tailscale and access to your Mac. Focus and notification settings can
silence or delay alerts.

Tap **Phone notifications on** to turn them off for that device. Revoking the
device or letting its pairing expire stops future sends; notifications already
accepted by the push provider cannot be recalled. Re-pair and enable again
after expiration. Each subscribed phone gets its own delivery, independent of
desktop notification claims.

The Mac app bundles pinned Web Push dependencies. For a CLI-hosted backend,
install them in that Python environment with
`python3 -m pip install -r desktop/macos/push-requirements.txt`, then restart the
server. All other CLI functionality remains dependency-free. Subscription
credentials, a persistent VAPID signing key, and the delivery queue are stored
privately beside the database in `.push.sqlite3`. Short-lived alerts retry
temporary failures and remove expired push subscriptions. Outgoing requests
use only recognized Apple, Chrome, and Firefox push providers; no public
incoming endpoint or developer account is required.

## Manage paired devices

**Remote access** on the Mac lists paired devices. **Revoke** removes that
device's access, including its event stream. **Disable remote access** locks
remote requests immediately, revokes every device and unused invitation, and
removes only the Tailscale route owned by this server. It does not stop local
conversations or reset other Tailscale services. If the route has been changed
externally, Ribbon Field leaves it untouched and reports this.

Remote access preferences and hashed device grants are stored alongside the
coordination database in a private `.remote.json` file. Normal shutdown removes
the owned route but retains the preference and grants; restarting restores
the route to the new local port. Only one backend per coordination database can
own remote access. Other UI servers report that it is managed elsewhere.
After a crash, the next owner may replace only the exact saved proxy mapping.

Closed Codex terminal conversations can be reopened in the browser. Active
terminal conversations remain with their terminal client; Claude Code threads
continue in Claude Code. Unsaved drafts belong to the browser window where
you typed them.

## Security and implementation

The Python backend still binds only to loopback. Tailscale Serve terminates
HTTPS and provides the private route. Ribbon Field never enables Funnel, never
uses `serve reset`, and refuses to take over an occupied port or Funnel route.
Administration and creation of pairing invitations are available only through
the local UI. Remote access requires an exact configured HTTPS host/origin and
an independently authorized browser, including for reads and event streams.
CSRF checks remain enabled. Forwarded headers never grant local privileges.

Pairing secrets travel in the URL fragment, are removed from browser history
on page load, and are exchanged once through a same-origin POST. Device
credentials use Secure, HttpOnly, SameSite=Strict cookies. Only credential
hashes are persisted. HTTP request logging does not record pairing secrets.
This feature assumes the Mac's local processes and user account are trusted.

## Implementation reference

The design was informed by [T3 Code](https://github.com/pingdotgg/t3code) at
commit `0678e4e23d8675ef88f9ac08e1ae90cfe7d6ef2e`, specifically:

- [Remote access guide](https://github.com/pingdotgg/t3code/blob/0678e4e23d8675ef88f9ac08e1ae90cfe7d6ef2e/docs/user/remote-access.md): separate network reachability, one-time pairing, and revocable device access.
- [Tailscale integration](https://github.com/pingdotgg/t3code/blob/0678e4e23d8675ef88f9ac08e1ae90cfe7d6ef2e/packages/tailscale/src/tailscale.ts): CLI status discovery, bounded command execution, MagicDNS HTTPS URLs, Serve enable/disable, and sanitized errors.
- [Desktop endpoint provider](https://github.com/pingdotgg/t3code/blob/0678e4e23d8675ef88f9ac08e1ae90cfe7d6ef2e/apps/desktop/src/backend/tailscaleEndpointProvider.ts): first-class private-network connection status.

This is an independent dependency-free Python implementation. It does not copy
T3 Code's relay infrastructure, multi-machine routing, or native mobile app.
Tailscale command behavior follows the [official Serve reference](https://tailscale.com/docs/reference/tailscale-cli/serve).
