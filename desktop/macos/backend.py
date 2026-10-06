"""Lifetime adapter for the macOS app's private, loopback-only UI server.

All UI and coordination behavior stays in agent_coord. The parent keeps stdin
open; EOF also shuts down the server after a native-app crash or forced quit.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import signal
import sys
import threading

from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.ui import make_ui_server


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db")
    args = parser.parse_args()
    server = make_ui_server(CoordinationStore(args.db), host="127.0.0.1", port=0)
    stopping = threading.Event()

    def parent_closed() -> None:
        try:
            # A daemon blocked in BufferedReader.read can abort Python during
            # signal-driven shutdown. Poll the raw descriptor so it can exit.
            while not stopping.is_set():
                readable, _, _ = select.select([sys.stdin.fileno()], [], [], 0.2)
                if readable and not os.read(sys.stdin.fileno(), 1):
                    break
        finally:
            stopping.set()

    def shutdown() -> None:
        stopping.wait()
        server.shutdown()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopping.set())
    try:
        print(json.dumps({"status": "serving", "url": f"http://127.0.0.1:{server.server_port}/"}), flush=True)
        threading.Thread(target=parent_closed, name="desktop-parent", daemon=True).start()
        threading.Thread(target=shutdown, name="desktop-shutdown", daemon=True).start()
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CoordinationError, OSError) as exc:
        print(f"Agent Coord could not start: {exc}", file=sys.stderr)
        raise SystemExit(1)
