# Ribbon Field macOS capabilities

Ribbon Field keeps its Swift/AppKit shell, WKWebView interface, and local Python
backend. Desktop improvements can build on that architecture while preserving
the shared browser UI. This document records the behavior and design direction;
work-thread checkpoints record task status, with Beads available optionally.

## Current foundation

The app connects to an independent local runtime, presents native menus and file
panels, delivers Notification Center alerts, and persists UI preferences across
launches. Closing the final window or quitting the interface keeps work running.
**Stop Work and Quit…** explicitly stops the runtime and its agents. The
[build and usage guide](README.md) covers installation.

## Desktop interaction pass

The first pass was implemented and validated on October 7, 2026, under
**agent-coord-lnx**. The delivered behavior is:

| Area | Behavior |
| --- | --- |
| File and folder drops | Insert quoted absolute paths into the current message draft. Preserve image attachments, including mixed image and file drops. Never submit a message as part of a drop. |
| Workspace drops | Type `/cd ` and drop a folder to insert its quoted path; sending the command changes the chat directory. |
| File selection | A native file and folder panel inserts path references into the draft, including directory paths for `/cd`. |
| Keyboard commands | Expose session creation, search, composer focus, sidebar and conversation layout, sending, zoom, and window creation in the native menus. Keep normal text editing and modal dialogs working. |
| Additional windows | Open separate workspace or thread windows with independent navigation and drafts, sharing one backend and database. Route commands, panels, and notification clicks to the appropriate window. |
| Window lifecycle | Closing a secondary window releases its web view. Retain the final closed window for Dock reopening and background notifications. Ordinary Quit detaches from the runtime; Stop Work and Quit stops it. |

The native layer resolves paths only from the drag pasteboard or a user-selected
file panel. Non-image files and folders are references, not uploaded copies;
agent access still follows the session's permissions. Drops must not navigate
away from the app or insert into a different thread after an asynchronous reply.

Multiple windows share local preferences but keep session storage and unsent
drafts separate. Closing a secondary window discards its unsent draft; saved
conversations and running work remain. Restoring all windows after a full app
restart is outside this first pass.

## Command palette

The follow-up under **agent-coord-7d8** adds **⌘K** and **View → Command Palette…**.
The palette searches available actions and open threads across saved views,
including thread titles, project and repository names, and workspace paths.
Arrow keys move through results; Return selects; Escape or ⌘K closes the palette
and restores focus. Thread switching uses the existing navigation flow to keep
per-window drafts. Existing dialogs keep their keyboard behavior.

The desktop script and native menu provide this capability without changing the
shared browser UI or backend. Commands that send messages or stop work are not
palette entries. Results use accessible combobox/listbox semantics, loading and
empty states, and literal text for thread metadata.

## Distribution

The app can bundle a pinned Python runtime and all backend dependencies for
Apple Silicon or Intel, resolve the recipient's database and tool paths at
launch, and ship in a drag-to-Applications DMG. The build supports Developer ID
signing, hardened runtime, notarization, stapling, and release checksums. See
the [distribution guide](README.md#build-a-standalone-disk-image) for commands.
Provider CLIs and authentication remain separate setup requirements. Ad hoc
test builds are not public, notarized releases.

## Updates and runtime lifetime

The implemented updater uses pinned Sparkle with signed archives and appcasts,
separate feeds per architecture and channel, and a fresh compatibility decision
before installation or relaunch. A compatible desktop update preserves the
active runtime, tools, approvals, output, and queued work. The runtime retains
its executable resources outside the replaced bundle and activates its own
replacement only after closing admissions and proving it is idle.

Focused runtime tests, independent updater review, and final assembled validation
have passed (1,671 Python tests with seven optional skips, and 1,018 JavaScript
tests without skips). Independent signed replacement acceptance passed all 38
checks, preserving a pending approval and queued work through the update and
upgrading the runtime only when idle. Final notarized packaging and the initial
rollout remain pending. Production feeds and signing configuration must be provisioned before
customer update notifications are operational. See the
[update contract and rollout guide](../../docs/runtime-updates.md) for evidence
and the one-time transition from the old app-owned backend.

## Further capability direction

These are design options, not additional committed implementation:

| Capability | Implementation direction |
| --- | --- |
| Background access | Menu bar controls, Dock badges, launch at login, and configurable system-wide shortcuts. |
| Native screens | Add AppKit or SwiftUI panels where native controls improve settings, accessibility, or workflow. |

The main screens remain web content. Platform integrations can become richer
without changing their renderer; Electron or Chromium-specific behavior still
needs individual evaluation in WebKit.

## Validation results

The first pass passed 332 Python tests and 128 JavaScript tests, including 12
desktop bridge tests for draft preservation, mixed drops, modal behavior,
shortcut routing, and new-window requests. The
[native smoke report](../../build/macos/smoke.json) confirms two actual WKWebViews,
independent navigation and session storage, live shared preferences, one backend,
file-reference delivery, active-window menu routing, and close/reopen/quit
behavior. The native build passes code-signature verification.

The first-pass build was installed at `~/Applications/Agent Coord.app`, passed
the [installed-app smoke checks](../../build/macos/installed-smoke.json), and was
reopened with its normal coordination database.

The Command-K follow-up passes 332 Python tests and 136 JavaScript tests,
including 20 desktop bridge and palette checks. The native smoke report also
checks the actual ⌘K menu shortcut, active-window targeting, filtering and
execution, repeated ⌘K, Escape focus restoration, and other modal dialogs.
The [palette screenshot](../../build/macos/smoke.json.palette.png) was inspected
in the native WKWebView. Because the installed app was hosting active sessions,
its update waits for those sessions to finish; the
[installation record](../../build/macos/command-palette-install.json) records
deployment and installed-app validation.

Native runtime smoke tests do not by themselves establish every physical Finder
drag gesture or keyboard layout. These remain useful hands-on checks alongside
the automated coverage.
