# Ribbon Field for iOS

A SwiftUI app containing the existing Mac-served Ribbon Field mobile UI in a persistent WKWebView. Requires iOS 16.4 or later; there is no embedded backend or new cloud service. The Mac must remain running with remote access enabled. Public branding is Ribbon Field; bundle ID `com.agentcoord.ios`, URL scheme `agentcoord`, and stored-setting identifiers retain the internal name.

## Connect

1. Install Tailscale on the Mac and iPhone and connect both to the same tailnet.
2. In Ribbon Field on the Mac, enable HTTPS Remote access and create a fresh pairing link.
3. In the iOS app’s Connection sheet, paste the complete `https://<mac>.<tailnet>.ts.net/pair#pair=<invitation>` link, name the device, and tap **Pair this device**.
4. Keep the Mac and its Ribbon Field server available. Use the connection button to check the connection, pair again, or disconnect.

The sheet accepts other valid HTTPS origins as well, including a trusted localhost test fixture. HTTP, credentials in URLs, malformed invitations, and unsafe stored addresses are rejected. TLS is verified by the operating system without overrides. Plain HTTPS links open in the browser by default; copy and paste the invitation into the app. An optional `agentcoord://pair?url=<percent-encoded-complete-HTTPS-pairing-link>` handoff opens the native confirmation sheet; it does not pair automatically. Other `agentcoord` links are not repurposed as mobile commands.

## Build and test

Open `mobile/ios/RibbonField.xcodeproj` in full Xcode and select the shared **RibbonField** scheme. For a physical device, select your signing team in the app target. No team ID or signing credentials are checked in. The deployment target is 16.4. This project uses only system frameworks, with no package downloads.

From the repository root, these commands avoid changing the global developer-directory selection:

```sh
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer

# Simulator build
xcodebuild -project mobile/ios/RibbonField.xcodeproj -scheme RibbonField \
  -configuration Debug -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath mobile/ios/DerivedData CODE_SIGNING_ALLOWED=NO build

# Unit tests; select an available simulator from xcrun simctl list devices available
xcodebuild -project mobile/ios/RibbonField.xcodeproj -scheme RibbonField \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  -derivedDataPath mobile/ios/DerivedData CODE_SIGNING_ALLOWED=NO \
  -only-testing:RibbonFieldTests test

# Foundation-only policy and native pairing tests on macOS (no iOS simulator)
xcrun swift test --package-path mobile/ios

# Unsigned device build; installation/archive distribution requires signing
xcodebuild -project mobile/ios/RibbonField.xcodeproj -scheme RibbonField \
  -configuration Release -destination 'generic/platform=iOS' \
  -derivedDataPath mobile/ios/DerivedData CODE_SIGNING_ALLOWED=NO build
```

The selected CommandLineTools Swift installation may lack XCTest. Use the full Xcode `DEVELOPER_DIR` override for Swift Package tests as well. A package test pass alone does not validate SwiftUI/WebKit, signing or device behavior.

`RibbonFieldUITests` contains an optional end-to-end smoke test. It skips without the `RIBBON_SMOKE_URL` build setting. The repository provides `tests/ios_smoke_server.py --cert CERT --key KEY --port 19443`, which uses a fresh temporary database and rejects agent-work submissions. Use a dedicated simulator and this isolated Mac backend fixture with a simulator-trusted TLS certificate and `GET /__smoke__/invitation` returning `{"url":"https://.../pair#pair=..."}`. This route is a fixture facility, not a production backend route. The fixture must serve the real mobile web UI and permit opening a new-session draft. The test never sends a prompt or launches a model turn. It verifies native pairing, real UI load, cookie/origin persistence through app termination, a draft surviving background/foreground, native notification setup feedback when the Mac is unconfigured, and disconnect through relaunch.

```sh
TEST_RUNNER_RIBBON_SMOKE_URL=https://localhost:19443 \
xcodebuild -project mobile/ios/RibbonField.xcodeproj -scheme RibbonField \
  -destination 'platform=iOS Simulator,id=<DEDICATED-SIMULATOR-UUID>' \
  -parallel-testing-enabled NO -derivedDataPath mobile/ios/DerivedData \
  CODE_SIGNING_ALLOWED=NO RIBBON_SMOKE_URL=https://localhost:19443 \
  -only-testing:RibbonFieldUITests test
```

Regenerate the opaque, square iOS icon (adapted from the existing macOS brand asset) with:

```sh
swift mobile/ios/Scripts/GenerateIcon.swift \
  mobile/ios/RibbonField/Assets.xcassets/AppIcon.appiconset/AppIcon.png
```

