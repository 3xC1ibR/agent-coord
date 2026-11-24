#!/usr/bin/env python3
"""Exercise the built native app with a temporary database and no model turns."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import plistlib
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]


def read_window_origins(address: Path, expected_pid: int) -> list:
    """Read one new snapshot from the running native process on macOS."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(address))
        # Darwin sys/un.h: SOL_LOCAL, LOCAL_PEERPID.
        if client.getsockopt(0, 2) != expected_pid:
            raise RuntimeError("Window observation came from another process")
        with client.makefile("rb") as stream:
            data = stream.read(65537)
    if len(data) > 65536:
        raise RuntimeError("Window observation exceeds its size limit")
    if not data:
        raise ConnectionResetError("Native interface closed before returning a window observation")
    return json.loads(data)["windows"]


class WindowServerObserver:
    def __init__(self):
        self.graphics = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        self.foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        pointer, index = ctypes.c_void_p, ctypes.c_long
        self.owner_key = pointer.in_dll(self.graphics, "kCGWindowOwnerPID")
        self.number_key = pointer.in_dll(self.graphics, "kCGWindowNumber")
        self._signature(self.graphics.CGWindowListCopyWindowInfo, [ctypes.c_uint32, ctypes.c_uint32], pointer)
        self._signature(self.foundation.CFArrayGetCount, [pointer], index)
        self._signature(self.foundation.CFArrayGetValueAtIndex, [pointer, index], pointer)
        self._signature(self.foundation.CFDictionaryGetValue, [pointer, pointer], pointer)
        self._signature(self.foundation.CFNumberGetValue, [pointer, index, pointer], ctypes.c_bool)
        self._signature(self.foundation.CFRelease, [pointer], None)

    @staticmethod
    def _signature(function, arguments, result):
        function.argtypes, function.restype = arguments, result

    def _integer(self, dictionary, key):
        number = self.foundation.CFDictionaryGetValue(dictionary, key)
        value = ctypes.c_int64()
        if number and self.foundation.CFNumberGetValue(number, 4, ctypes.byref(value)):
            return value.value

    def windows_present(self, pid, numbers):
        # kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements.
        windows = self.graphics.CGWindowListCopyWindowInfo(1 | 16, 0)
        if not windows:
            raise RuntimeError("WindowServer enumeration is unavailable")
        try:
            for index in range(self.foundation.CFArrayGetCount(windows)):
                row = self.foundation.CFArrayGetValueAtIndex(windows, index)
                if self._integer(row, self.owner_key) == pid and self._integer(row, self.number_key) in numbers:
                    return True
            return False
        finally:
            self.foundation.CFRelease(windows)


def observe_window_moves(app, address, report, deadline):
    observations = set()
    last = None
    windows_gone = None
    observer = WindowServerObserver()
    directory = address.parent
    endpoint_file = directory / "origin-endpoints.json"
    staged_origin = directory / "observed-staged-origin"
    final_origin = directory / "observed-original-origin"
    endpoints = None
    while time.monotonic() < deadline:
        if final_origin.exists():
            if report.is_file():
                result = json.loads(report.read_bytes())
                if result.get("error"):
                    break
                numbers = set(result.get("quit_window_numbers", []))
                if len(numbers) == 2 and not observer.windows_present(app.pid, numbers):
                    windows_gone = time.monotonic_ns()
                    break
            elif app.poll() is not None:
                break
            time.sleep(.004)
            continue
        if app.poll() is not None:
            break
        try:
            windows = read_window_origins(address, app.pid)
        except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError, TimeoutError):
            time.sleep(.025)
            continue
        if len(windows) == 2 and all(row["ready"] for row in windows):
            rows = tuple(sorted((row["id"], row["origin"]) for row in windows))
            if rows != last:
                last = rows
                observations.add(rows)
                origins = {origin for _, origin in rows}
                if endpoints is None and endpoint_file.is_file():
                    endpoints = json.loads(endpoint_file.read_bytes())
                if endpoints and len({identifier for identifier, _ in rows}) == 2:
                    if origins == {endpoints["staged"]}:
                        staged_origin.touch()
                    elif origins == {endpoints["original"]} and staged_origin.exists():
                        final_origin.touch()
        time.sleep(.025)
    return observations, windows_gone


