"""Dependency-free JSON-RPC stdio transport shared by ACP providers."""
from __future__ import annotations

from collections import deque
from concurrent.futures import Future, TimeoutError
import copy
import inspect
import json
import logging
import os
import signal
import subprocess
import threading
import uuid

from . import history_persistence
from .history_persistence import read_history
from .provider_transport import ProviderLaunchRejected
from .store import CoordinationError


class ACPError(CoordinationError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class _CompletedOutcome:
    """Synchronous Future-compatible wrapper passed to on_response callbacks."""

    def __init__(self, *, result=None, exception=None):
        self._result = result
        self._exception = exception

    def result(self, timeout=None):
        if self._exception is not None:
            raise self._exception
        return self._result

    def exception(self, timeout=None):
        return self._exception


def accepts_keyword(callable_object, parameter_name: str) -> bool:
    """Return whether callable_object accepts parameter_name as a keyword argument."""
    try:
        parameters = inspect.signature(callable_object).parameters
    except (TypeError, ValueError):
        return False
    return (parameter_name in parameters
            or any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()))


def run_or_capture_write(callback, *, required: bool = True) -> None:
    """Defer SQLite bookkeeping into CoordinatorProjection's transaction when active."""
    from .browser_persistence import _CAPTURE

    captured = getattr(_CAPTURE, "writes", None)
    if captured is not None:
        captured.append((callback, required))
        return
    callback()


def transport_handover_active(store) -> bool:
    projection = getattr(store, "provider_projection", None) if store is not None else None
    sessions = getattr(projection, "sessions", None) if projection is not None else None
    return bool(getattr(sessions, "transport_handover", False))


_STOIC_NOTICES = (
    "Stoic conversation sync is on for this session.",
    "Stoic conversation sync is off for this session.",
    "Stoic conversation sync is deferred; check its local sync state.",
    "Stoic conversation sync could not be enabled: the agent token is missing or its file is not private.",
)
_BOOTSTRAP_PREFIX = "This session is registered with agent-coord as "
_THREAD_CONTEXT_MARKER = "\nThread context (saved data): "
_UPDATED_CONTEXT_PREFIX = "Updated coordination context: "
_MUSE_ESCALATION_NOTE = (
    "For Agent Coord CLI calls, set the native bash tool's sandbox_permissions to "
    "require_escalated so Muse can ask for approval to access the coordination database "
    "outside this workspace. An ordinary sandboxed-command approval does not grant "
    "that filesystem access."
)
_SKILL_DIRECTORY_HEADER = (
    "Read a named skill from its listed directory before acting. "
    "Resolve its resources relative to SKILL.md."
)


def _consume_json_object(text: str, start_index: int) -> int | None:
    if start_index >= len(text) or text[start_index] != "{":
        return None
    try:
        _, end_index = json.JSONDecoder().raw_decode(text, start_index)
    except ValueError:
        return None
    return end_index


def strip_coordination_preamble(text: str) -> str:
    """Strip Turn-1 bootstrap, skill directory, or context-delta preamble from replayed text."""
    if not isinstance(text, str) or not text:
        return ""
    remaining = text
    changed = True
    while changed and remaining:
        changed = False
        trimmed = remaining.lstrip("\r\n")
        for notice in _STOIC_NOTICES:
            if trimmed.startswith(notice):
                remaining = trimmed[len(notice):].lstrip("\r\n")
                changed = True
                break
        if changed:
            continue
        if trimmed.startswith(_UPDATED_CONTEXT_PREFIX):
            json_end = _consume_json_object(trimmed, len(_UPDATED_CONTEXT_PREFIX))
            if json_end is not None:
                remaining = trimmed[json_end:].lstrip("\r\n")
                changed = True
                continue
        if trimmed.startswith(_BOOTSTRAP_PREFIX):
            marker_index = trimmed.find(_THREAD_CONTEXT_MARKER)
            if marker_index != -1:
                json_start = marker_index + len(_THREAD_CONTEXT_MARKER)
                json_end = _consume_json_object(trimmed, json_start)
                if json_end is not None:
                    tail = trimmed[json_end:].lstrip("\r\n")
                    if tail.startswith(_MUSE_ESCALATION_NOTE):
                        tail = tail[len(_MUSE_ESCALATION_NOTE):].lstrip("\r\n")
                    if tail.startswith(_SKILL_DIRECTORY_HEADER):
                        lines = tail[len(_SKILL_DIRECTORY_HEADER):].splitlines()
                        line_index = 0
                        while line_index < len(lines):
                            entry = lines[line_index]
                            if not entry.strip():
                                line_index += 1
                                continue
                            if entry.startswith("Directory: ") or (
                                entry.startswith("- ") and entry.endswith("/SKILL.md)")
                            ):
                                line_index += 1
                                continue
                            break
                        tail = "\n".join(lines[line_index:]).lstrip("\r\n")
                    remaining = tail
                    changed = True
                    continue
        if trimmed.startswith(_SKILL_DIRECTORY_HEADER):
            lines = trimmed[len(_SKILL_DIRECTORY_HEADER):].splitlines()
            line_index = 0
            while line_index < len(lines):
                entry = lines[line_index]
                if not entry.strip():
                    line_index += 1
                    continue
                if entry.startswith("Directory: ") or (entry.startswith("- ") and entry.endswith("/SKILL.md)")):
                    line_index += 1
                    continue
                break
            remaining = "\n".join(lines[line_index:]).lstrip("\r\n")
            changed = True
    return remaining


