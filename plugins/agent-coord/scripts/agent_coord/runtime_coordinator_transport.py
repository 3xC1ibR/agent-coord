"""Let a coordinator stop waiting for its own read without stopping a provider."""
from contextlib import closing
from http.client import HTTPConnection, HTTPException
import json
import socket
import threading

from .provider_transport import Client, TransportError, encode
from .runtime_edge import authority


class CoordinatorTransport(Client):
    def __init__(self, record, coordinator, epoch):
        super().__init__(record, coordinator, epoch)
        self._poll_lock = threading.Lock()
        self._poll_socket = None
        self._poll_cancelled = False

    def cancel_event_poll(self):
        with self._poll_lock:
            self._poll_cancelled = True
            if self._poll_socket:
                try:
                    self._poll_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # The read may already have ended.

    def resume_event_poll(self):
        with self._poll_lock:
            self._poll_cancelled = False

    def call(self, action, **values):
        event_poll = action == "events" and values.get("wait") is True
        empty = {"events": [], "event_wait": True}
        details = {"instance": self.record["instance"], "coordinator": self.coordinator,
                   "epoch": self.epoch, "action": action, **values}
        with closing(HTTPConnection(authority(self.record["url"]), timeout=10)) as connection:
            try:
                connection.connect()
                if event_poll:
                    with self._poll_lock:
                        if self._poll_cancelled:
                            return empty
                        self._poll_socket = connection.sock
                # This authenticated owner only listens on loopback. Avoid
                # process-wide proxy discovery on first claim and submission.
                connection.request("POST", "/", body=encode(details).encode(), headers={
                    "Authorization": "Bearer " + self.record["token"], "Content-Type": "application/json"})
                with connection.getresponse() as response:
                    if response.status != 200:
                        raise HTTPException(f"Provider transport returned {response.status}")
                    result = json.load(response)
            except (OSError, ValueError, HTTPException) as error:
                if event_poll:
                    with self._poll_lock:
                        if self._poll_cancelled:
                            return empty
                    raise TransportError("Provider event read is unavailable.") from error
                raise TransportError("Provider transport outcome unavailable; do not retry delivery blindly.") from error
            finally:
                if event_poll:
                    with self._poll_lock:
                        self._poll_socket = None
        if "error" in result:
            raise TransportError(result["error"])
        return result