def require_smoke_bundle(app: Path) -> None:
    info = app / "Contents/Info.plist"
    if info.is_file() and plistlib.loads(info.read_bytes())["CFBundleIdentifier"] != "com.agentcoord.desktop.smoke":
        raise RuntimeError("Build a test app with desktop/macos/build.py --smoke-harness before running native smoke.")


def prepare_window_conversation(app, directory):
    """Seed a saved conversation before the disposable native backend starts."""
    # The imported modules are sealed resources in the candidate application.
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(app / "Contents/Resources/backend"))
    from agent_coord.codex_app_server import BrowserSessions
    from agent_coord.store import CoordinationStore
    store = CoordinationStore(directory / "state.sqlite3")
    sessions = BrowserSessions(store, str(directory))
    thread = "native-window-observation"
    try:
        store.register(session_id=thread, client="claude", cwd=str(directory), name="Window continuity")
        with store._connection() as db:
            db.execute("""INSERT INTO browser_sessions(thread_id,cwd,name,client,created_at,updated_at)
                          VALUES (?,?,?,?,?,?)""", (thread, str(directory), "Window continuity", "claude", time.time(), time.time()))
        sessions.history_persistence.save(thread, {"id": thread, "turns": []})
    finally:
        sessions.close()
    return thread


def stopped(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def smoke_processes(directory, previous=None):
    """Identify private runtime roots and their descendants, including detached groups."""
    database = directory / "state.sqlite3"
    transport = directory / ".state.sqlite3.runtime/provider-transport"
    output = subprocess.check_output(
        ["ps", "-axww", "-o", "pid=,ppid=,pgid=,stat=,lstart=,command="],
        text=True, errors="replace", timeout=5)
    rows = {}
    owned = {}
    for line in output.splitlines():
        fields = line.split(None, 9)
        if len(fields) < 10 or fields[3].startswith("Z"):
            continue
        pid, parent, group = map(int, fields[:3])
        born, command = tuple(fields[4:9]), fields[9]
        rows[pid] = (parent, group, born)
        native = any(flag in command for flag in ("--runtime-supervise ", "--runtime-serve "))
        if ((native and f" --db {database} " in command + " ")
                or ("agent_coord.provider_transport " in command and f" --directory {transport} " in command + " ")
                or (previous or {}).get(pid) == (group, born)):
            owned[pid] = (group, born)
    while True:
        children = {pid: (group, born) for pid, (parent, group, born) in rows.items()
                    if parent in owned and pid not in owned}
        if not children:
            return owned
        owned.update(children)


def stop_smoke_runtime(directory):
    sys.path.insert(0, str(ROOT / "plugins/agent-coord/scripts"))
    from agent_coord import desktop_runtime

    database = directory / "state.sqlite3"
    runtime = desktop_runtime.runtime_directory(database)
    owned = smoke_processes(directory)

    def survivors():
        nonlocal owned
        owned = smoke_processes(directory, owned)
        return owned

    def wait(seconds):
        deadline = time.monotonic() + seconds
        while survivors() and time.monotonic() < deadline:
            time.sleep(.1)
        return not owned

    try:
        discovery = runtime / "discovery.json"
        if discovery.is_file():
            desktop_runtime.control(json.loads(discovery.read_bytes()), "stop")
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Native smoke runtime stop: {error}", file=sys.stderr)
    finally:
        try:
            desktop_runtime.stop_retained_transport(runtime, database)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"Native smoke transport stop: {error}", file=sys.stderr)
        if not wait(2):
            for sig in (signal.SIGTERM, signal.SIGKILL):
                for pid, (group, _) in survivors().items():
                    try:
                        if pid == group:
                            os.killpg(group, sig)
                        else:
                            os.kill(pid, sig)
                    except ProcessLookupError:
                        continue
                if wait(2):
                    break
            else:
                raise RuntimeError(f"Disposable smoke processes survived cleanup: {sorted(owned)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=ROOT / "build/macos/Ribbon Field Smoke.app")
    parser.add_argument("--report", type=Path, default=ROOT / "build/macos/smoke.json")
    parser.add_argument("--timeout", type=float, default=110, help="Seconds allowed for each native smoke run.")
    parser.add_argument("--files-only", action="store_true", help="Run focused native file-browser checks.")
    parser.add_argument("--sessions-only", action="store_true", help="Run focused native session-switching checks.")
    parser.add_argument("--detach-only", action="store_true", help="Run focused native conversation-detachment checks.")
    parser.add_argument("--scratchpad-only", action="store_true", help="Run focused project scratchpad checks.")
    parser.add_argument("--notifications-only", action="store_true", help="Run focused notification and conversation link routing checks.")
    parser.add_argument("--attribution-only", action="store_true", help="Verify the native download-token handoff without sending telemetry.")
    parser.add_argument("--titlebar-only", action="store_true", help="Run focused workspace title band checks.")
    parser.add_argument("--runtime-only", action="store_true", help="Quit and reconnect two native interfaces to the same detached runtime.")
    parser.add_argument("--origins-only", action="store_true", help="Observe the origins of two native windows across a backend change.")
    args = parser.parse_args()
    bundle = args.app.expanduser().resolve()
    require_smoke_bundle(bundle)
    executable = bundle / "Contents/MacOS/Agent Coord"
    report = args.report.expanduser().resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    # The macOS Unix-domain socket address has a 104-byte limit, including the directory.
    temporary = tempfile.TemporaryDirectory(prefix="ribbon-native-smoke-", dir="/private/tmp" if args.origins_only else None)
    smoke_directory = Path(temporary.name).resolve()
    command = [str(executable), "--smoke-test", str(report), "--smoke-directory", str(smoke_directory)]
    if args.files_only:
        command.append("--smoke-files-only")
    if args.sessions_only:
        command.append("--smoke-sessions-only")
    if args.detach_only:
        command.append("--smoke-detach-only")
    if args.scratchpad_only:
        command.append("--smoke-scratchpad-only")
    if args.attribution_only:
        command.append("--smoke-attribution-only")
    if args.notifications_only:
        command.append("--smoke-notifications-only")
    if args.titlebar_only:
        command.append("--smoke-titlebar-only")
    if args.runtime_only:
        command += ["--smoke-runtime-only", "--smoke-preserve-runtime"]
    address = smoke_directory / "origins"
    if args.origins_only:
        saved = prepare_window_conversation(bundle, smoke_directory)
        command += ["--smoke-origin-change-only", "--smoke-observe-origins", saved,
                    "--observe-window-origins", str(address), "--smoke-preserve-runtime"]
    first = None
    try:
        for attempt in range(2 if args.runtime_only else 1):
            with tempfile.TemporaryFile() as diagnostics, subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=diagnostics
            ) as app:
                deadline = time.monotonic() + args.timeout
                try:
                    if args.origins_only:
                        observed, windows_gone = observe_window_moves(app, address, report, deadline)
                    app.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as error:
                    diagnostics.seek(max(0, diagnostics.seek(0, os.SEEK_END) - 4000))
                    raise RuntimeError("Native smoke test timed out: " + diagnostics.read().decode(errors="replace")) from error
                finally:
                    if app.poll() is None:
                        app.kill()
                        app.wait(timeout=5)
                if app.returncode != 0 or not report.exists():
                    diagnostics.seek(max(0, diagnostics.seek(0, os.SEEK_END) - 4000))
                    raise RuntimeError(f"Native app failed (exit {app.returncode}): " + diagnostics.read().decode(errors="replace"))
            if args.runtime_only:
                current = json.loads(report.read_text())
                if current.get("error") or not current.get("runtime_attached"):
                    raise RuntimeError(str(current))
                if attempt == 0:
                    first = current
                    if stopped(first["backend_pid"]):
                        raise RuntimeError("Ordinary native Quit stopped the detached runtime")
                    command.remove("--smoke-preserve-runtime")
                elif current["backend_pid"] != first["backend_pid"] or current["url"] != first["url"]:
                    raise RuntimeError("Native relaunch did not reconnect to the same runtime")
        if args.origins_only:
            result = json.loads(report.read_text())
            if result.get("error"):
                raise RuntimeError(result["error"])
            requested = result.get("quit_action_monotonic_ns")
            if result.get("quit_window_server_before") != 2 or windows_gone is None or not requested:
                raise RuntimeError("WindowServer did not observe both windows disappearing on ordinary Quit")
            quit_time = (windows_gone - requested) / 1_000_000
            if not 0 <= quit_time < 100:
                raise RuntimeError(f"Ordinary Quit windows disappeared in {quit_time:.1f} ms (limit 100 ms)")
            if stopped(result["backend_pid"]):
                raise RuntimeError("Ordinary Quit stopped the private runtime")
            window_set = json.loads((smoke_directory / "windows/state.json").read_text())
            if (sorted(row["id"] for row in window_set["windows"]) != sorted(result["draft_window_ids"])
                    or Path(window_set["database"]).resolve() != (smoke_directory / "state.sqlite3").resolve()):
                raise RuntimeError("Ordinary Quit did not preserve the two private window identities")
            restored_report = report.with_suffix(".restored.json")
            restored_report.unlink(missing_ok=True)
            restore = [str(executable), "--smoke-test", str(restored_report), "--smoke-directory", str(smoke_directory),
                       "--smoke-verify-quit", saved, "--smoke-window-restoration", "--smoke-preserve-runtime"]
            # A detached runtime can inherit file descriptors. Never wait for
            # EOF from a pipe once the native child has already exited.
            with tempfile.TemporaryFile() as diagnostics:
                restored = subprocess.run(restore, stdout=subprocess.DEVNULL, stderr=diagnostics, timeout=args.timeout)
                if restored.returncode or not restored_report.exists():
                    diagnostics.seek(max(0, diagnostics.seek(0, os.SEEK_END) - 4000))
                    raise RuntimeError("Native draft relaunch failed: " + diagnostics.read().decode(errors="replace"))
            recovery = json.loads(restored_report.read_text())
            if recovery.get("error") or sorted(result["draft_window_ids"]) != recovery.get("quit_restored_window_ids"):
                raise RuntimeError(recovery.get("error") or "Native relaunch did not restore the original draft windows")
    finally:
        # Quit keeps detached runtimes alive, including on a native timeout.
        stop_smoke_runtime(smoke_directory)
        temporary.cleanup()
    result = json.loads(report.read_text())
    if result.get("error"):
        raise RuntimeError(result["error"])
    if args.origins_only:
        result["quit_windows_gone_milliseconds"] = quit_time
        result["quit_window_server_remaining"] = 0
        expected = set(result["window_origins"].values())
        if len(expected) != 2 or result["url"] not in expected or any(
            urlsplit(origin).scheme != "http" or urlsplit(origin).hostname not in {"localhost", "127.0.0.1"}
            for origin in expected
        ):
            raise RuntimeError("Invalid private origins in the native smoke report")
        matching = {}
        for rows in observed:
            ids = tuple(identifier for identifier, _ in rows)
            values = {origin for _, origin in rows}
            if len(set(ids)) == 2 and len(values) == 1:
                matching.setdefault(ids, set()).update(values)
        if not any(expected <= origins for origins in matching.values()):
            raise RuntimeError("The same two native windows were not observed on both loopback origins")
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", args.app], check=True)
        result["observed_both_native_window_origins"] = True
        result["ordinary_quit_preserved_both_window_drafts"] = True
        result["runtime_survived_native_quit"] = True
        report.write_text(json.dumps(result, indent=2) + "\n")
        return 0
    if args.runtime_only:
        if not stopped(result["backend_pid"]):
            raise RuntimeError("Explicit native Stop and Quit left the test runtime alive")
        result.update(runtime_survived_native_quit=True, native_reconnected_same_runtime=True, backend_exited_on_quit=True)
        report.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        return 0
    titlebar = ("titlebar_controls_centered", "titlebar_page_state", "titlebar_header_in_band",
                "titlebar_sidebar_clears_controls", "titlebar_band_hits_web_view", "titlebar_band_click_reaches_page",
                "titlebar_collapsed_header_clears_controls", "titlebar_empty_band_requests_drag",
                "titlebar_content_press_ignored", "titlebar_controls_after_resize", "titlebar_tabs_match_controls", "titlebar_file_pane_band", "titlebar_compact_header_clears_controls")
    required = titlebar if args.titlebar_only else ("detach_normal_tab_click", "detach_escape_cancelled", "detach_reorder_preserved", "detach_window_created", "detach_state_restored", "detach_history_and_reading", "detach_window_focused", "detach_remaining_tabs",
                "detach_inside_window_ignored", "detach_last_tab_overview", "detach_shared_backend", "detach_compact_host",
                "detach_singleton_moves_window", "detach_drop_into_existing", "detach_repeat_moves_tab",
                "detach_dock_back_closes_empty_host", "detach_host_survives_reload") if args.detach_only else (
                "ui_loaded", "notification_bridge", "window_closed", "backend_survived_close",
                "download_attribution_link",
                "cold_start_link_queued", "app_link_project_filter", "app_link_source_window",
                "app_link_acknowledged", "app_link_back_preserves_draft", "app_link_saved_view_unchanged",
                "app_link_closed_window_fallback",
                "notification_reveals_existing_tab", "notification_preserves_draft",
                "notification_restores_minimized_owner", "notification_recent_window_fallback",
                "project_link_tab_in_own_project", "project_link_misplaced_tab_removed",
                "next_session_shortcut", "previous_session_shortcut",
                "next_session_wraps_in_filtered_view", "previous_session_wraps_and_preserves_draft",
                "next_session_tiled_shortcut", "next_session_selects_stacked_tab",
                "next_session_plain_tab_keeps_selection", "next_session_dialog_keeps_selection",
                "previous_session_preserves_tiled_draft",
                "tile_threads_shortcut", "pane_composer_focused", "pane_maximize_shortcut",
                "pane_maximized_from_composer", "pane_escape_restores_layout", "app_link_back_restores_pane_draft",
                "window_reopened", "preferences_persisted", "new_window_shortcut", "multiple_windows",
                "window_storage_isolated", "live_preferences_shared", "find_thread_shortcut", "commands_target_key_window",
                "new_session_shortcut", "new_session_button_and_shortcut_match",
                "roll_up_shortcut", "roll_up_active_window", "roll_up_oldest_waiting", "roll_up_queue_flow",
                "command_palette_shortcut", "command_palette_active_window", "command_palette_action",
                "command_palette_toggle", "command_palette_focus_and_modal",
                "project_scratchpad_shortcut", "project_scratchpad_active_window", "project_scratchpad_escape",
                "project_scratchpad_saved_and_focus_restored", "project_scratchpad_tab_and_isolation",
                "mixed_file_drop", "workspace_folder_drop", "window_drafts_and_navigation_isolated",
                "file_browser_default_collapsed", "file_browser_collapsed_layout", "file_browser_open_shortcut",
                "file_browser_opened", "file_browser_visible_preference_restored", "file_browser_hidden_preference_restored",
                "settings_opened", "settings_single_window", "settings_preferences_shared", "settings_closed",
                "settings_conversations_preserved", "settings_editor_picker_opens", "settings_editor_picker_cancels",
                "file_browser_follows_conversation", "file_browser_window_isolation", "file_browser_lazy_expansion",
                "file_browser_preview", "file_browser_inserts_without_sending", "file_browser_refresh",
                "file_browser_shortcut", "file_browser_collapsed",
                "external_window_rejected", "secondary_window_closed", "one_shared_backend")
    for key in required:
        if args.files_only and not (key.startswith(("file_browser_", "settings_")) or key == "ui_loaded"):
            continue
        if args.attribution_only and key not in {"ui_loaded", "download_attribution_link"}:
            continue
        if args.sessions_only and not (key.startswith(("next_session_", "previous_session_")) or key == "ui_loaded"):
            continue
        if args.scratchpad_only and not (key.startswith("project_scratchpad_") or key == "ui_loaded"):
            continue
        if args.notifications_only and not (key.startswith(("notification_", "project_link_")) or key in {"ui_loaded", "multiple_windows"}):
            continue
        if result.get(key) is not True:
            raise RuntimeError(f"Native behavior check failed: {key}")
    if not stopped(result["backend_pid"]):
        raise RuntimeError("The owned backend survived Quit")
    try:
        with urllib.request.urlopen(result["url"], timeout=1):
            raise RuntimeError("The backend port remained open after Quit")
    except OSError:
        pass
    result["backend_exited_on_quit"] = True
    report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if args.titlebar_only:
        print(f"Title band: {report}.titlebar.png")
        print(f"Title band with collapsed sidebar: {report}.titlebar-collapsed.png")
        return 0
    if not args.sessions_only and not args.scratchpad_only and not args.detach_only and not args.notifications_only:
        print(f"File browser: {report}.files.png")
        print(f"Collapsed file browser: {report}.files-collapsed.png")
    if not args.files_only and not args.sessions_only and not args.scratchpad_only and not args.detach_only and not args.notifications_only:
        print(f"Screenshot: {report}.png")
        print(f"Command palette: {report}.palette.png")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