def _starts_with_known_preamble(text: str) -> bool:
    trimmed = text.lstrip("\r\n")
    return (
        trimmed.startswith(_BOOTSTRAP_PREFIX)
        or trimmed.startswith(_UPDATED_CONTEXT_PREFIX)
        or trimmed.startswith(_SKILL_DIRECTORY_HEADER)
        or any(trimmed.startswith(notice) for notice in _STOIC_NOTICES)
    )


def _bounded_value(value):
    encoded = json.dumps(value, ensure_ascii=False)
    return value if len(encoded) <= 64000 else encoded[:64000] + "…"


class ACPReplayCollector:
    """Reconstruct completed turns from native ACP session/load notifications."""

    def __init__(self, thread_id: str, *, provider: str, unknown_empty_tool_result: bool = False):
        self.thread_id = thread_id
        self.provider = provider
        self.unknown_empty_tool_result = unknown_empty_tool_result
        self._turns: list[dict] = []
        self._current: dict | None = None

    def _start_turn(self) -> dict:
        turn = {"items": [{"type": "userMessage", "content": []}], "has_response": False}
        self._turns.append(turn)
        self._current = turn
        return turn

    def observe(self, update: dict) -> None:
        kind = update.get("sessionUpdate")
        if kind == "user_message_chunk":
            content = update.get("content") or {}
            part_type = content.get("type")
            if part_type not in {"text", "image"}:
                return
            if self._current is None or self._current["has_response"]:
                turn = self._start_turn()
            else:
                turn = self._current
            user_content = turn["items"][0]["content"]
            if part_type == "text":
                raw_text = content.get("text", "")
                if not isinstance(raw_text, str) or not raw_text:
                    return
                if _starts_with_known_preamble(raw_text) and strip_coordination_preamble(raw_text) == "":
                    return
                if user_content and user_content[-1].get("type") == "text":
                    user_content[-1]["text"] += raw_text
                else:
                    user_content.append({"type": "text", "text": raw_text})
            elif part_type == "image":
                url = content.get("url")
                if not isinstance(url, str) or not url:
                    mime_type = content.get("mimeType") or content.get("mediaType") or "image/png"
                    data = content.get("data") or content.get("base64Data") or ""
                    if not data:
                        return
                    url = f"data:{mime_type};base64,{data}"
                user_content.append({"type": "image", "url": url})
            return

        if self._current is None:
            # Ignore orphan chunks emitted without a preceding user turn.
            return

        if kind in {"agent_message_chunk", "agent_thought_chunk"}:
            content = update.get("content") or {}
            if content.get("type") != "text":
                return
            text = content.get("text", "")
            if not isinstance(text, str) or not text:
                return
            self._current["has_response"] = True
            item_type = "agentMessage" if kind == "agent_message_chunk" else "reasoning"
            last_item = self._current["items"][-1] if self._current["items"] else {}
            if last_item.get("type") != item_type:
                last_item = (
                    {"type": "agentMessage", "text": ""}
                    if item_type == "agentMessage"
                    else {"type": "reasoning", "summary": [""]}
                )
                self._current["items"].append(last_item)
            if item_type == "agentMessage":
                last_item["text"] += text
            else:
                last_item["summary"][0] += text
        elif kind in {"tool_call", "tool_call_update"}:
            item_id = update.get("toolCallId")
            if not item_id:
                return
            self._current["has_response"] = True
            item = next(
                (entry for entry in self._current["items"] if entry.get("id") == item_id),
                None,
            )
            if item is None:
                default_title = "Antigravity tool" if self.provider == "antigravity" else "Kimi tool"
                item = {
                    "id": item_id,
                    "type": "mcpToolCall",
                    "tool": update.get("title", default_title),
                    "arguments": {},
                    "status": "inProgress",
                }
                self._current["items"].append(item)
            if "title" in update and isinstance(update["title"], str):
                item["tool"] = update["title"][:1000]
            if "rawInput" in update:
                item["arguments"] = _bounded_value(update["rawInput"])
            if "rawOutput" in update or "content" in update:
                output = update.get("rawOutput")
                if output is None:
                    output = update.get("content")
                if output is not None:
                    item["result"] = _bounded_value(output)
            if "status" in update:
                if self.unknown_empty_tool_result:
                    item["providerStatus"] = update["status"]
                status = {"pending": "inProgress", "in_progress": "inProgress"}.get(
                    update["status"], update["status"]
                )
                item["status"] = status
                if self.unknown_empty_tool_result:
                    item["statusReason"] = None
                    if update["status"] == "completed" and (
                        item.get("result") is None or item.get("result") == []
                    ):
                        item.update(
                            status="unknown",
                            statusReason="Antigravity reported completion without a tool result.",
                        )

    def finalize(self) -> list[dict]:
        completed_turns: list[dict] = []
        for turn_index, raw_turn in enumerate(self._turns):
            items = raw_turn["items"]
            user_item = items[0]
            cleaned_content = []
            for part in user_item["content"]:
                if part.get("type") == "text":
                    cleaned = strip_coordination_preamble(part.get("text", ""))
                    if cleaned:
                        cleaned_content.append({"type": "text", "text": cleaned})
                elif part.get("type") == "image" and part.get("url"):
                    cleaned_content.append({"type": "image", "url": part["url"]})
            response_items = items[1:]
            if not cleaned_content and not response_items:
                continue
            if not response_items:
                # Skip incomplete trailing prompts that produced no response items.
                continue
            user_item["content"] = cleaned_content
            fingerprint = json.dumps(
                {"turn_index": turn_index, "user": cleaned_content, "items": response_items},
                sort_keys=True,
                ensure_ascii=False,
            )
            turn_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"acp-replay:{self.provider}:{self.thread_id}:{turn_index}:{fingerprint}",
                )
            )
            finalized_items = []
            for item_index, item in enumerate(items):
                entry = copy.deepcopy(item)
                if "id" not in entry:
                    entry["id"] = str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"{turn_id}:item:{item_index}")
                    )
                if entry.get("status") == "inProgress":
                    entry["status"] = "completed"
                finalized_items.append(entry)
            completed_turns.append(
                {"id": turn_id, "status": "completed", "items": finalized_items}
            )
        return completed_turns


