"""Recover terminal Kimi errors omitted by ACP, scoped to one new prompt.

Read only the mapped main agent's canonical, versioned journal. A cursor excludes
previous prompts; identity and a byte anchor reject replaced/rewritten logs.
Only bounded error fields leave this reader, never prompts or tool contents.
"""
from __future__ import annotations

import json
import os
import time

from .kimi_usage import WIRE_VERSIONS, _session_directory

MAX_BYTES = 64 * 1024 * 1024
MAX_LINE = 8 * 1024 * 1024
FLUSH_WAIT_SECONDS = 0.25
FLUSH_POLL_SECONDS = 0.005


class PromptJournal:
    def __init__(self, native_id, cwd):
        self.native_id, self.cwd = native_id, cwd
        self.started_ms = int(time.time() * 1000)
        self.path, self.identity, self.offset, self.anchor = None, None, 0, b""
        try:
            self.path = self._path()
            with self.path.open("rb") as journal:
                stat = os.fstat(journal.fileno())
                self.identity = (stat.st_dev, stat.st_ino)
                self.offset = stat.st_size
                journal.seek(max(0, self.offset - 128))
                self.anchor = journal.read(min(128, self.offset))
        except (OSError, ValueError):
            # Some runtimes create the native state/journal on the first prompt.
            # Its turn.prompt timestamp must then prove the prompt is new.
            pass

    def _path(self):
        directory, _ = _session_directory(self.native_id, self.cwd, None)
        return directory / "agents" / "main" / "wire.jsonl"

    def failure(self):
        deadline = time.monotonic() + FLUSH_WAIT_SECONDS
        while True:
            settled, failure = self._scan()
            if settled or time.monotonic() >= deadline:
                return failure
            time.sleep(FLUSH_POLL_SECONDS)

    def _scan(self):
        try:
            path = self._path()  # Revalidate native ID and cwd at completion.
            if self.path is not None and self.path != path:
                return True, None
        except (OSError, ValueError):
            return True, None
        try:
            with path.open("rb") as journal:
                stat = os.fstat(journal.fileno())
                if self.identity is not None:
                    if ((stat.st_dev, stat.st_ino) != self.identity or stat.st_size < self.offset):
                        return True, None
                    journal.seek(max(0, self.offset - 128))
                    if journal.read(len(self.anchor)) != self.anchor:
                        return True, None
                if stat.st_size - self.offset > MAX_BYTES:
                    return True, None
                journal.seek(0)
                header_line = journal.readline(MAX_LINE + 1)
                if len(header_line) > MAX_LINE:
                    return True, None
                if not header_line.endswith(b"\n"):
                    return False, None
                header = json.loads(header_line)
                if not isinstance(header, dict) or header.get("type") != "metadata" or header.get("protocol_version") not in WIRE_VERSIONS:
                    return True, None
                journal.seek(self.offset or journal.tell())
                # Never interpret the suffix of a record already being written
                # at submission as a new event.
                if self.offset and not self.anchor.endswith(b"\n"):
                    partial = journal.readline(MAX_LINE + 1)
                    if len(partial) > MAX_LINE:
                        return True, None
                    if not partial.endswith(b"\n"):
                        return False, None
                turn_id, prompt_id = None, None
                settled, failure, step_failure = False, None, None
                while journal.tell() < stat.st_size:
                    line = journal.readline(min(MAX_LINE + 1, stat.st_size - journal.tell()))
                    if len(line) > MAX_LINE:
                        return True, None
                    if not line.endswith(b"\n"):
                        break  # Ignore an incomplete tail after a terminal event.
                    record = json.loads(line)
                    if not isinstance(record, dict) or record.get("type") == "metadata":
                        return True, None
                    if record.get("agentId") != "main":
                        continue
                    fresh = (self.identity is not None
                             or (isinstance(record.get("time"), (int, float)) and record["time"] >= self.started_ms))
                    kind = record.get("type")
                    if kind == "turn.prompt":
                        if not fresh:
                            continue
                        turn_id, prompt_id = record.get("turnId"), record.get("promptId")
                        settled, failure, step_failure = False, None, None
                    elif kind in {"prompt.submitted", "prompt.started"}:
                        if not fresh:
                            continue
                        if prompt_id != record.get("promptId"):
                            turn_id, prompt_id = None, record.get("promptId")
                            settled, failure, step_failure = False, None, None
                    elif (turn_id is not None and kind == "turn.step.interrupted"
                          and record.get("turnId") == turn_id and record.get("reason") == "error"):
                        step_failure = _step_error(record.get("message"))
                    elif (turn_id is not None and kind == "turn.ended"
                          and record.get("turnId") == turn_id):
                        settled = True
                        if record.get("reason") == "failed":
                            failure = (_error(record.get("error")) or step_failure
                                       or {"message": "Kimi's native turn failed without error details."})
                        else:
                            failure, step_failure = None, None
                    elif kind in {"prompt.completed", "prompt.aborted"}:
                        if prompt_id is not None and record.get("promptId") == prompt_id:
                            settled = True
                            if kind == "prompt.completed" and record.get("reason") == "failed" and failure is None:
                                failure = step_failure or {"message": "Kimi's native prompt failed without error details."}
                        elif turn_id is None and prompt_id is None and fresh:
                            prompt_id = record.get("promptId")
                            settled = True
                            if kind == "prompt.completed" and record.get("reason") == "failed":
                                failure = {"message": "Kimi's native prompt failed without error details."}
                return settled, failure or step_failure
        except FileNotFoundError:
            return self.identity is not None, None
        except (OSError, ValueError, TypeError, AttributeError):
            return True, None  # An unavailable/unsupported log cannot establish failure.


def _step_error(value):
    if isinstance(value, str) and value.strip():
        return {"message": value.strip()[:2000]}
    return {"message": "Kimi's native turn failed without error details."}


def _error(value):
    if not isinstance(value, dict) or not isinstance(value.get("message"), str) or not value["message"].strip():
        return None
    result = {"message": value["message"][:2000]}
    for key in ("code", "name"):
        if isinstance(value.get(key), str):
            result[key] = value[key][:200]
    if isinstance(value.get("retryable"), bool):
        result["retryable"] = value["retryable"]
    details = value.get("details")
    if isinstance(details, dict) and type(details.get("statusCode")) is int:
        result["statusCode"] = details["statusCode"]
    return result
