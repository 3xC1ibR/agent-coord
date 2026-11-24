"""Coordinator listener and post-commit startup lifecycles."""
from __future__ import annotations

import selectors
import socket
import threading


class RuntimeListeners:
    """Serve bound coordinator sockets with an immediate, shared wakeup."""

    def __init__(self, *servers):
        self.servers = servers
        self.wakeup, self.signal = socket.socketpair()
        self.thread = threading.Thread(target=self._run, name="runtime-listeners", daemon=True)

    def start(self):
        try:
            self.thread.start()
        except BaseException:
            self.wakeup.close()
            self.signal.close()
            raise

    def _run(self):
        with selectors.DefaultSelector() as selector:
            selector.register(self.wakeup, selectors.EVENT_READ)
            for server in self.servers:
                selector.register(server, selectors.EVENT_READ)
            while True:
                ready = selector.select()
                if any(key.fileobj is self.wakeup for key, _ in ready):
                    return
                for key, _ in ready:
                    # This is the same single ready-socket dispatch used by
                    # BaseServer.serve_forever. Each server owns its workers.
                    key.fileobj._handle_request_noblock()

    def close(self):
        if self.signal.fileno() == -1:
            return
        with self.signal, self.wakeup:
            self.signal.sendall(b"stop")
            self.thread.join()
