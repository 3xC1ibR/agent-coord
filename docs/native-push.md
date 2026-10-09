# Native iPhone notifications

Ribbon Field's iOS app uses Apple Push Notification service (APNs) for finished/failed turns, approval requests, and expired snoozes. The owning Mac discovers events and sends visible alerts directly to Apple, even when the iOS app is suspended or closed. The Mac must be running Ribbon Field and have internet access. Opening an alert requires the iPhone to reach its paired Mac, normally through Tailscale.

This implementation supports an operator-controlled Mac with an Apple signing key. It does not provision a hosted push relay or distribute developer signing keys to customers. A public distribution that sends notifications from customer-owned Macs needs a separately designed credential/relay arrangement; never bundle a provider key in either app.

## Apple and Mac setup

Use a Mac backend built from the version containing these native-push routes. Refreshing the CLI plugins does not replace an already-running server or a previously packaged desktop app; rebuild/install that app or run the updated source backend before testing.

1. In the Apple Developer account used to sign the iOS app, register the explicit App ID `com.agentcoord.ios` and enable **Push Notifications**. Create/download an APNs authentication key (`.p8`) authorized for that topic and intended environment. Record the 10-character key ID and team ID.
2. Keep the key outside this repository in a private directory on the Mac running Ribbon Field. Set its permissions to `600`; do not paste the key into chat, source files, defaults, or the iOS app.
3. Create a private JSON configuration beside the coordination database, named `<database-path>.apns.json`. For example, a database at `/path/state.sqlite3` uses `/path/state.sqlite3.apns.json`. Alternatively set `AGENT_COORD_APNS_CONFIG` to the configuration's absolute path **in the server process environment**. A shell variable does not automatically reach a Finder-launched app.

   ```json
   {
     "team_id": "YOURTEAMID",
     "key_id": "YOURKEYID1",
     "key_path": "/absolute/private/path/AuthKey_YOURKEYID1.p8",
     "topic": "com.agentcoord.ios"
   }
   ```

   Replace the example IDs and path. Both IDs must be 10 uppercase letters/digits. Run `chmod 600` on both the configuration and key. Only these four configuration fields are accepted. Never check either file into git.

4. Restart the Mac's Ribbon Field backend after adding or changing configuration. It requires the existing optional desktop `cryptography` dependency and `/usr/bin/curl` with HTTP/2 support (`/usr/bin/curl --disable --version`). The CLI remains usable without them; native push reports setup unavailable. Requests use system TLS verification and bypass proxy/curlrc settings.
5. Open `mobile/ios/RibbonField.xcodeproj`, select the signing team in the app target, and obtain a provisioning profile with Push Notifications enabled. The checked-in entitlement requests development APNs; distribution signing/export must supply the production entitlement. The app reads the embedded profile's actual entitlement, rather than guessing from Debug/Release. App Store installs without an embedded profile use production.
6. Build/install the signed app on an iPhone. Pair it to the Mac with a fresh Remote access link. In **Connection → Notifications**, tap **Enable notifications**, accept the iOS permission prompt, and tap **Send test notification**. Test again with the phone locked. A queued test means the Mac accepted the request; it does not prove Apple delivered the alert.

Physical-device validation requires the actual key, matching signing/profile, and reachable Mac. An unsigned build, mocked transport, or simulator-injected notification cannot validate this chain. Apple may delay or suppress presentation because of user settings, Focus, or connectivity.

## Behavior and privacy

- Device tokens are obtained from Apple on each opted-in launch and retained only in app memory. The app saves the paired origin and notification preference, not the token. Rotated tokens replace the old Mac registration. Sandbox and production are separate registrations and fixed Apple endpoints.
- Registration, status, unsubscribe, and test routes require the paired device's existing Secure/HttpOnly cookie. Mutations also require exact Origin and a fresh backend CSRF token. Tokens are never accepted for an arbitrary client-supplied device ID. Native URLSession requests use ephemeral storage and reject redirects.
- The Mac stores registrations and the durable delivery outbox in a private `<database-path>.apns.sqlite3`, separate from browser Web Push. Apple signing keys remain in their original private files and are never stored in that database. Bearer/device tokens pass to curl through stdin, never command-line arguments or application logs.
- Alerts contain project/repository and thread titles plus the event type. Apple receives this metadata; prompts, responses, approval commands, pairing secrets, and device cookies are excluded. iOS preview settings control lock-screen visibility.
- Delivery reuses the existing completion/approval/snooze eligibility rules, receipts, bounded retries, and expiry. Initial enrollment does not replay historical completed turns. Pending approvals can alert. Invalid/unregistered Apple tokens are removed; temporary failures retry. Revoking a paired device, disabling Remote access, or changing its origin stops further sends. An alert already accepted by Apple cannot be recalled by the Mac.
- **Turn off notifications** stops registration locally and removes the Mac enrollment. Failed removal is retried when the app reconnects. Disconnect attempts removal before clearing cookies; if the Mac was unreachable, revoke that device on the Mac to remove the old grant. Re-pairing normally removes the prior enrollment first. If that removal failed, revoke the old grant before enabling the same token under a new grant.
- Alert taps accept only `/` or a bounded thread fragment from the currently paired, opted-in origin. External URLs and pairing/command routes are rejected. Navigation waits for the authenticated page to be ready and respects its existing explicit reload/recovery flow.
- Safari/Home Screen Web Push remains separate. Its installation controls are hidden inside WKWebView; native settings live in the Connection sheet. No silent background execution mode is requested: iOS presents visible APNs alerts without keeping the web view alive.

## Validation

Backend tests cover authorization, token ownership/rotation, revocation, outbox restart/deduplication/retry, Apple endpoint selection, signed JWT verification and expiry, token-safe subprocess transport, and failure handling. Swift tests cover same-origin notification destinations, provisioning environment selection, fresh-CSRF enrollment, cookie isolation, and redirect/authentication rejection. The HTTPS UI smoke test covers pairing, relaunch, draft preservation, unavailable-configuration feedback, and disconnect.

References: Apple's [token-based APNs connections](https://developer.apple.com/documentation/usernotifications/establishing-a-token-based-connection-to-apns), [notification requests](https://developer.apple.com/documentation/usernotifications/sending-notification-requests-to-apns), [device registration](https://developer.apple.com/documentation/usernotifications/registering-your-app-with-apns), and [APNs entitlement](https://developer.apple.com/documentation/BundleResources/Entitlements/aps-environment).
