# Agent Coord for macOS

The native app displays the existing Agent Coord UI in a Swift AppKit/WKWebView
window. It starts a private local backend automatically and uses the same
coordination database as the CLI and plugins.

## Build and install

Requirements: macOS 12 or later, Xcode Command Line Tools (`xcode-select --install`),
Python 3.10 or later, and an installed, authenticated Codex CLI. Trust the Agent
Coord hooks through the existing plugin setup before starting agent sessions.

From the repository root:

```bash
python3 desktop/macos/build.py --install
open "$HOME/Applications/Agent Coord.app"
```

The build creates `build/macos/Agent Coord.app` and installs it into
`~/Applications`. Drag it to the Dock if desired. `--install-dir /Applications`
selects a different installation directory. Omit `--install` to build only.

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

- Closing the window (or Command-W) keeps the app and its work running. Click
  its Dock icon to reopen the same window.
- Command-Q quits. When this app has running turns or unanswered requests, a
  confirmation offers **Keep Working** or **Quit and Stop**. Saved conversations
  remain available on the next launch.
- Each app instance owns a server on an automatically allocated loopback port.
  It does not attach to or shut down servers started in other terminals.
- If the native app crashes or is force-quit, closing its parent pipe asks the
  backend to clean up its server and Codex connection.
- Native launches are single-instance. The app retains its window and backend
  when closed; normal Finder/Dock launches reopen that instance.
- UI preferences persist across launches even though the local port changes.
- External web links open in the default browser. File selection uses the
  native macOS file picker.

**Enable notifications** in the sidebar requests macOS notification permission.
Turn completion alerts use Notification Center. Clicking an alert focuses the
app and opens its thread while that page is still loaded; after a page reload,
it focuses the app. macOS controls alert style and permission in System Settings.

Menus include **File → Show Workspace** (Command-1), **View → Reload** (Command-R),
**View → Back** (Command-[), **View → Open in Browser**, and **Help → Open Backend Log**.
Logs are under `~/Library/Application Support/Agent Coord/backend.log` and rotate
at the next start when larger than 1 MB. Startup failures provide **Try Again**
and **Open Log** actions.

## Update and validate

Quit the native app, then rerun the build/install command to pick up backend or
UI changes. The installer refuses to replace a running executable, an unrelated
application, or a symbolic link. The repository's plugin refresh workflow still
applies whenever plugin sources change; the native app build is separate from
installing the Codex and Claude plugins.

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
node --test tests/test_desktop_bridge.js
python3 desktop/macos/build.py
python3 desktop/macos/smoke_test.py
```

The native smoke mode uses a temporary database and preference domain, starts
no model turns, loads the real UI in WKWebView, checks the notification bridge,
closes and reopens the window, verifies preference persistence, and quits. It
writes `build/macos/smoke.json` and a `.json.png` screenshot beside it, then
verifies the owned backend exited. The command fails if any check fails. Successful reports
have `error: null` and all behavior checks set to `true`. The backend tests also
exercise EOF, SIGTERM, an open event stream, independent server ownership, and
cleanup of a fake Codex subprocess.
