#!/usr/bin/env python3
"""Exercise the built native app with a temporary database and no model turns."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
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
        if client.getsockopt(0, 2) != expected_pid:
            raise RuntimeError("Window observation came from another process")
        with client.makefile("rb") as stream:
            data = stream.read(65537)
    if len(data) > 65536:
        raise RuntimeError("Window observation exceeds its size limit")
    if not data:
        raise ConnectionResetError("Native interface closed before returning a window observation")
    return json.loads(data)["windows"]


def observe_single_origin_across_update(app, address: Path, smoke_directory: Path, app_bundle: Path) -> dict:
    """Verify two native windows stay on one permanent edge origin across a live coordinator upgrade."""
    sys.path.insert(0, str(ROOT / "plugins/agent-coord/scripts"))
    from agent_coord import desktop_runtime

    database = smoke_directory / "state.sqlite3"
    observed_origins: set[str] = set()
    window_ids: set[str] = set()
    sample_count = 0
    initial_backend_pid = None
    replaced_backend_pid = None
    upgrade_triggered = False

    while app.poll() is None:
        try:
            windows = read_window_origins(address, app.pid)
        except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError):
            time.sleep(0.02)
            continue
        if len(windows) == 2 and all(row["ready"] for row in windows):
            sample_count += 1
            for row in windows:
                observed_origins.add(row["origin"])
                window_ids.add(row["id"])
            if not upgrade_triggered and (smoke_directory / "windows-ready").exists():
                upgrade_triggered = True
                record = desktop_runtime.discover(database, timeout=5)
                if not record:
                    raise RuntimeError("Could not discover running smoke coordinator before update")
                initial_backend_pid = record["pid"]
                candidate = smoke_directory / "CandidateResources"
                shutil.copytree(app_bundle / "Contents/Resources", candidate, symlinks=True)
                metadata = json.loads((candidate / "backend.json").read_text())
                metadata["runtime_version"] = "99.0.0+999"
                (candidate / "backend.json").write_text(json.dumps(metadata, indent=2) + "\n")
                python_exe = metadata["python"]
                if not Path(python_exe).is_absolute():
                    python_exe = str(candidate / python_exe)
                staged = json.loads(subprocess.check_output(
                    [python_exe, "-B", str(candidate / "backend.py"), "--runtime-bootstrap",
                     "--resources", str(candidate), "--db", str(database), "--runtime-confirm-updates"],
                    env=dict(os.environ, PYTHONPATH=str(candidate / "backend"),
                             PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1"),
                    text=True, timeout=30,
                ))
                desktop_runtime.control(
                    record, "activate-update",
                    request_id="smoke-origin-continuity", payload=staged["pending"], timeout=15,
                )
            elif upgrade_triggered and replaced_backend_pid is None:
                try:
                    current = desktop_runtime.discover(database, timeout=1)
                    if current and current["pid"] != initial_backend_pid:
                        status = desktop_runtime.control(current, "status", timeout=2)
                        if (status.get("update") or {}).get("phase") == "committed" and status.get("status") == "serving":
                            replaced_backend_pid = status["pid"]
                            (smoke_directory / "update-committed").write_text(str(replaced_backend_pid))
                except (OSError, ValueError, RuntimeError):
                    pass
        time.sleep(0.02)

    return {
        "origins": sorted(observed_origins),
        "window_ids": sorted(window_ids),
        "samples": sample_count,
        "initial_backend_pid": initial_backend_pid,
        "replaced_backend_pid": replaced_backend_pid,
    }


def prepare_window_conversation(app, directory):
    """Seed a saved conversation before the disposable native backend starts."""
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


def smoke_processes(directory: Path, previous=None):
    """Identify private runtime roots (supervisor, backend, edge, transport) and all descendants."""
    database = directory / "state.sqlite3"
    runtime_dir = directory / ".state.sqlite3.runtime"
    transport = runtime_dir / "provider-transport"
    recorded_pids: set[int] = set()
    for record_file in (runtime_dir / "discovery.json", runtime_dir / "supervisor.json",
                        transport / "owner.json", runtime_dir / "runtime-edge/owner.json"):
        if record_file.is_file():
            try:
                payload = json.loads(record_file.read_bytes())
                for key in ("pid", "coordinator_pid", "edge_pid"):
                    if isinstance(payload.get(key), int) and payload[key] > 1:
                        recorded_pids.add(payload[key])
            except (OSError, ValueError):
                pass
    output = subprocess.check_output(
        ["ps", "-axww", "-o", "pid=,ppid=,pgid=,stat=,lstart=,command="],
        text=True, errors="replace", timeout=5)
    rows = {}
    owned = {}
    marker = str(directory)
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
                or ("agent_coord.runtime_edge " in command and f" --directory {runtime_dir} " in command + " ")
                or (pid in recorded_pids and marker in command)
                or (previous or {}).get(pid) == (group, born)):
            owned[pid] = (group, born)
    while True:
        children = {pid: (group, born) for pid, (parent, group, born) in rows.items()
                    if parent in owned and pid not in owned}
        if not children:
            return owned
        owned.update(children)


def stop_smoke_runtime(smoke_directory: Path):
    sys.path.insert(0, str(ROOT / "plugins/agent-coord/scripts"))
    from agent_coord import desktop_runtime

    database = smoke_directory / "state.sqlite3"
    runtime_dir = desktop_runtime.runtime_directory(database)
    owned = smoke_processes(smoke_directory)
    current_pgid = os.getpgrp()

    def survivors():
        nonlocal owned
        owned = smoke_processes(smoke_directory, owned)
        return owned

    def wait(seconds):
        deadline = time.monotonic() + seconds
        while survivors() and time.monotonic() < deadline:
            time.sleep(.05)
        return not owned

    try:
        discovery = runtime_dir / "discovery.json"
        if discovery.is_file():
            desktop_runtime.control(json.loads(discovery.read_bytes()), "stop", timeout=8)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Native smoke runtime stop: {error}", file=sys.stderr)
    finally:
        try:
            desktop_runtime.stop_retained_transport(database)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"Native smoke transport stop: {error}", file=sys.stderr)
        if not wait(2):
            for sig in (signal.SIGTERM, signal.SIGKILL):
                signalled_groups: set[int] = set()
                for pid, (group, _) in list(survivors().items()):
                    try:
                        if group > 1 and group != current_pgid and group not in signalled_groups:
                            signalled_groups.add(group)
                            os.killpg(group, sig)
                        else:
                            os.kill(pid, sig)
                    except (ProcessLookupError, PermissionError):
                        continue
                if wait(2):
                    break
            else:
                raise RuntimeError(f"Disposable smoke processes survived cleanup: {sorted(owned)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=ROOT / "build/macos/Ribbon Field.app")
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
    parser.add_argument("--origins-only", action="store_true", help="Verify permanent single-origin continuity across a live backend update.")
    args = parser.parse_args()
    app_bundle = args.app.expanduser().resolve()
    executable = app_bundle / "Contents/MacOS/Agent Coord"
    report = args.report.expanduser().resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
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
        saved = prepare_window_conversation(app_bundle, smoke_directory)
        command += ["--smoke-origin-change-only", "--smoke-observe-origins", saved,
                    "--observe-window-origins", str(address), "--smoke-preserve-runtime"]
    first = None
    observed = None
    try:
        for attempt in range(2 if args.runtime_only else 1):
            with ThreadPoolExecutor(max_workers=1) as executor, subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            ) as app:
                observation = (
                    executor.submit(observe_single_origin_across_update, app, address, smoke_directory, app_bundle)
                    if args.origins_only else None
                )
                try:
                    _, errors = app.communicate(timeout=args.timeout)
                except subprocess.TimeoutExpired:
                    app.kill()
                    _, errors = app.communicate(timeout=5)
                    raise RuntimeError("Native smoke test timed out: " + errors[-4000:])
                if observation is not None:
                    observed = observation.result()
                if app.returncode != 0 or not report.exists():
                    raise RuntimeError(f"Native app failed (exit {app.returncode}): {errors[-4000:]}")
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
    finally:
        stop_smoke_runtime(smoke_directory)
        temporary.cleanup()
    result = json.loads(report.read_text())
    if result.get("error"):
        raise RuntimeError(result["error"])
    if args.origins_only:
        port = urlsplit(result["url"]).port
        expected_origin = f"http://127.0.0.1:{port}/"
        if (not observed or observed["origins"] != [expected_origin]
                or len(observed["window_ids"]) != 2
                or not observed["initial_backend_pid"] or not observed["replaced_backend_pid"]
                or observed["initial_backend_pid"] == observed["replaced_backend_pid"]):
            raise RuntimeError(f"Native windows did not maintain permanent single-origin continuity across update: {observed}")
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", app_bundle], check=True)
        result.update(
            observed_permanent_single_origin=True,
            zero_webview_reloads=True,
            initial_backend_pid=observed["initial_backend_pid"],
            replaced_backend_pid=observed["replaced_backend_pid"],
        )
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
