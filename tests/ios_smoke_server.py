#!/usr/bin/env python3
"""Serve the real mobile UI over local HTTPS for the optional iOS UI smoke test.

Requires a localhost TLS certificate trusted by the dedicated test simulator.
Uses a temporary database, a fake Tailscale route, and blocks agent submissions.
Never modifies a user's coordination database or Tailscale configuration.
"""
from __future__ import annotations

import argparse
import copy
from http import HTTPStatus
from pathlib import Path
import ssl
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent-coord/scripts"))

from agent_coord.remote_access import RemoteAccess
from agent_coord.store import CoordinationStore
from agent_coord.ui import make_ui_server


class LocalRoute:
    def __init__(self):
        self.mapping = {}

    def hostname(self):
        return "localhost"

    def config(self):
        return copy.deepcopy(self.mapping)

    def enable(self, port, target):
        self.mapping = {
            "TCP": {str(port): {"HTTPS": True}},
            "Web": {f"localhost:{port}": {"Handlers": {"/": {"Proxy": target}}}},
        }

    def disable(self, port):
        self.mapping = {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cert", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--port", type=int, default=19443)
    args = parser.parse_args()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)

    with tempfile.TemporaryDirectory(prefix="ribbon-ios-smoke-") as directory:
        store = CoordinationStore(Path(directory) / "state.sqlite3")
        remote = RemoteAccess(store.database_path, tailscale=LocalRoute())
        server = make_ui_server(store, port=args.port, cwd=directory, remote_access=remote)
        parent = server.RequestHandlerClass

        class FixtureHandler(parent):
            def parse_request(self):
                valid = super().parse_request()
                if valid:
                    # Exercise remote authorization rather than localhost admin access.
                    self.headers["X-Forwarded-Proto"] = "https"
                return valid

            def do_GET(self):
                if self.path == "/__smoke__/invitation":
                    if not self._valid_host():
                        self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid host."})
                        return
                    self._json(HTTPStatus.OK, remote.pairing())
                    return
                super().do_GET()

            def do_POST(self):
                # Boot restores its selected folder through this read-only probe;
                # the backend confines it to our temporary workspace.
                if self.path not in {"/api/remote/pair", "/api/browser/workspaces/open"}:
                    self._json(HTTPStatus.FORBIDDEN, {"error": "The iOS smoke fixture cannot submit agent work."})
                    return
                super().do_POST()

        server.RequestHandlerClass = FixtureHandler
        try:
            remote.enable(server.server_port)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            print(f"iOS smoke fixture: https://localhost:{server.server_port}", flush=True)
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
