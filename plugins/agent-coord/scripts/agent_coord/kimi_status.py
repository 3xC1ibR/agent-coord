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
        try:
            path = self._path()  # Revalidate native ID and cwd at completion.
            if self.path is not None and self.path != path:
                return None
            with path.open("rb") as journal:
                stat = os.fstat(journal.fileno())
                if self.identity is not None:
                    if ((stat.st_dev, stat.st_ino) != self.identity or stat.st_size < self.offset):
                        return None
                    journal.seek(max(0, self.offset - 128))
                    if journal.read(len(self.anchor)) != self.anchor:
                        return None
                if stat.st_size - self.offset > MAX_BYTES:
                    return None
                journal.seek(0)
                header = json.loads(journal.readline(MAX_LINE + 1))
                if header.get("type") != "metadata" or header.get("protocol_version") not in WIRE_VERSIONS:
                    return None
                journal.seek(self.offset or journal.tell())
                # Never interpret the suffix of a record already being written
                # at submission as a new event.
                if self.offset and not self.anchor.endswith(b"\n"):
                    journal.readline(MAX_LINE + 1)
                turn_id, prompt_id, failure = None, None, None
                while journal.tell() < stat.st_size:
                    line = journal.readline(min(MAX_LINE + 1, stat.st_size - journal.tell()))
                    if len(line) > MAX_LINE:
                        return None
                    if not line.endswith(b"\n"):
                        break  # Ignore an incomplete tail after a terminal event.
                    record = json.loads(line)
                    if not isinstance(record, dict) or record.get("type") == "metadata":
                        return None
                    if record.get("agentId") != "main":
                        continue
                    if record.get("type") == "turn.prompt":
                        if self.identity is None and (not isinstance(record.get("time"), (int, float))
                                or record["time"] < self.started_ms):
                            continue
                        turn_id, prompt_id = record.get("turnId"), record.get("promptId")
                        failure = None
                    elif (turn_id is not None and record.get("type") == "turn.ended"
                          and record.get("turnId") == turn_id):
                        failure = _error(record.get("error")) if record.get("reason") == "failed" else None
                        if record.get("reason") == "failed" and failure is None:
                            failure = {"message": "Kimi's native turn failed without error details."}
                    elif (prompt_id is not None and record.get("type") == "prompt.completed"
                          and record.get("promptId") == prompt_id and record.get("reason") == "failed" and failure is None):
                        failure = {"message": "Kimi's native prompt failed without error details."}
                return failure
        except (OSError, ValueError, TypeError, AttributeError):
            return None  # An unavailable/unsupported log cannot establish failure.


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
