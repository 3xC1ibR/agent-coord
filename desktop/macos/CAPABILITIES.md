# Ribbon Field macOS capabilities

Ribbon Field keeps its Swift/AppKit shell, WKWebView interface, and local Python
backend. Desktop improvements can build on that architecture while preserving
the shared browser UI. This document records the behavior and design direction;
Beads remains the source of task status.

## Current foundation

The app owns its local backend, presents native menus and file panels, delivers
Notification Center alerts, and persists UI preferences across launches. Closing
the final window keeps work running; quitting stops the backend after checking
for active turns. The [build and usage guide](README.md) covers installation.

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
| Window lifecycle | Closing a secondary window releases its web view. Retain the final closed window for Dock reopening and background notifications. Quit stops the shared backend once. |

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

## Further capability direction

These are design options, not additional committed implementation:

| Capability | Implementation direction |
| --- | --- |
| Portable installation | Bundle Python, remove build-machine paths, and validate the package on another Mac. Codex authentication remains a setup requirement. |
| Public distribution | Developer ID signing, notarization, and a distributable installer. |
| Automatic updates | Integrate an updater such as [Sparkle](https://sparkle-project.org/documentation/) with signed releases and a hosted update feed. |
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