def _user_signature(turn: dict) -> tuple:
    parts = []
    for item in turn.get("items", []):
        if item.get("type") != "userMessage":
            continue
        for part in item.get("content", []):
            if part.get("type") == "text":
                cleaned = strip_coordination_preamble(part.get("text", "")).strip()
                if cleaned:
                    parts.append(("text", cleaned))
            elif part.get("type") == "image" and part.get("url"):
                parts.append(("image", part["url"]))
    return tuple(parts)


def _merge_turn_items(existing_turn: dict, replayed_turn: dict) -> bool:
    """Merge missing completed response items from replayed_turn into existing_turn."""
    changed = False
    existing_items = existing_turn.setdefault("items", [])
    by_id = {item["id"]: item for item in existing_items if isinstance(item.get("id"), str)}
    has_agent_text = any(
        item.get("type") == "agentMessage" and item.get("text", "").strip()
        for item in existing_items
    )
    for item in replayed_turn.get("items", []):
        if item.get("type") == "userMessage":
            continue
        item_id = item.get("id")
        if item_id and item_id in by_id:
            target = by_id[item_id]
            if target.get("status") in {"inProgress", "unknown"} and item.get("status") in {
                "completed",
                "failed",
            }:
                target.update(copy.deepcopy(item))
                changed = True
            continue
        if item.get("type") == "agentMessage" and has_agent_text:
            continue
        existing_items.append(copy.deepcopy(item))
        changed = True
    if existing_turn.get("status") in {"inProgress", "interrupted"} and replayed_turn.get(
        "status"
    ) == "completed":
        existing_turn["status"] = "completed"
        existing_turn.pop("termination", None)
        changed = True
    return changed


