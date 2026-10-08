# Native workspace file browser

The Mac app uses an AppKit outline view beside its existing conversation web
view. It follows the focused conversation's working directory, including tiled
conversations and `/cd` changes. Choose a folder to browse independently;
enable **Follow conversation** to return. Each window owns its browser state.
**View → Toggle File Browser** (Option-Command-B) shows or hides the pane.

Folders load on expansion. The visible browser refreshes every three seconds
and offers manual refresh. Select a file for a bounded, read-only UTF-8 preview;
double-click to open files with the selected local application. The **Editor**
menu below the preview offers **System Default** and **Choose Application…**.
The selection is saved across launches and shared by all windows; Open, Return,
double-click and the context menu use it without changing macOS file associations.
A missing editor or launch failure displays an error so another app can be chosen.
Directory links
are leaves to avoid loops. Hidden files are optional. Context actions insert a
quoted path into the current draft, copy its path, or reveal it in Finder.
Dragging a row exports a local file URL using AppKit's normal drag machinery.
Browsing does not change agent permissions or send a prompt.

## Shared operations and future platforms

`agent_coord.workspace_files` owns directory listing and preview behavior. The
existing server exposes POST `/api/browser/files/list` and `/preview` under its
normal authenticated, same-origin/CSRF boundary. Requests include `root`, a
server-native absolute workspace path, and `path`, a slash-separated relative
path. Listing accepts `hidden`. Responses carry JSON entries or bounded text.
The server validates the root against its workspace filter and prevents paths
or symlinks from escaping the chosen root. This is not an isolation boundary
against arbitrary code running as the same OS user.

`WorkspaceFileProvider` separates the native view from the transport.
`BackendWorkspaceFiles` currently connects only to the app's private local
backend. Asynchronous operations and generation checks keep stale replies from
changing a different workspace or file. Finder, Open, and file URL drags are
local capabilities. Remote authentication, remote path insertion, downloads,
uploads and remote editor integration are not implemented by this change.

A future Windows client can reuse the Python operations and relative-path
protocol. The AppKit pane needs a Windows UI counterpart. A future remote
provider needs authenticated transport and capability-specific local actions;
server paths must not become local file URLs. No assumption that the client
and workspace share an operating system belongs in the shared protocol.

## References

Reference reviewed: [CodeEdit ProjectNavigator at fa2aebd](https://github.com/CodeEditApp/CodeEdit/tree/fa2aebd86373211c78626074b53ab75010767575/CodeEdit/Features/NavigatorArea/ProjectNavigator/OutlineView),
particularly its outline data source, folder rows and pasteboard file URLs.
CodeEdit is MIT licensed. We use it as a design reference, not as a vendored
dependency; no CodeEdit implementation is copied into this app.

Apple's [outline and split view sample](https://developer.apple.com/documentation/appkit/navigating-hierarchical-data-using-outline-and-split-views)
documents the native controls. This implementation uses those system controls
without adding a Swift package or replacing the existing application shell.

## Validation

Backend tests cover traversal, root scope, symlinks, hidden files, bounded
listing and previews, binary files, special files and HTTP access controls.
The native smoke test uses the real backend with temporary files and no model
turns, checking folder following, window isolation, lazy expansion, preview,
refresh, path insertion and the menu shortcut. Its `.files.png` artifact shows
the native pane alongside the conversation UI.

Run `python3 desktop/macos/smoke_test.py --files-only` for the focused browser
checks. The complete smoke run also exercises foreground window activation;
unlock the Mac for those checks. The focused run still verifies backend
shutdown and does not start a model turn.
