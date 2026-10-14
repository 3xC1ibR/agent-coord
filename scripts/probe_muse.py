#!/usr/bin/env python3
"""Exercise a fresh installed Muse adapter against the native, keyless echo host.

Run after the README plugin refresh. Uses a temporary coordination database and
workspace; Muse retains its own probe transcripts. No model API request is made.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scripts-root", required=True, type=Path)
    parser.add_argument("--muse-bin", required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.scripts_root.resolve()))
    from agent_coord.codex_app_server import BrowserSessions
    from agent_coord.muse_code import MuseConnection, MuseRPC
    from agent_coord.store import CoordinationStore

    class NoCodex:
        def __init__(self, *args):
            pass

        def request(self, *args):
            raise AssertionError("Muse probe must not call Codex")

        def close(self):
            pass

    with tempfile.TemporaryDirectory(prefix="ribbon-muse-probe-") as directory:
        root = Path(directory).resolve()
        store = CoordinationStore(root / "coord/state.sqlite3")
        workspace = root / "workspace"
        workspace.mkdir()
        native_events = []

        def factory(store, callback, options):
            command = [str(args.muse_bin.resolve()), "serve", "--provider", "echo"]
            if options.get("yolo"):
                command.append("--disable-sandbox")
            def event(message):
                native_events.append(message)
                callback(message)
            return MuseConnection(store, event, options, command=command)

        def make_sessions():
            return BrowserSessions(store, str(workspace), rpc_factory=NoCodex,
                muse_factory=lambda store, event: MuseRPC(store, event, connection_factory=factory))

        def send(sessions, thread, text):
            sessions.send(thread, {"message": text})
            with sessions.changed:
                if not sessions.changed.wait_for(lambda: thread not in sessions.active, timeout=30):
                    raise AssertionError("Muse turn did not finish")
            turn = sessions.read(thread)["thread"]["turns"][-1]
            if turn["status"] != "completed":
                raise AssertionError(json.dumps(turn.get("error", turn)))
            if not any(text in item.get("text", "") for item in turn["items"]):
                raise AssertionError("Muse echo response missing")
            return turn

        sessions = make_sessions()
        try:
            thread = sessions.create({"client": "muse"})["session"]["thread_id"]
            first = send(sessions, thread, "Ribbon Field native Muse probe")
            native_id = sessions.muse.native[thread]
        finally:
            sessions.close()
        sessions = make_sessions()
        try:
            if sessions.read(thread)["thread"]["turns"][-1] != first:
                raise AssertionError("Display history changed across restart")
            send(sessions, thread, "Resume the same Muse session")
            if sessions.muse.native[thread] != native_id:
                raise AssertionError("Native session identity changed")
            sessions.close_work_thread(thread)
            sessions.reopen_work_thread(thread)
            send(sessions, thread, "Reopen the same Muse session")
            sessions.send(thread, {"message": "Interrupt this Muse turn"})
            if thread in sessions.active:
                sessions.interrupt(thread)
            send(sessions, thread, "Continue after stop")
            if sessions.muse.native[thread] != native_id:
                raise AssertionError("Stop replaced the native session")
            hook_items = [e["params"]["item"] for e in native_events if e.get("method") == "item/completed"
                          and e.get("params", {}).get("item", {}).get("kind") == "hookRun"]
            if not hook_items or any(item.get("runStatus") != "completed" for item in hook_items):
                raise AssertionError("Native coordination hooks did not complete successfully")
            print(json.dumps({"status": "passed", "native_session": native_id,
                "checks": ["native handshake", "streaming", "restart", "close/reopen", "stop/resume"],
                "observed_hook_items": len(hook_items)}, indent=2))
        finally:
            sessions.close()


if __name__ == "__main__":
    main()