def merge_replayed_turns(existing_turns: list[dict], replayed_turns: list[dict]) -> tuple[list[dict], bool]:
    """Merge replayed native turns into existing_turns in chronological order without duplicates."""
    if not replayed_turns:
        return existing_turns, False
    saved = [copy.deepcopy(turn) for turn in existing_turns]
    index_by_id = {turn["id"]: idx for idx, turn in enumerate(saved) if isinstance(turn.get("id"), str)}
    matched_replayed_to_saved: dict[int, int] = {}
    used_saved: set[int] = set()
    search_floor = 0

    for replay_idx, replay_turn in enumerate(replayed_turns):
        replay_id = replay_turn.get("id")
        if isinstance(replay_id, str) and replay_id in index_by_id:
            saved_idx = index_by_id[replay_id]
            matched_replayed_to_saved[replay_idx] = saved_idx
            used_saved.add(saved_idx)
            search_floor = max(search_floor, saved_idx + 1)
            continue
        replay_sig = _user_signature(replay_turn)
        if not replay_sig:
            continue
        for candidate_idx in range(search_floor, len(saved)):
            if candidate_idx in used_saved:
                continue
            if _user_signature(saved[candidate_idx]) == replay_sig:
                matched_replayed_to_saved[replay_idx] = candidate_idx
                used_saved.add(candidate_idx)
                search_floor = candidate_idx + 1
                break

    changed = False
    for replay_idx, saved_idx in matched_replayed_to_saved.items():
        if _merge_turn_items(saved[saved_idx], replayed_turns[replay_idx]):
            changed = True

    if len(matched_replayed_to_saved) == len(replayed_turns):
        return saved, changed

    # Splice unmatched replayed turns into chronological position relative to matched anchors.
    merged: list[dict] = []
    saved_cursor = 0
    for replay_idx, replay_turn in enumerate(replayed_turns):
        if replay_idx in matched_replayed_to_saved:
            anchor = matched_replayed_to_saved[replay_idx]
            while saved_cursor <= anchor:
                merged.append(saved[saved_cursor])
                saved_cursor += 1
        else:
            # Advance past any earlier saved turns only if no later replayed turn anchors to them.
            next_anchor = next(
                (
                    matched_replayed_to_saved[later_idx]
                    for later_idx in range(replay_idx + 1, len(replayed_turns))
                    if later_idx in matched_replayed_to_saved
                ),
                None,
            )
            if next_anchor is None:
                while saved_cursor < len(saved):
                    merged.append(saved[saved_cursor])
                    saved_cursor += 1
            merged.append(copy.deepcopy(replay_turn))
            changed = True
    while saved_cursor < len(saved):
        merged.append(saved[saved_cursor])
        saved_cursor += 1
    return merged, changed


def _resolve_browser_sessions(store, on_event):
    projection = getattr(store, "provider_projection", None) if store is not None else None
    if projection is not None and getattr(projection, "sessions", None) is not None:
        return projection.sessions
    owner = getattr(on_event, "__self__", None)
    if owner is not None and hasattr(owner, "histories") and hasattr(owner, "lock"):
        return owner
    return None


