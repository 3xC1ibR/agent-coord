"""Dependency-free JSON-RPC stdio transport shared by ACP providers."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import uuid
from collections import deque
from concurrent.futures import Future, TimeoutError

from .store import CoordinationError


class ACPError(CoordinationError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class ACPConnection:
    label = "ACP agent"
    sign_in = ""

    def __init__(self, callback, *, command, cwd, env):
        self.callback = callback
        self.lock = threading.RLock()
        self.pending = {}
        self.errors = deque(maxlen=12)
        self.closed = False
        try:
            self.process = subprocess.Popen(command, cwd=cwd, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1, start_new_session=True)
        except OSError as exc:
            raise CoordinationError(f"Cannot start {self.label}: {exc}") from exc
        self.readers = [threading.Thread(target=self._read, daemon=True),
                        threading.Thread(target=self._stderr, daemon=True)]
        for reader in self.readers:
            reader.start()

    def write(self, message):
        with self.lock:
            if self.closed or self.process.poll() is not None:
                raise CoordinationError(f"{self.label} disconnected. Send again to resume.")
            try:
                self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise CoordinationError(f"Connection to {self.label} was lost.") from exc

    def submit(self, method, params):
        request_id, future = str(uuid.uuid4()), Future()
        with self.lock:
            self.pending[request_id] = future
        try:
            self.write({"id": request_id, "method": method, "params": params})
        except Exception:
            with self.lock:
                self.pending.pop(request_id, None)
            raise
        return future

    def request(self, method, params, *, timeout=60):
        future = self.submit(method, params)
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            raise CoordinationError(f"{self.label} did not respond to {method}.") from exc
        finally:
            with self.lock:
                self.pending = {k: v for k, v in self.pending.items() if v is not future}

    def _fail(self, error):
        with self.lock:
            pending, self.pending = list(self.pending.values()), {}
        for future in pending:
            if not future.done():
                future.set_exception(error)

    def _consume_line(self, line):
        return False

    def _stderr(self):
        for line in self.process.stderr:
            if not self._consume_line(line):
                self.errors.append(line.rstrip()[-1000:])

    def _read(self):
        try:
            for line in self.process.stdout:
                if self._consume_line(line):
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # Some ACP runtimes emit startup diagnostics.
                if not isinstance(message, dict):
                    continue
                if "method" in message:
                    self.callback(message)
                elif "id" in message:
                    with self.lock:
                        future = self.pending.pop(message["id"], None)
                    if future and not future.done():
                        if "error" in message:
                            error = message["error"]
                            detail = str(error.get("message", "Request failed"))
                            if error.get("code") == -32000 and self.sign_in:
                                detail += ". " + self.sign_in
                            future.set_exception(ACPError(f"{self.label}: {detail}", error.get("code")))
                        else:
                            future.set_result(message.get("result") or {})
        except (OSError, ValueError, TypeError, KeyError, CoordinationError) as exc:
            self.errors.append(str(exc)[-1000:])
        finally:
            error = CoordinationError(f"{self.label} disconnected. " + "\n".join(self.errors))
            self._fail(error)
            if not self.closed:
                self.callback({"method": "disconnected", "params": {"error": str(error)}})

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._fail(CoordinationError(f"{self.label} connection closed."))
        # Own only this process group, including tools spawned by the agent.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                if os.name == "posix":
                    os.killpg(self.process.pid, sig)
                elif self.process.poll() is None:
                    self.process.terminate() if sig == signal.SIGTERM else self.process.kill()
            except (ProcessLookupError, PermissionError):
                if self.process.poll() is None:
                    self.process.terminate() if sig == signal.SIGTERM else self.process.kill()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                continue
        for reader in self.readers:
            if reader is not threading.current_thread():
                reader.join(timeout=3)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()
