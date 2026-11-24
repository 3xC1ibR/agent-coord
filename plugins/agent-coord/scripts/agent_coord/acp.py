"""Dependency-free JSON-RPC stdio transport shared by ACP providers."""
from __future__ import annotations

import json
import logging
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

    def __init__(self, callback, *, command, cwd, env, store=None, provider=None, thread_id=None):
        self.store, self.provider, self.thread_id = store, provider, thread_id
        self.channel = None
        self.bindings = {}
        self.callback = callback
        self.lock = threading.RLock()
        self.pending = {}
        self.errors = deque(maxlen=12)
        self.closed = False
        projection = getattr(store, "provider_projection", None) if thread_id else None
        if projection is not None:
            self.channel = provider + ":" + str(uuid.uuid4())
            self.readers = []
            self.process = projection.open(self.channel, command, env, cwd, self._message,
                stderr_callback=self._on_stderr, stdout_callback=self._consume_line,
                provider=provider, thread_id=thread_id)
            return
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
        if self.channel:
            if self.closed or self.process.poll() is not None:
                raise CoordinationError(f"{self.label} disconnected. Send again to resume.")
            self.store.provider_projection.write(self.channel, {"jsonrpc": "2.0", **message})
            return
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
            # The descriptor is checkpointed with the command before delivery.
            # Futures and callback closures never cross a coordinator boundary.
            self.bindings[request_id] = {"method": method, "command_id": params.get("commandId")}
            if self.channel:
                adapter = getattr(self.store.provider_projection.sessions, self.provider)
                self.bindings[request_id]["turn_id"] = adapter.turns.get(self.thread_id, {}).get("id")
        try:
            self.write({"id": request_id, "method": method, "params": params})
        except Exception:
            with self.lock:
                self.pending.pop(request_id, None)
                if not self.channel:
                    self.bindings.pop(request_id, None)
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
            self._on_stderr(line)

    def _on_stderr(self, line):
        if not self._consume_line(line):
            self.errors.append(line.rstrip()[-1000:])

    @classmethod
    def attach(cls, store, callback, thread_id, channel, pid, *, provider, state=None):
        connection = cls.__new__(cls)
        connection.store, connection.provider, connection.thread_id = store, provider, thread_id
        connection.callback, connection.channel = callback, channel
        connection.lock, connection.pending = threading.RLock(), {}
        connection.errors, connection.readers, connection.closed = deque(maxlen=12), [], False
        connection.bindings = (state or {}).get("bindings", {})
        connection.options, connection.auth_urls = {}, set()
        connection.temporary = connection.configuration = None
        connection.notifications, connection.dispatcher = None, None
        connection.resource_path = (state or {}).get("resource_path")
        connection.process = store.provider_projection.attach(channel, pid, connection._message,
            stderr_callback=connection._on_stderr, stdout_callback=connection._consume_line,
            provider=provider, thread_id=thread_id)
        return connection

    def _message(self, message):
        if "method" in message:
            self.callback(message)
            return
        if "id" not in message:
            return
        request_id = message["id"]
        binding = self.bindings.pop(request_id, {})
        result = Future()
        if "error" in message:
            error = message["error"]
            detail = str(error.get("message", "Request failed"))
            if error.get("code") == -32000 and self.sign_in:
                detail += ". " + self.sign_in
            result.set_exception(ACPError(f"{self.label}: {detail}", error.get("code")))
        else:
            result.set_result(message.get("result") or {})
        projection = self.store.provider_projection if self.channel else None
        if projection is not None and binding.get("method") == "session/prompt":
            # Complete adapter state in the same transaction as the replay
            # cursor. Completing only a Future after commit loses the turn on
            # a process exit between the cursor commit and its callback.
            adapter = getattr(projection.sessions, self.provider)
            adapter._prompt_done(self.thread_id, binding.get("turn_id"), result)
        elif projection is not None and binding.get("method") == "turn/start":
            adapter = getattr(projection.sessions, self.provider)
            try:
                response = result.result()
                if (response.get("turnId") != binding.get("command_id")
                        or response.get("disposition") not in {"started", "queued"}):
                    raise CoordinationError("Muse did not confirm the retained turn start.")
                adapter.initial_prompt.confirm(self.thread_id)
            except Exception as error:
                adapter._finish(self.thread_id, "failed", str(error))
        with self.lock:
            future = self.pending.get(request_id)
        if future is not None:
            def complete():
                with self.lock:
                    self.pending.pop(request_id, None)
                if not future.done():
                    error = result.exception()
                    future.set_exception(error) if error else future.set_result(result.result())
            projection.after_commit(complete) if projection is not None else complete()

    def _read(self):
        failure = None
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
                self._message(message)
        except Exception as exc:
            # Adapter callbacks can fail independently of the provider
            # process. Preserve the actual cause instead of reporting its
            # unrelated stderr tail as if the process had disconnected.
            # Completion callbacks may explicitly close this same reader's
            # stream. That is expected cleanup, not a second provider failure.
            if not self.closed:
                failure = f"{self.label} stream processing failed: {type(exc).__name__}: {str(exc)[-1000:]}"
                logging.getLogger(__name__).exception("%s stream processing failed", self.label)
        finally:
            error = CoordinationError(failure or (f"{self.label} disconnected. " + "\n".join(self.errors)))
            self._fail(error)
            if not self.closed:
                self.callback({"method": "disconnected", "params": {"error": str(error)}})

    def close(self):
        if self.closed:
            return
        if self.channel:
            if getattr(self.store.provider_projection.sessions, "transport_handover", False):
                return
            self.store.provider_projection.close_channel(self.channel)
            self.closed = True
            self._fail(CoordinationError(f"{self.label} connection closed."))
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