## Connection and security behavior

- A pairing invitation exists only in native memory while entered/redeemed. It is never written to defaults, put into WebKit navigation/history, logged by app code, or sent in a request URL. An ephemeral URLSession sends JSON `{token,name}` to `/api/remote/pair` with the exact `Origin` header expected by the existing Mac backend. Every HTTP redirect during redemption is rejected.
- Only the returned Secure, HttpOnly, unexpired `__Host-agent-coord-device` cookie for the exact host and `/` path is copied into the persistent `WKWebsiteDataStore.default()` cookie store. The canonical HTTPS origin and local notification/lifecycle preferences are saved in UserDefaults; pairing credentials and Apple tokens are not. The app does not read Safari cookies or copy credentials to shared URLSession storage.
- Disconnect tears down the web view, deletes all WebKit cookies and website data, and removes the remembered origin. A durable deletion marker resumes cleanup if the app is terminated during disconnect. Disconnect also deletes website-local drafts. To invalidate the grant server-side, revoke the device in Remote access on the Mac; local disconnect alone cannot revoke an offline Mac’s record.
- Navigation remains on the paired scheme, host and effective port. Only a user-activated external HTTPS link can open outside the app, after confirmation. Other schemes, credential-bearing links, invitation links, popups without user activation, external redirects and unsupported downloads are blocked. Desktop-specific links/files remain Mac features. There is no native script-message bridge and no TLS/ATS bypass.
- The same WKWebView stays mounted through normal foreground changes. A lightweight same-origin config fetch checks connectivity without reloading; the web UI retains responsibility for routine EventSource reconnects. Temporary failures keep the current page and draft available.
- If the server’s CSRF token changes after a restart, its EventSource is permanently closed, or the user re-pairs a loaded page, the app requests an explicit reload with a warning to copy unsent text first. The shared web UI’s automatic 401 reload is intercepted while a page is loaded, allowing native re-pairing first. Confirmed reload retains the current URL/thread but clears unsent text/attachments. Memory-pressure web-process termination cannot retain DOM state; the app explains that limitation and offers recovery.

The small injected adaptation tracks the server EventSource, reads the existing `state.config.token` to detect a restarted backend, and hides the web notification controls. This is intentionally limited to the current bundled web UI; changes to those integration points need lifecycle smoke validation.

## Notifications and validation limits

Native APNs notifications are implemented for finished/failed turns, approval requests, and expired snoozes. Enable them in the Connection sheet after configuring the Mac with an Apple push key and installing a correctly signed iPhone build. See [Apple push setup and validation](../../docs/native-push.md) for the exact configuration, signing, security, and delivery behavior. Safari/Home Screen Web Push grants remain separate from the native app; its browser setup controls stay hidden.

The app is a source/build deliverable, not an App Store/TestFlight deployment. Physical-device Tailscale connectivity, signing, iOS 16.4 runtime behavior, and distribution remain separate validation boundaries even when a newer simulator passes.

Apple documents the persistent default store in [WKWebsiteDataStore](https://developer.apple.com/documentation/webkit/wkwebsitedatastore) and navigation enforcement in [WKNavigationDelegate](https://developer.apple.com/documentation/webkit/wknavigationdelegate).

### Validation performed on 2026-10-09

- Xcode 26.0.1: Debug iOS Simulator build and unsigned Release iPhoneOS build passed.
- All 18 Foundation security/transport tests passed on macOS Swift Package and iOS Simulator.
- The full 19-test Xcode scheme passed on an iPhone 17 Pro / iOS 26 simulator against the checked-in `tests/ios_smoke_server.py` HTTPS fixture. The UI scenario verified native pairing, real mobile UI, cookie persistence after termination, unsent composer text after background/foreground, native notification setup feedback for an unconfigured Mac, and disconnect remaining cleared after termination.
- The backend suite passed 686 tests with one skip. All nine native APNs tests passed, including the final key/response failure checks. Python compilation and plugin/skill validation passed; both installed plugin copies were refreshed and verified against all 95 source files.
- The live test caught a WebKit initialization dependency: create the WKWebView before installing its persistent cookie so the cookie-store completion callback can run. This is fixed in `ConnectionModel.pair`.
- Property lists and shared scheme XML validate. The app icon is 1024×1024 RGB without alpha. Physical-device Tailscale, an iOS 16.4 runtime, backend-restart/process-termination recovery prompts, Apple token enrollment/delivery and notification taps on a signed phone, and signing/distribution were not exercised by this smoke test. Live APNs requires the operator's key and signing setup described above.
