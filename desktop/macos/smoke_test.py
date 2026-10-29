#!/usr/bin/env python3
"""Exercise the built native app with a temporary database and no model turns."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]


def stopped(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=ROOT / "build/macos/Ribbon Field.app")
    parser.add_argument("--report", type=Path, default=ROOT / "build/macos/smoke.json")
    parser.add_argument("--files-only", action="store_true", help="Run focused native file-browser checks.")
    parser.add_argument("--sessions-only", action="store_true", help="Run focused native session-switching checks.")
    parser.add_argument("--detach-only", action="store_true", help="Run focused native conversation-detachment checks.")
    parser.add_argument("--scratchpad-only", action="store_true", help="Run focused project scratchpad checks.")
    parser.add_argument("--notifications-only", action="store_true", help="Run focused multi-window notification routing checks.")
    parser.add_argument("--attribution-only", action="store_true", help="Verify the native download-token handoff without sending telemetry.")
    parser.add_argument("--runtime-only", action="store_true", help="Quit and reconnect two native interfaces to the same detached runtime.")
    args = parser.parse_args()
    executable = args.app.expanduser().resolve() / "Contents/MacOS/Agent Coord"
    report = args.report.expanduser().resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="ribbon-native-smoke-")
    smoke_directory = Path(temporary.name)
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
    if args.runtime_only:
        command += ["--smoke-runtime-only", "--smoke-preserve-runtime"]
    first = None
    try:
        for attempt in range(2 if args.runtime_only else 1):
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as app:
                try:
                    _, errors = app.communicate(timeout=110)
                except subprocess.TimeoutExpired:
                    app.kill()
                    _, errors = app.communicate(timeout=5)
                    raise RuntimeError("Native smoke test timed out: " + errors[-4000:])
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
        # A timed-out native process no longer owns the runtime lifetime. Clean
        # up only this test's authenticated discovery, never a live app or PID.
        sys.path.insert(0, str(ROOT / "plugins/agent-coord/scripts"))
        from agent_coord import desktop_runtime
        discovery = smoke_directory / ".state.sqlite3.runtime/discovery.json"
        if discovery.exists():
            record = json.loads(discovery.read_text())
            desktop_runtime.control(record, "stop")
            for _ in range(150):
                if stopped(record["pid"]):
                    break
                time.sleep(.1)
        temporary.cleanup()
    result = json.loads(report.read_text())
    if result.get("error"):
        raise RuntimeError(result["error"])
    if args.runtime_only:
        if not stopped(result["backend_pid"]):
            raise RuntimeError("Explicit native Stop and Quit left the test runtime alive")
        result.update(runtime_survived_native_quit=True, native_reconnected_same_runtime=True, backend_exited_on_quit=True)
        report.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        return 0
    required = ("detach_normal_tab_click", "detach_escape_cancelled", "detach_reorder_preserved", "detach_window_created", "detach_state_restored", "detach_history_and_reading", "detach_window_focused", "detach_remaining_tabs",
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
        if args.notifications_only and not (key.startswith("notification_") or key in {"ui_loaded", "multiple_windows"}):
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
