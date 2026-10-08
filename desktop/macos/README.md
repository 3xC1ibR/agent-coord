# Ribbon Field for macOS

The native app displays the Ribbon Field UI in a Swift AppKit/WKWebView
window. It starts a private local backend automatically and uses the same
coordination database as the CLI and plugins.

## Build and install

Requirements: macOS 12 or later, Xcode Command Line Tools (`xcode-select --install`),
Python 3.10 or later, and an installed, authenticated Codex CLI. Trust the Agent
Coord hooks through the existing plugin setup before starting agent sessions.

From the repository root:

```bash
python3 desktop/macos/build.py --install
open "$HOME/Applications/Ribbon Field.app"
```

The build creates `build/macos/Ribbon Field.app` and installs it into
`~/Applications`. Drag it to the Dock if desired. `--install-dir /Applications`
selects a different installation directory. Omit `--install` to build only.

When upgrading from Agent Coord, quit the old app first. Installation replaces
an identified `Agent Coord.app` in the selected installation directory with
`Ribbon Field.app`, including when a previous Ribbon Field build is present.
The installer refuses running apps, unrelated bundles, and symbolic links;
it restores the old bundles if the final replacement fails. Re-add Ribbon Field
to the Dock if an old shortcut still points to the previous filename.

The bundle ID, internal executable, `agentcoord://` links, preferences, and
existing data/log directories retain their Agent Coord identities. No data
migration or plugin/skill rename is needed. Existing hooks and CLI commands
continue to use `agent-coord`.

The app contains a snapshot of the shared Python backend and web assets from
`plugins/agent-coord/scripts/agent_coord/`. It does not reference the repository
or a versioned plugin cache at runtime. `backend-snapshot.json` inside its
resources records the bundled file hashes.

Python itself and Codex remain installed tools. The build records the Python
executable, tool PATH, and default coordination database so launching from Finder
works without a shell. Use `--python /path/to/python3` or `--database /path/to/state.sqlite3`
to override them. This is a local build for this Mac; rebuild if those paths
move or to use a different Mac. Public distribution would additionally need a
bundled runtime, Developer ID signing, and notarization. Local builds are ad hoc
signed by the build command.

## Window and process behavior

- **File → New Window** opens an independent workspace window. Use **Open
  Current View in New Window**, the conversation's ↗ button, or Command-click a
  thread to open it alongside the original. All windows share one backend and
  database; navigation, selected views, and message drafts are independent.
- Closing a secondary window (or Command-W) discards that window's unsent draft
  and releases its web view. Closing the final window keeps it and the app's
  work running. Click the Dock icon to reopen it. Window restoration after a
  full quit is not yet implemented.
- Command-Q quits. When this app has running turns or unanswered requests, a
  confirmation offers **Keep Working** or **Quit and Stop**. Saved conversations
  remain available on the next launch.
- Each app instance owns a server on an automatically allocated loopback port.
  It does not attach to or shut down servers started in other terminals.
- If the native app crashes or is force-quit, closing its parent pipe asks the
  backend to clean up its server and Codex connection.
- Native launches are single-instance. Additional windows belong to that same
  app; normal Finder/Dock launches reopen its current window.
- UI preferences persist across launches even though the local port changes.
- External web links open in the default browser. File selection uses the
  native macOS file picker.

## Open an app destination

The app registers `agentcoord://` links. From any directory, an installed
Agent Coord CLI can launch or focus the app at a filtered overview, saved view,
or conversation:

```bash
agent-coord ui open --project "Billing" --from-session <session-id>
agent-coord ui link --project "Billing"
agent-coord ui open --view "Release review"
agent-coord ui open --repository /opt/projects/example
agent-coord ui open --thread <session-id>
```

`ui link` returns a stable app URL containing destination IDs. It works across
app restarts and the backend's changing port. The app must be installed with
this URL support; rebuild older installations first. Links from the app's own
conversations navigate in the same window. External links launch the app or
focus its current window; `--from-session` targets the native window that sent
that session's latest message. A closed source window falls back to the current
one. Links received during startup wait until the UI is ready.

Overview links select temporary All work filters. They preserve named saved
views, thread placement, and message drafts. **Back** returns to the previous
view or conversation. The UI validates IDs before navigating and shows an error
for missing destinations or links for another coordination database.

The command returns `displayed` only after the UI acknowledges navigation.
`requested` means macOS accepted the request but display is still unconfirmed;
`failed` includes the navigation error. The default acknowledgement wait is
five seconds; `--wait 0..30` adjusts it. The native smoke test exercises the
delegate's URL handler with a real CLI-generated link and a temporary database,
including source-window selection, filters, saved views, acknowledgement, and
Back/draft preservation, without sending links to the user's installed app.

**Enable notifications** in the sidebar requests macOS notification permission.
Turn completion and Codex approval-request alerts use Notification Center.
Command, file-change, and permission approvals notify once while pending;
the conversation already open in a focused window stays quiet.
Clicking an alert focuses its
originating window and opens its thread while that page is still loaded; after
a page reload or secondary-window closure, it focuses the available window.
macOS controls alert style and permission in System Settings.

## Files and folders