def reconcile_replayed_history(store, on_event, thread_id: str, replayed_turns: list[dict]) -> bool:
    """Reconcile native replayed turns into saved and in-memory history outside sessions.lock."""
    if not replayed_turns or not thread_id:
        return False
    persisted = read_history(store, thread_id)
    if persisted is None:
        persisted = {"id": thread_id, "turns": []}
    sessions = _resolve_browser_sessions(store, on_event)
    to_persist = None

    if sessions is not None:
        with sessions.lock:
            resident = sessions.histories.get(thread_id)
            base_history = resident if resident is not None else persisted
            merged_turns, changed = merge_replayed_turns(
                base_history.get("turns", []), replayed_turns
            )
            if changed:
                updated = {**copy.deepcopy(base_history), "id": thread_id, "turns": merged_turns}
                sessions.histories[thread_id] = updated
                sessions._dirty_history(thread_id)
                to_persist = copy.deepcopy(updated)
    else:
        merged_turns, changed = merge_replayed_turns(persisted.get("turns", []), replayed_turns)
        if changed:
            to_persist = {**persisted, "id": thread_id, "turns": merged_turns}

    if to_persist is None:
        return False

    save_fn = getattr(history_persistence, "save_history", None)
    if save_fn is not None:
        save_fn(store, thread_id, to_persist)
    elif sessions is not None and hasattr(sessions, "history_persistence"):
        sessions.history_persistence.save(thread_id, to_persist)
    else:
        with store._connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS browser_history (thread_id TEXT PRIMARY KEY, history_json TEXT NOT NULL)"
            )
            history_persistence.ensure_schema(db)
        history_persistence.HistoryPersistence(store).save(thread_id, to_persist)
    return True


class ACPConnection:
    label = "ACP agent"
    sign_in = ""

    def __init__(
        self,
        callback,
        *,
        command,
        cwd,
        env,
        store=None,
        provider=None,
        thread_id=None,
        channel=None,
        pid=None,
    ):
        self.store = store
        self.provider = provider
        self.thread_id = thread_id
        self.callback = callback
        self.lock = threading.RLock()
        self.pending = {}
        self.response_callbacks = {}
        self.errors = deque(maxlen=12)
        self.closed = False
        self.readers = []
        projection = getattr(store, "provider_projection", None) if store is not None else None
        if projection is not None and provider is not None:
            self.channel = channel or f"{provider}:{thread_id or 'probe'}:{uuid.uuid4().hex[:8]}"
            kwargs = {"provider": provider, "thread_id": thread_id}
            if accepts_keyword(projection.open, "stderr_callback"):
                kwargs["stderr_callback"] = self._on_stderr
            self.process = projection.open(
                self.channel, command, env, cwd, self._on_stdout, **kwargs
            )
            return
        self.channel = channel
        try:
            self.process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            raise ProviderLaunchRejected(f"Cannot start {self.label}: {exc}") from exc
        self.readers = [
            threading.Thread(target=self._read, daemon=True),
            threading.Thread(target=self._stderr, daemon=True),
        ]
        for reader in self.readers:
            reader.start()

    def _init_attached(self, **kwargs):
        """Hook for subclasses to initialize instance state before projection.attach."""

    @classmethod
    def attach(cls, store, callback, thread_id, channel, pid, *, provider, returncode=None, **kwargs):
        connection = cls.__new__(cls)
        connection.store = store
        connection.provider = provider
        connection.thread_id = thread_id
        connection.channel = channel
        connection.callback = callback
        connection.lock = threading.RLock()
        connection.pending = {}
        connection.response_callbacks = {}
        connection.errors = deque(maxlen=12)
        connection.closed = False
        connection.readers = []
        connection._init_attached(**kwargs)
        projection = store.provider_projection
        attach_kwargs = {"provider": provider, "thread_id": thread_id}
        if accepts_keyword(projection.attach, "stderr_callback"):
            attach_kwargs["stderr_callback"] = connection._on_stderr
        connection.process = projection.attach(
            channel, pid, connection._on_stdout, **attach_kwargs
        )
        if returncode is not None:
            connection.process.returncode = returncode
        return connection

    def bind_pending_request(self, request_id, *, on_response=None) -> Future:
        """Rebind an in-flight JSON-RPC request ID after coordinator restart."""
        future = Future()
        future.request_id = request_id
        with self.lock:
            self.pending[request_id] = future
            if on_response is not None:
                self.response_callbacks[request_id] = on_response
        return future

    def write(self, message):
        projection = getattr(self.store, "provider_projection", None) if self.store is not None else None
        if projection is not None and self.channel is not None:
            if self.closed or self.process.poll() is not None:
                raise CoordinationError(f"{self.label} disconnected. Send again to resume.")
            projection.write(self.channel, {"jsonrpc": "2.0", **message})
            return
        with self.lock:
            if self.closed or self.process.poll() is not None:
                raise CoordinationError(f"{self.label} disconnected. Send again to resume.")
            try:
                self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise CoordinationError(f"Connection to {self.label} was lost.") from exc

    def submit(self, method, params, *, request_id=None, on_response=None):
        request_id = str(uuid.uuid4()) if request_id is None else request_id
        future = Future()
        future.request_id = request_id
        projection = getattr(self.store, "provider_projection", None) if self.store is not None else None
        if projection is not None and self.channel is not None:
            def register():
                if self.closed or self.process.poll() is not None:
                    raise CoordinationError(f"{self.label} disconnected. Send again to resume.")
                with self.lock:
                    self.pending[request_id] = future
                    if on_response is not None:
                        self.response_callbacks[request_id] = on_response
                return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}

            try:
                projection.write_registered(self.channel, register)
            except Exception:
                with self.lock:
                    self.pending.pop(request_id, None)
                    self.response_callbacks.pop(request_id, None)
                raise
            return future
        with self.lock:
            self.pending[request_id] = future
            if on_response is not None:
                self.response_callbacks[request_id] = on_response
        try:
            self.write({"id": request_id, "method": method, "params": params})
        except Exception:
            with self.lock:
                self.pending.pop(request_id, None)
                self.response_callbacks.pop(request_id, None)
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
            callbacks = list(self.response_callbacks.values())
            self.response_callbacks = {}
        outcome = _CompletedOutcome(exception=error)
        for callback in callbacks:
            callback(outcome)
        for future in pending:
            if not future.done():
                future.set_exception(error)

    def _consume_line(self, line):
        return False

    def _on_stderr(self, line):
        if not isinstance(line, str):
            return
        for segment in line.splitlines() or [line]:
            if not self._consume_line(segment):
                self.errors.append(segment.rstrip()[-1000:])

    def _resolve_response(self, message, *, projection=None):
        response_id = message.get("id")
        with self.lock:
            future = self.pending.pop(response_id, None)
            callback = self.response_callbacks.pop(response_id, None)
            if future is None and response_id is not None:
                key = str(response_id)
                future = self.pending.pop(key, None)
                if callback is None:
                    callback = self.response_callbacks.pop(key, None)
        if (future is None or future.done()) and callback is None:
            return
        if "error" in message:
            error = message["error"] if isinstance(message["error"], dict) else {"message": str(message["error"])}
            detail = str(error.get("message", "Request failed"))
            if error.get("code") == -32000 and self.sign_in:
                detail += ". " + self.sign_in
            exc = ACPError(f"{self.label}: {detail}", error.get("code"))
            outcome = _CompletedOutcome(exception=exc)

            def complete():
                if future is not None and not future.done():
                    future.set_exception(exc)
        else:
            result = message.get("result") or {}
            outcome = _CompletedOutcome(result=result)

            def complete():
                if future is not None and not future.done():
                    future.set_result(result)

        if callback is not None:
            callback(outcome)
        if projection is not None:
            projection.after_commit(complete)
        else:
            complete()

    def _on_stdout(self, message):
        if not isinstance(message, dict):
            return
        projection = getattr(self.store, "provider_projection", None) if self.store is not None else None
        if message.get("type") == "disconnected" or message.get("method") == "disconnected":
            raw_error = message.get("error") or (message.get("params") or {}).get("error")
            detail = str(raw_error or f"{self.label} disconnected.")
            if self.errors:
                stderr_tail = "\n".join(self.errors)
                if stderr_tail and stderr_tail not in detail:
                    detail = f"{detail} {stderr_tail}".strip()
            error = CoordinationError(detail)
            if projection is not None:
                projection.after_commit(lambda: self._fail(error))
            else:
                self._fail(error)
            if not self.closed:
                self.callback({"method": "disconnected", "params": {"error": detail}})
            return
        if "method" in message:
            self.callback(message)
        elif "id" in message:
            self._resolve_response(message, projection=projection)

    def _stderr(self):
        for line in self.process.stderr:
            if not self._consume_line(line):
                self.errors.append(line.rstrip()[-1000:])

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
                if "method" in message:
                    self.callback(message)
                elif "id" in message:
                    self._resolve_response(message)
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
        self.closed = True
        handover = transport_handover_active(self.store)
        if not handover:
            self._fail(CoordinationError(f"{self.label} connection closed."))
        projection = getattr(self.store, "provider_projection", None) if self.store is not None else None
        if projection is not None and self.channel is not None:
            if not handover:
                if getattr(getattr(projection, "in_event", None), "active", False):
                    projection.after_commit(lambda ch=self.channel: projection.close_channel(ch))
                else:
                    projection.close_channel(self.channel)
            return
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