The native **Files** pane follows the selected conversation's working folder.
Expand folders, select files for a read-only preview, or double-click a file to
open it in its default app. **Insert Path** adds a reference to the draft without
sending it. Right-click for Copy Path and Reveal in Finder. **Choose…** browses
another folder; **Follow conversation** returns to the conversation's folder.
Hidden files are optional. The visible pane refreshes every three seconds;
the refresh button updates it immediately. **View → Toggle File Browser**
(Option-Command-B) controls its visibility. See [design and open-source
references](FILE_BROWSER.md) for the shared backend and future remote/Windows
boundary.

Drop Finder files and folders into an editable conversation to insert their
quoted absolute paths at the cursor. Existing text is preserved and the message
is not sent. PNG, JPEG, WebP, and GIF images keep the existing attachment behavior,
including when dropped together with other files. Image limits remain four per
message and 5 MiB each; insert at most 32 path references per drop or file panel.

**File → Insert File or Folder Paths…** opens a native picker. This command
inserts references, including for selected images. Other files and folders are
not uploaded or copied; the agent accesses them under the session's permissions.

**New session** and **⌘N** open a chat directly. Type `/cd ` and drop a folder
or use the file command to insert its path, then send the command to change
directories. `/model`, `/effort`, and `/permissions` configure the chat; `/help`
lists commands. A file drop never sends a message automatically.

## Keyboard commands

Commands apply to the active window. Dialogs and disabled controls retain their
normal behavior, and Undo, Redo, Cut, Copy, Paste, and Select All use macOS editing.

| Command | Shortcut |
| --- | --- |
| Command palette | ⌘K |
| Next / previous session in current view | ⌃Tab / ⌃⇧Tab |
| Roll up waiting threads | ⌥⌘R |
| New session | ⌘N |
| New window | ⇧⌘N |
| Open current view in new window | ⌥⌘N |
| Insert file or folder paths | ⇧⌘O |
| Show workspace | ⌘1 |
| Find thread | ⌘F |
| Focus message | ⌘L |
| Send message | ⌘Return |
| Toggle sidebar | ⌥⌘S |
| Expand or collapse conversation | ⇧⌘F |
| Zoom in, out, actual size | ⌘+, ⌘−, ⌘0 |
| Back and forward | ⌘[, ⌘] |
| Reload | ⌘R |
| Close window | ⌘W |
| Minimize | ⌘M |
| Toggle full screen | ⌃⌘F |

**⌘K** (or **View → Command Palette…**) searches available commands and open
threads across your saved views. Match a thread's title, project, repository, or
workspace path; use ↑/↓ and Return to choose a result. Escape or ⌘K closes the
palette and restores your previous focus. Switching threads preserves drafts
within that window. Other open dialogs keep their normal keyboard behavior.

**Control-Tab** and **Control-Shift-Tab** cycle sessions in the current view's
displayed order, wrapping at either end and preserving drafts. In tiled views,
they include stacked tabs within each pane. They work while typing in a message;
from the overview they open the first or last session. These shortcuts apply to
the native app; browsers retain their own tab-switching shortcuts.

The Window menu lists open windows and includes **Bring All to Front**. Additional
menus include **View → Open in Browser** and **Help → Open Backend Log**.
Logs are under `~/Library/Application Support/Agent Coord/backend.log` and rotate
at the next start when larger than 1 MB. Startup failures provide **Try Again**
and **Open Log** actions.

**Roll up** opens the longest-waiting Now thread needing attention across projects,
regardless of the current view filters. It advances after an accepted message,
the final pending approval or answer, or **Handled & next**. Failed submissions
preserve the draft and stay in place. Local slash commands do not advance.
**Skip** leaves the thread waiting and excludes it for this pass; new requests
join the end without interrupting reading or typing. **Exit** or Escape ends the
mode; an open menu or dialog handles Escape first. An empty queue shows **All
caught up** and leaves the last conversation open. Each window has its own pass.
Starting from tiled threads opens a single conversation and preserves pane drafts.
Terminal conversations remain eligible; use their existing continuation controls
or Skip when a response cannot be sent from this window.

## Update and validate

Quit the native app, then rerun the build/install command to pick up backend or
UI changes. The installer refuses to replace a running executable, an unrelated
application, or a symbolic link. The repository's plugin refresh workflow still
applies whenever plugin sources change; the native app build is separate from
installing the Codex and Claude plugins.

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
node --test tests/test_desktop_bridge.js tests/test_desktop_palette.js
python3 desktop/macos/build.py
python3 desktop/macos/smoke_test.py
# Focused native session-switching check:
python3 desktop/macos/smoke_test.py --sessions-only
```

The native smoke mode uses a temporary database and preference domain, starts
no model turns, and loads the real UI in two WKWebViews. It checks native menu
shortcuts, independent drafts/navigation/session storage, shared preferences,
mixed file/image drops through a private pasteboard, directory folder drops into `/cd`, New session/⌘N parity,
command-palette filtering, execution, focus restoration and modal behavior,
Roll up shortcut routing, oldest-waiting order, handled/skip controls and window isolation,
window disposal and Dock reopening, the notification bridge, and quitting. It
writes `build/macos/smoke.json` with `.json.png`, `.json.palette.png`, and
`.json.roll-up.png` screenshots beside it, then verifies the owned backend exited.
The Roll up screenshot captures an active attention queue. The command fails
if any check fails. Successful reports
have `error: null` and all behavior checks set to `true`. The backend tests also
exercise EOF, SIGTERM, an open event stream, independent server ownership, and
cleanup of a fake Codex subprocess.

[macOS capabilities](CAPABILITIES.md) records the design, implementation boundary,
and further distribution and integration options. Beads owns task status.
