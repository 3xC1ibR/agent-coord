"""Offset-indexed JSONL transcript storage with lock-free O(1) window reads.

Directory layout per conversation:
  <db_dir>/transcripts/<safe_id[:2]>/<safe_id>/
    live.jsonl          Append-only delta journal for the single running turn;
                        truncated to zero bytes immediately on turn completion.
    turns.compact.jsonl One UTF-8 JSON line per finalized turn, pre-abridged
                        with byte coordinates into turns.full.jsonl.
    turns.full.jsonl    Append-only lossless finalized turns.
    meta.json           Small top-level thread metadata excluding turns.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
import weakref
from pathlib import Path
from typing import Any

from .history_window import RECENT_TURNS, abridge_turn
from .timeline_clock import seconds

_SAFE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,119}")
_MAX_COALESCED_PREAD = 64 * 1024 * 1024
_NO_META = object()

THREAD_TURNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS thread_turns (
    thread_id TEXT NOT NULL,
    turn_seq INTEGER NOT NULL,
    turn_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at REAL,
    completed_at REAL,
    compact_offset INTEGER NOT NULL,
    compact_length INTEGER NOT NULL,
    full_offset INTEGER NOT NULL,
    full_length INTEGER NOT NULL,
    PRIMARY KEY (thread_id, turn_seq)
);
CREATE INDEX IF NOT EXISTS thread_turns_turn_id_idx
    ON thread_turns (thread_id, turn_id, turn_seq DESC);
"""


def safe_thread_id(thread_id: str) -> str:
    """Map an arbitrary thread_id to a collision-free sharded directory name."""
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("Thread identifier must be a non-empty string.")
    if _SAFE_ID_PATTERN.fullmatch(thread_id) and "~" not in thread_id:
        return thread_id
    digest = hashlib.sha256(thread_id.encode("utf-8", "surrogatepass")).hexdigest()
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", thread_id)[:48]
    return f"{digest[:16]}~{cleaned}"


def transcript_root(database_path: str | Path) -> Path:
    return Path(database_path).expanduser().resolve().parent / "transcripts"


def thread_directory(database_path: str | Path, thread_id: str) -> Path:
    safe_id = safe_thread_id(thread_id)
    return transcript_root(database_path) / safe_id[:2].lower() / safe_id


def ensure_schema(db: sqlite3.Connection) -> None:
    """Ensure the additive thread_turns offset index exists with all columns."""
    exists = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='thread_turns'"
    ).fetchone()
    if not exists:
        db.executescript(THREAD_TURNS_SCHEMA)
        return
    columns = {row[1] for row in db.execute("PRAGMA table_info(thread_turns)")}
    required = {
        "thread_id",
        "turn_seq",
        "turn_id",
        "compact_offset",
        "compact_length",
        "full_offset",
        "full_length",
    }
    if not required <= columns or "digest" in columns or "item_count" in columns:
        db.execute("DROP TABLE thread_turns")
        db.executescript(THREAD_TURNS_SCHEMA)
        return
    if "status" not in columns:
        db.execute("ALTER TABLE thread_turns ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'")
    if "started_at" not in columns:
        db.execute("ALTER TABLE thread_turns ADD COLUMN started_at REAL")
    if "completed_at" not in columns:
        db.execute("ALTER TABLE thread_turns ADD COLUMN completed_at REAL")
    if "UNIQUE" in (exists[0] or "").upper():
        db.executescript("""
            CREATE TABLE thread_turns_migrated (
                thread_id TEXT NOT NULL,
                turn_seq INTEGER NOT NULL,
                turn_id TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at REAL,
                completed_at REAL,
                compact_offset INTEGER NOT NULL,
                compact_length INTEGER NOT NULL,
                full_offset INTEGER NOT NULL,
                full_length INTEGER NOT NULL,
                PRIMARY KEY (thread_id, turn_seq)
            );
            INSERT INTO thread_turns_migrated (
                thread_id, turn_seq, turn_id, status, started_at, completed_at,
                compact_offset, compact_length, full_offset, full_length
            )
            SELECT thread_id, turn_seq, turn_id, status, started_at, completed_at,
                   compact_offset, compact_length, full_offset, full_length
            FROM thread_turns;
            DROP TABLE thread_turns;
            ALTER TABLE thread_turns_migrated RENAME TO thread_turns;
        """)
    db.execute(
        "CREATE INDEX IF NOT EXISTS thread_turns_turn_id_idx ON thread_turns (thread_id, turn_id, turn_seq DESC)"
    )


def _quick_turn_stamp(turn: dict) -> tuple:
    """Compute an O(1)-per-item structural stamp without JSON serialization or SHA-256."""
    items = turn.get("items") or []
    item_stamps = []
    for item in items:
        if not isinstance(item, dict):
            item_stamps.append(None)
            continue
        text = item.get("text")
        agg = item.get("aggregatedOutput")
        content = item.get("content")
        first_part = ""
        if isinstance(content, list) and content and isinstance(content[0], dict):
            part_text = content[0].get("text")
            if isinstance(part_text, str):
                first_part = part_text[:48]
        result_val = item.get("result")
        result_stamp: Any = None
        if isinstance(result_val, str):
            result_stamp = (len(result_val), result_val[:32], result_val[-32:])
        elif isinstance(result_val, dict):
            result_stamp = len(result_val)
        item_stamps.append(
            (
                item.get("id"),
                item.get("type"),
                item.get("status"),
                (len(text), text[:32], text[-32:]) if isinstance(text, str) else text,
                (len(agg), agg[:32], agg[-32:]) if isinstance(agg, str) else None,
                len(content) if isinstance(content, list) else None,
                first_part,
                result_stamp,
            )
        )
    return (
        turn.get("id"),
        turn.get("status"),
        turn.get("startedAt"),
        turn.get("completedAt"),
        turn.get("error"),
        tuple(item_stamps),
    )


def _pread_exact(fd: int, length: int, offset: int) -> bytes:
    chunks = []
    remaining = length
    position = offset
    while remaining > 0:
        piece = os.pread(fd, min(remaining, _MAX_COALESCED_PREAD), position)
        if not piece:
            break
        chunks.append(piece)
        read_len = len(piece)
        remaining -= read_len
        position += read_len
    return b"".join(chunks) if len(chunks) != 1 else chunks[0]


def _pread_ranges(fd: int, ranges: list[tuple[int, int]], *, max_gap: int = 65536) -> list[bytes]:
    """Read (offset, length) byte ranges, coalescing nearby spans while skipping superseded gaps."""
    if not ranges:
        return []
    indexed = sorted(enumerate(ranges), key=lambda item: item[1][0])
    out: list[bytes] = [b""] * len(ranges)
    group_start = indexed[0][1][0]
    group_end = group_start + indexed[0][1][1]
    group_items = [indexed[0]]
    for entry in indexed[1:]:
        off, length = entry[1]
        end = off + length
        if off - group_end <= max_gap:
            if end > group_end:
                group_end = end
            group_items.append(entry)
        else:
            buf = _pread_exact(fd, group_end - group_start, group_start)
            for orig_idx, (r_off, r_len) in group_items:
                rel = r_off - group_start
                out[orig_idx] = buf[rel : rel + r_len]
            group_start, group_end, group_items = off, end, [entry]
    buf = _pread_exact(fd, group_end - group_start, group_start)
    for orig_idx, (r_off, r_len) in group_items:
        rel = r_off - group_start
        out[orig_idx] = buf[rel : rel + r_len]
    return out


def _close_live_fds(live_fds: dict[str, int], lock: threading.Lock) -> None:
    with lock:
        descriptors = list(live_fds.values())
        live_fds.clear()
    for fd in descriptors:
        try:
            os.close(fd)
        except OSError:
            pass


class TranscriptStore:
    """Sharded JSONL transcript storage indexed by SQLite byte offsets."""

    def __init__(self, store: Any) -> None:
        self._store_ref = weakref.ref(store)
        self.database_path = Path(store.database_path).expanduser().resolve()
        self.root = transcript_root(self.database_path)
        self._live_lock = threading.Lock()
        self._live_fds: dict[str, int] = {}
        self._write_lock = threading.Lock()
        self._thread_write_locks: dict[str, threading.Lock] = {}
        # thread_id -> list of (turn_id, status, compact_offset, compact_len, full_offset, full_len, stamp)
        self._index_cache: dict[str, list[tuple]] = {}
        self._meta_cache: dict[str, Any] = {}
        self.write_generation = 0
        self._schema_ready = False
        self._coverage_lock = threading.Lock()
        self._saved_threads: set[str] | None = None
        self._partial_threads: set[str] | None = None
        self._finalizer = weakref.finalize(self, _close_live_fds, self._live_fds, self._live_lock)

    @property
    def store(self) -> Any:
        store = self._store_ref()
        if store is None:
            raise RuntimeError("CoordinationStore has been garbage-collected.")
        return store

    def _thread_lock(self, thread_id: str) -> threading.Lock:
        with self._write_lock:
            return self._thread_write_locks.setdefault(thread_id, threading.Lock())

    def directory_for(self, thread_id: str) -> Path:
        safe_id = safe_thread_id(thread_id)
        return self.root / safe_id[:2].lower() / safe_id

    def close(self) -> None:
        _close_live_fds(self._live_fds, self._live_lock)

    # ------------------------------------------------------------------
    # Live in-progress turn delta journal (live.jsonl)
    # ------------------------------------------------------------------

    def append_live(self, thread_id: str, event: dict) -> None:
        """Append a streaming frame to live.jsonl with zero SQLite locks or fsync."""
        if not thread_id:
            return
        encoded = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8", "surrogatepass")
        with self._live_lock:
            fd = self._live_fds.get(thread_id)
            if fd is None:
                directory = self.directory_for(thread_id)
                directory.mkdir(parents=True, exist_ok=True)
                fd = os.open(directory / "live.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                self._live_fds[thread_id] = fd
            os.write(fd, encoded)

    def truncate_live(self, thread_id: str) -> None:
        """Truncate live.jsonl to 0 bytes and release its descriptor on turn commit."""
        if not thread_id:
            return
        with self._live_lock:
            fd = self._live_fds.pop(thread_id, None)
            if fd is not None:
                try:
                    os.ftruncate(fd, 0)
                finally:
                    os.close(fd)
                return
        live_path = self.directory_for(thread_id) / "live.jsonl"
        try:
            if live_path.exists() and live_path.stat().st_size > 0:
                fd = os.open(live_path, os.O_WRONLY)
                try:
                    os.ftruncate(fd, 0)
                finally:
                    os.close(fd)
        except OSError:
            pass

    def read_live_events(self, thread_id: str) -> list[dict]:
        """Read buffered frames from live.jsonl for crash/handover recovery."""
        live_path = self.directory_for(thread_id) / "live.jsonl"
        try:
            raw = live_path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError:
            return []
        if not raw:
            return []
        events: list[dict] = []
        for line in raw.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                break
            try:
                parsed = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
        return events

    def reconstruct_live_turn(self, thread_id: str, base_turn: dict | None = None) -> dict | None:
        """Reconstruct a turn from turn activity in the provider journal."""
        events = self.read_live_events(thread_id)
        if not events:
            return base_turn
        turn: dict[str, Any] = copy.deepcopy(base_turn) if isinstance(base_turn, dict) else {
            "id": None,
            "status": "inProgress",
            "items": [],
        }
        items_by_id: dict[str, dict] = {
            item["id"]: item for item in turn.get("items", []) if isinstance(item, dict) and item.get("id")
        }
        claude_blocks: dict[int, dict] = {}
        has_turn_activity = False
        for entry in events:
            payload = entry
            if "stream" in entry and "data" in entry:
                if entry.get("stream") != "stdout":
                    continue
                try:
                    payload = json.loads(entry["data"]) if isinstance(entry["data"], str) else entry["data"]
                except ValueError:
                    continue
            if not isinstance(payload, dict):
                continue
            method = payload.get("method")
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
            if (isinstance(method, str) and method.startswith(("turn/", "item/"))
                    or payload.get("type") in ("user", "assistant", "stream_event")):
                has_turn_activity = True
            if method in {"turn/started", "turn/completed"}:
                incoming = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                if incoming.get("id"):
                    turn["id"] = incoming["id"]
                elif params.get("turnId"):
                    turn["id"] = params["turnId"]
                for key, value in incoming.items():
                    if key != "items":
                        turn[key] = copy.deepcopy(value)
                for item in incoming.get("items") or []:
                    if isinstance(item, dict) and item.get("id"):
                        existing = items_by_id.get(item["id"])
                        if existing is None:
                            copied = copy.deepcopy(item)
                            turn.setdefault("items", []).append(copied)
                            items_by_id[item["id"]] = copied
                        else:
                            existing.update(copy.deepcopy(item))
            elif method in {"item/started", "item/completed", "item/updated"}:
                if params.get("turnId") and not turn.get("id"):
                    turn["id"] = params["turnId"]
                item = params.get("item")
                if isinstance(item, dict) and item.get("id"):
                    item_id = item["id"]
                    existing = items_by_id.get(item_id)
                    if existing is None:
                        copied = copy.deepcopy(item)
                        turn.setdefault("items", []).append(copied)
                        items_by_id[item_id] = copied
                    else:
                        existing.update(copy.deepcopy(item))
            elif method in {"item/agentMessage/delta", "item/commandExecution/outputDelta", "item/delta"}:
                if params.get("turnId") and not turn.get("id"):
                    turn["id"] = params["turnId"]
                item_id = params.get("itemId")
                delta = params.get("delta")
                target = items_by_id.get(item_id) if isinstance(item_id, str) else None
                if target is not None and isinstance(delta, str):
                    field = (
                        "aggregatedOutput"
                        if method == "item/commandExecution/outputDelta"
                        else params.get("field", "text")
                    )
                    if field in {"text", "aggregatedOutput", "visibleOutput"}:
                        target[field] = (target.get(field) or "") + delta
            elif method == "session/update":
                update = params.get("update") if isinstance(params.get("update"), dict) else {}
                kind = update.get("sessionUpdate")
                if kind in ("user_message_chunk", "agent_message_chunk", "agent_thought_chunk",
                            "tool_call", "tool_call_update", "plan"):
                    has_turn_activity = True
                if kind in {"agent_message_chunk", "agent_thought_chunk"}:
                    content = update.get("content") if isinstance(update.get("content"), dict) else {}
                    text_chunk = content.get("text")
                    if content.get("type") == "text" and isinstance(text_chunk, str):
                        item_type = "agentMessage" if kind == "agent_message_chunk" else "reasoning"
                        turn_items = turn.setdefault("items", [])
                        last_item = turn_items[-1] if turn_items else None
                        if not isinstance(last_item, dict) or last_item.get("type") != item_type:
                            new_id = f"live-{len(turn_items)}"
                            last_item = (
                                {"id": new_id, "type": "agentMessage", "text": ""}
                                if item_type == "agentMessage"
                                else {"id": new_id, "type": "reasoning", "summary": [""]}
                            )
                            turn_items.append(last_item)
                            items_by_id[new_id] = last_item
                        if item_type == "agentMessage":
                            last_item["text"] = (last_item.get("text") or "") + text_chunk
                        else:
                            summary = last_item.setdefault("summary", [""])
                            summary[0] = (summary[0] or "") + text_chunk
                elif kind in {"tool_call", "tool_call_update"}:
                    call_id = update.get("toolCallId")
                    if isinstance(call_id, str) and call_id:
                        existing = items_by_id.get(call_id)
                        if existing is None:
                            existing = {
                                "id": call_id,
                                "type": "mcpToolCall",
                                "tool": str(update.get("title") or "tool")[:1000],
                                "arguments": {},
                                "status": "inProgress",
                            }
                            turn.setdefault("items", []).append(existing)
                            items_by_id[call_id] = existing
                        if "rawInput" in update:
                            existing["arguments"] = update["rawInput"]
                        if "rawOutput" in update or "content" in update:
                            existing["result"] = update.get("rawOutput", update.get("content"))
                        if "status" in update:
                            existing["status"] = {"pending": "inProgress", "in_progress": "inProgress"}.get(
                                update["status"], update["status"]
                            )
            elif payload.get("type") == "stream_event":
                ev = payload.get("event") if isinstance(payload.get("event"), dict) else {}
                idx = ev.get("index", 0)
                ev_type = ev.get("type")
                if ev_type == "content_block_start":
                    block = ev.get("content_block") if isinstance(ev.get("content_block"), dict) else {}
                    btype = block.get("type")
                    item_id = block.get("id") or f"claude-block-{idx}"
                    new_item = None
                    if btype == "text":
                        new_item = {"id": item_id, "type": "agentMessage", "text": block.get("text", "")}
                    elif btype in {"thinking", "redacted_thinking"}:
                        new_item = {"id": item_id, "type": "reasoning", "summary": [block.get("thinking", "")]}
                    elif btype == "tool_use":
                        new_item = {
                            "id": item_id,
                            "type": "mcpToolCall",
                            "tool": block.get("name", "tool"),
                            "arguments": block.get("input", {}),
                            "status": "inProgress",
                        }
                    if new_item is not None:
                        if item_id in items_by_id:
                            existing_block = items_by_id[item_id]
                            existing_block.clear()
                            existing_block.update(new_item)
                            claude_blocks[idx] = existing_block
                        else:
                            turn.setdefault("items", []).append(new_item)
                            items_by_id[item_id] = new_item
                            claude_blocks[idx] = new_item
                elif ev_type == "content_block_delta":
                    target = claude_blocks.get(idx)
                    delta = ev.get("delta") if isinstance(ev.get("delta"), dict) else {}
                    if target is not None:
                        if delta.get("type") == "text_delta":
                            target["text"] = (target.get("text") or "") + delta.get("text", "")
                        elif delta.get("type") == "thinking_delta":
                            summary = target.setdefault("summary", [""])
                            summary[0] = (summary[0] or "") + delta.get("thinking", "")
        if not turn.get("id"):
            # Providers also journal configuration and usage before the first prompt.
            if not has_turn_activity and base_turn is None:
                return None
            turn["id"] = "recovered-live-turn"
        return turn

    # ------------------------------------------------------------------
    # Metadata file helpers (meta.json)
    # ------------------------------------------------------------------

    def _read_meta(self, thread_id: str) -> dict | None:
        cached = self._meta_cache.get(thread_id)
        if cached is _NO_META:
            return None
        if isinstance(cached, dict):
            return dict(cached)
        meta_path = self.directory_for(thread_id) / "meta.json"
        try:
            raw = meta_path.read_bytes()
        except FileNotFoundError:
            self._meta_cache[thread_id] = _NO_META
            return None
        except OSError:
            return None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            return None
        if isinstance(parsed, dict):
            self._meta_cache[thread_id] = dict(parsed)
            return parsed
        return None

    def _record_coverage(self, thread_id: str, meta: dict | None) -> None:
        with self._coverage_lock:
            if self._saved_threads is None or self._partial_threads is None:
                return
            if isinstance(meta, dict) and "__raw_history__" in meta and not isinstance(meta["__raw_history__"], dict):
                self._partial_threads.add(thread_id)
                self._saved_threads.discard(thread_id)
            elif isinstance(meta, dict) and meta.get("historyUnavailable"):
                self._partial_threads.add(thread_id)
                self._saved_threads.discard(thread_id)
            else:
                self._saved_threads.add(thread_id)
                self._partial_threads.discard(thread_id)

    def coverage_sets(self, db: sqlite3.Connection, tables: set[str], search: Any) -> tuple[set[str], set[str]]:
        """Return (saved_thread_ids, partial_thread_ids) in O(1) after one-time warm-up."""
        with self._coverage_lock:
            if self._saved_threads is not None and self._partial_threads is not None:
                return set(self._saved_threads), set(self._partial_threads)

        indexed_ids = search.indexed_thread_ids()
        all_saved: set[str] = set()
        all_partial: set[str] = set()

        if "thread_turns" in tables:
            rows = db.execute("SELECT DISTINCT thread_id FROM thread_turns").fetchall()
            missing_transcript_threads = []
            for row in rows:
                tid = row[0]
                meta = self._read_meta(tid) or {}
                if meta.get("historyUnavailable"):
                    all_partial.add(tid)
                else:
                    all_saved.add(tid)
                if tid not in indexed_ids:
                    missing_transcript_threads.append(tid)
            for tid in missing_transcript_threads:
                history = self.read_full_history(tid)
                if isinstance(history, dict):
                    pairs = [
                        (seq, turn)
                        for seq, turn in enumerate(history.get("turns") or [])
                        if isinstance(turn, dict) and turn.get("status") != "inProgress"
                    ]
                    if pairs:
                        search.index_turns_sync(tid, pairs)

        if self.root.exists():
            try:
                for shard in os.scandir(self.root):
                    if not shard.is_dir():
                        continue
                    for entry in os.scandir(shard.path):
                        if not entry.is_dir():
                            continue
                        meta_file = Path(entry.path) / "meta.json"
                        if not meta_file.exists():
                            continue
                        try:
                            parsed = json.loads(meta_file.read_bytes().decode("utf-8"))
                        except (OSError, ValueError, UnicodeError):
                            continue
                        if isinstance(parsed, dict) and "__raw_history__" in parsed:
                            continue
                        tid = parsed.get("id") if isinstance(parsed, dict) else None
                        if isinstance(tid, str) and tid not in all_saved and tid not in all_partial:
                            if parsed.get("historyUnavailable"):
                                all_partial.add(tid)
                            else:
                                all_saved.add(tid)
            except OSError:
                pass

        with self._coverage_lock:
            if self._saved_threads is None or self._partial_threads is None:
                self._saved_threads = set(all_saved)
                self._partial_threads = set(all_partial)
            else:
                self._saved_threads.update(all_saved - self._partial_threads)
                self._partial_threads.update(all_partial - self._saved_threads)
            return set(self._saved_threads), set(self._partial_threads)

    def _write_meta(self, directory: Path, thread_id: str, meta: dict) -> bool:
        cached = self._meta_cache.get(thread_id)
        if isinstance(cached, dict) and cached == meta and (directory / "meta.json").exists():
            self._record_coverage(thread_id, meta)
            return False
        directory.mkdir(parents=True, exist_ok=True)
        temp = directory / f"meta.json.{uuid.uuid4().hex}"
        encoded = json.dumps(meta, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8", "surrogatepass")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, encoded)
        finally:
            os.close(fd)
        os.replace(temp, directory / "meta.json")
        self._meta_cache[thread_id] = copy.deepcopy(meta)
        self._record_coverage(thread_id, meta)
        self.write_generation += 1
        return True

    # ------------------------------------------------------------------
    # Incremental O(1) turn append & SQLite thread_turns upsert
    # ------------------------------------------------------------------

    def _load_index_cache(self, thread_id: str) -> list[tuple]:
        cached = self._index_cache.get(thread_id)
        if cached is not None:
            return cached
        with self.store.reading(), self.store._connection() as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_turns'"
            ).fetchone()
            if not exists:
                return []
            rows = db.execute(
                """SELECT turn_seq, turn_id, status, compact_offset, compact_length,
                          full_offset, full_length
                   FROM thread_turns WHERE thread_id = ? ORDER BY turn_seq ASC""",
                (thread_id,),
            ).fetchall()
        entries = [
            (
                row["turn_id"],
                row["status"],
                row["compact_offset"],
                row["compact_length"],
                row["full_offset"],
                row["full_length"],
                None,
            )
            for row in rows
        ]
        self._index_cache[thread_id] = entries
        return entries

    def prepare_turns_write(self, thread_id: str, history: Any) -> dict[str, Any]:
        """Write new/updated JSONL turn records on disk outside any SQLite transaction.

        Returns a commit plan consumed by ``commit_turns_index(db, plan)``.
        """
        directory = self.directory_for(thread_id)
        if not isinstance(history, dict):
            self._write_meta(directory, thread_id, {"__raw_history__": history})
            return {"thread_id": thread_id, "raw_only": True, "upserts": [], "truncate_from": 0, "changed": True}
        turns = history.get("turns")
        if turns is not None and (
            not isinstance(turns, list)
            or any(
                not isinstance(t, dict) or (t.get("items") is not None and not isinstance(t.get("items"), list))
                for t in turns
            )
        ):
            self._write_meta(directory, thread_id, {"__raw_history__": history})
            return {"thread_id": thread_id, "raw_only": True, "upserts": [], "truncate_from": 0, "changed": True}

        turns = turns or []
        meta = {key: value for key, value in history.items() if key != "turns"}
        meta_changed = self._write_meta(directory, thread_id, meta)

        existing = self._load_index_cache(thread_id)
        existing_count = len(existing)
        small_history = len(turns) <= 8 and existing_count <= 8

        first_changed = min(len(turns), existing_count)
        for seq in range(first_changed):
            turn = turns[seq]
            expected_id = str(turn.get("id") or f"turn-{seq}")
            prev_id, prev_status, _, _, prev_full_off, prev_full_len, prev_stamp = existing[seq]
            if expected_id != prev_id:
                first_changed = seq
                break
            stamp = _quick_turn_stamp(turn)
            if prev_stamp is not None and stamp != prev_stamp:
                first_changed = seq
                break
            if prev_stamp is None:
                candidate = (json.dumps(turn, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                    "utf-8", "surrogatepass"
                )
                if len(candidate) != prev_full_len:
                    first_changed = seq
                    break
                full_path = directory / "turns.full.jsonl"
                try:
                    fd = os.open(full_path, os.O_RDONLY)
                    try:
                        disk_bytes = _pread_exact(fd, prev_full_len, prev_full_off)
                    finally:
                        os.close(fd)
                except OSError:
                    first_changed = seq
                    break
                if disk_bytes != candidate:
                    first_changed = seq
                    break
                existing[seq] = (*existing[seq][:6], stamp)

        if first_changed == len(turns) and len(turns) == existing_count:
            return {
                "thread_id": thread_id,
                "raw_only": False,
                "upserts": [],
                "truncate_from": len(turns),
                "changed": meta_changed,
                "new_cache": existing,
                "finalized_turns": [],
                "all_finalized": all(t.get("status") != "inProgress" for t in turns),
            }

        directory.mkdir(parents=True, exist_ok=True)
        full_chunks: list[bytes] = []
        compact_chunks: list[bytes] = []
        upserts: list[tuple] = []
        new_entries: list[tuple] = list(existing[:first_changed])
        finalized_turns: list[tuple[int, dict]] = []

        full_path = directory / "turns.full.jsonl"
        compact_path = directory / "turns.compact.jsonl"
        full_fd = os.open(full_path, os.O_WRONLY | os.O_CREAT, 0o600)
        compact_fd = os.open(compact_path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            # Separate store instances can prepare records concurrently. Reserve
            # offsets and append both files under one filesystem lock; closing
            # full_fd releases it before any SQLite index transaction.
            fcntl.flock(full_fd, fcntl.LOCK_EX)
            full_cursor = os.lseek(full_fd, 0, os.SEEK_END)
            compact_cursor = os.lseek(compact_fd, 0, os.SEEK_END)
            for seq in range(first_changed, len(turns)):
                turn = turns[seq]
                turn_id = str(turn.get("id") or f"turn-{seq}")
                status = str(turn.get("status") or "completed")
                started_at = seconds(turn.get("startedAt"))
                completed_at = seconds(turn.get("completedAt"))

                full_line = (json.dumps(turn, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                    "utf-8", "surrogatepass"
                )
                full_offset = full_cursor
                full_length = len(full_line)
                full_cursor += full_length
                full_chunks.append(full_line)

                compact_turn = (
                    turn
                    if status == "inProgress"
                    else abridge_turn(turn, full_offset=full_offset, full_length=full_length)
                )
                compact_line = (
                    json.dumps(compact_turn, ensure_ascii=False, separators=(",", ":")) + "\n"
                ).encode("utf-8", "surrogatepass")
                compact_offset = compact_cursor
                compact_length = len(compact_line)
                compact_cursor += compact_length
                compact_chunks.append(compact_line)

                stamp = _quick_turn_stamp(turn)
                upserts.append(
                    (
                        thread_id,
                        seq,
                        turn_id,
                        status,
                        started_at,
                        completed_at,
                        compact_offset,
                        compact_length,
                        full_offset,
                        full_length,
                    )
                )
                new_entries.append(
                    (turn_id, status, compact_offset, compact_length, full_offset, full_length, stamp)
                )
                if status != "inProgress":
                    finalized_turns.append((seq, turn))

            if full_chunks:
                os.write(full_fd, b"".join(full_chunks))
            if compact_chunks:
                os.write(compact_fd, b"".join(compact_chunks))
        finally:
            os.close(full_fd)
            os.close(compact_fd)

        return {
            "thread_id": thread_id,
            "raw_only": False,
            "upserts": upserts,
            "truncate_from": len(turns),
            "changed": True,
            "new_cache": new_entries,
            "finalized_turns": finalized_turns,
            "all_finalized": all(t.get("status") != "inProgress" for t in turns),
        }

    def commit_turns_index(self, db: sqlite3.Connection, plan: dict[str, Any]) -> bool:
        """Apply prepared thread_turns index updates inside an open SQLite write scope."""
        if not self._schema_ready:
            ensure_schema(db)
            self._schema_ready = True
        thread_id = plan["thread_id"]
        try:
            return self._apply_turns_index_rows(db, thread_id, plan)
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            ensure_schema(db)
            self._schema_ready = True
            return self._apply_turns_index_rows(db, thread_id, plan)

    def _apply_turns_index_rows(self, db: sqlite3.Connection, thread_id: str, plan: dict[str, Any]) -> bool:
        if plan["raw_only"]:
            db.execute("DELETE FROM thread_turns WHERE thread_id = ?", (thread_id,))
            return True

        prev_len = len(self._index_cache.get(thread_id, ()))
        truncate_from = plan["truncate_from"]
        if truncate_from < prev_len:
            db.execute("DELETE FROM thread_turns WHERE thread_id = ? AND turn_seq >= ?", (thread_id, truncate_from))
        upserts = plan["upserts"]
        if upserts:
            db.executemany(
                """INSERT OR REPLACE INTO thread_turns (
                       thread_id, turn_seq, turn_id, status, started_at, completed_at,
                       compact_offset, compact_length, full_offset, full_length
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                upserts,
            )
        return bool(plan["changed"])

    def finalize_turns_commit(self, plan: dict[str, Any]) -> None:
        """Update in-memory caches and truncate live.jsonl after SQLite commits."""
        thread_id = plan["thread_id"]
        self.write_generation += 1
        if plan["raw_only"]:
            self._index_cache.pop(thread_id, None)
            self.truncate_live(thread_id)
            return
        self._index_cache[thread_id] = plan["new_cache"]
        self._record_coverage(thread_id, self._read_meta(thread_id))
        if plan.get("upserts") or plan.get("all_finalized"):
            self.truncate_live(thread_id)

    def save_turns(self, thread_id: str, history: Any, *, on_commit_db=None) -> dict[str, Any]:
        """Incrementally append new/updated turns to JSONL and upsert thread_turns."""
        with self._thread_lock(thread_id):
            plan = self.prepare_turns_write(thread_id, history)
            if plan["upserts"] or plan["raw_only"] or on_commit_db is not None or plan["truncate_from"] < len(self._index_cache.get(thread_id, ())):
                with self.store.database_operation("transcript.save", priority="background"), self.store._connection() as db:
                    changed = self.commit_turns_index(db, plan)
                    if on_commit_db is not None:
                        on_commit_db(db, changed)
                self.finalize_turns_commit(plan)
            else:
                changed = bool(plan["changed"])
                if plan.get("all_finalized"):
                    self.truncate_live(thread_id)

        if plan.get("finalized_turns"):
            search = getattr(self.store, "search_store", None)
            if search is not None:
                search.enqueue_turns(thread_id, plan["finalized_turns"])
        written_bytes = sum(row[7] + row[9] for row in plan.get("upserts", ()))
        return {
            "changed": changed,
            "written_turns": len(plan.get("upserts", ())),
            "written_bytes": written_bytes,
        }

    # ------------------------------------------------------------------
    # Lock-free O(1) read paths (os.pread)
    # ------------------------------------------------------------------

    def has_thread(self, thread_id: str, *, db: sqlite3.Connection | None = None) -> bool:
        if self._read_meta(thread_id) is not None:
            return True
        if db is not None:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_turns'"
            ).fetchone()
            return bool(
                exists
                and db.execute("SELECT 1 FROM thread_turns WHERE thread_id = ? LIMIT 1", (thread_id,)).fetchone()
            )
        with self.store.reading(), self.store._connection() as conn:
            return self.has_thread(thread_id, db=conn)

    def read_window(
        self,
        thread_id: str,
        *,
        before_seq: int | None = None,
        limit: int = RECENT_TURNS,
        live_turn: dict | None = None,
        active_turn_id: str | None = None,
    ) -> dict | None:
        """Read a window of turns via one coalesced os.pread() with zero Python locks."""
        with self.store.reading(), self.store._connection() as db:
            if not self._schema_ready:
                has_table = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_turns'"
                ).fetchone()
                if not has_table:
                    rows = []
                    remaining = 0
                else:
                    self._schema_ready = True
            if self._schema_ready:
                try:
                    if before_seq is None:
                        rows = db.execute(
                            """SELECT turn_seq, turn_id, status, compact_offset, compact_length,
                                      full_offset, full_length
                               FROM thread_turns WHERE thread_id = ?
                               ORDER BY turn_seq DESC LIMIT ?""",
                            (thread_id, limit),
                        ).fetchall()
                    else:
                        rows = db.execute(
                            """SELECT turn_seq, turn_id, status, compact_offset, compact_length,
                                      full_offset, full_length
                               FROM thread_turns WHERE thread_id = ? AND turn_seq < ?
                               ORDER BY turn_seq DESC LIMIT ?""",
                            (thread_id, before_seq, limit),
                        ).fetchall()
                    remaining = (
                        db.execute(
                            "SELECT COUNT(*) FROM thread_turns WHERE thread_id = ? AND turn_seq < ?",
                            (thread_id, rows[-1]["turn_seq"]),
                        ).fetchone()[0]
                        if rows and rows[-1]["turn_seq"] > 0
                        else 0
                    )
                except sqlite3.OperationalError as exc:
                    if "no such table" not in str(exc):
                        raise
                    self._schema_ready = False
                    rows = []
                    remaining = 0

        meta = self._read_meta(thread_id)
        if meta is not None and "__raw_history__" in meta:
            raw_val = copy.deepcopy(meta["__raw_history__"])
            return raw_val if isinstance(raw_val, dict) else None
        live_recovered = None
        if before_seq is None and live_turn is None:
            live_recovered = self._has_nonempty_live(thread_id)
        if not rows and meta is None and live_turn is None and not live_recovered:
            return None

        ordered = list(reversed(rows))
        turns: list[dict] = []
        if ordered:
            directory = self.directory_for(thread_id)
            compact_path = directory / "turns.compact.jsonl"
            fd = os.open(compact_path, os.O_RDONLY)
            try:
                slices = _pread_ranges(fd, [(r["compact_offset"], r["compact_length"]) for r in ordered])
            finally:
                os.close(fd)
            for row, raw_line in zip(ordered, slices):
                if active_turn_id and row["turn_id"] == active_turn_id and live_turn is None:
                    full_turn = self._pread_full_row(directory, row["full_offset"], row["full_length"])
                    if full_turn is not None:
                        turns.append(full_turn)
                        continue
                turns.append(json.loads(raw_line.decode("utf-8", "surrogatepass")))

        if before_seq is None and live_turn is not None:
            live_copy = copy.deepcopy(live_turn)
            if turns and turns[-1].get("id") == live_copy.get("id"):
                turns[-1] = live_copy
            else:
                turns.append(live_copy)
                if len(turns) > limit:
                    dropped = len(turns) - limit
                    turns = turns[dropped:]
                    remaining += dropped
        elif before_seq is None and live_recovered:
            before_len = len(turns)
            turns = self._merge_live_tail(thread_id, turns)
            if len(turns) > limit:
                dropped = len(turns) - limit
                turns = turns[dropped:]
                remaining += max(0, dropped - (len(turns) - before_len))

        view: dict[str, Any] = dict(meta) if meta is not None else {"id": thread_id}
        view["turns"] = turns
        view["earlierTurns"] = remaining
        if remaining > 0 and ordered:
            view["beforeSeq"] = ordered[0]["turn_seq"]
        elif remaining > 0:
            view["beforeSeq"] = remaining
        view["view"] = "recent"
        return view

    @staticmethod
    def _pread_full_row(directory: Path, offset: int, length: int) -> dict | None:
        full_path = directory / "turns.full.jsonl"
        try:
            fd = os.open(full_path, os.O_RDONLY)
            try:
                raw = _pread_exact(fd, length, offset)
            finally:
                os.close(fd)
            return json.loads(raw.decode("utf-8", "surrogatepass"))
        except (OSError, ValueError, UnicodeError):
            return None

    def read_turn_full(self, thread_id: str, turn_id: str) -> dict | None:
        """Read one lossless finalized turn via os.pread on turns.full.jsonl."""
        with self.store.reading(), self.store._connection() as db:
            has_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_turns'"
            ).fetchone()
            row = (
                db.execute(
                    """SELECT full_offset, full_length FROM thread_turns
                       WHERE thread_id = ? AND turn_id = ?
                       ORDER BY turn_seq DESC LIMIT 1""",
                    (thread_id, turn_id),
                ).fetchone()
                if has_table
                else None
            )
        if row is None:
            return None
        return self._pread_full_row(self.directory_for(thread_id), row["full_offset"], row["full_length"])

    def read_full_history(self, thread_id: str) -> dict | None:
        """Reconstruct complete lossless history from turns.full.jsonl + meta.json."""
        with self.store.reading(), self.store._connection() as db:
            has_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_turns'"
            ).fetchone()
            rows = (
                db.execute(
                    """SELECT turn_seq, full_offset, full_length FROM thread_turns
                       WHERE thread_id = ? ORDER BY turn_seq ASC""",
                    (thread_id,),
                ).fetchall()
                if has_table
                else []
            )
        meta = self._read_meta(thread_id)
        if meta is not None and "__raw_history__" in meta:
            return copy.deepcopy(meta["__raw_history__"])
        has_live = self._has_nonempty_live(thread_id)
        if not rows and meta is None and not has_live:
            return None

        turns: list[dict] = []
        if rows:
            full_path = self.directory_for(thread_id) / "turns.full.jsonl"
            fd = os.open(full_path, os.O_RDONLY)
            try:
                slices = _pread_ranges(fd, [(r["full_offset"], r["full_length"]) for r in rows])
            finally:
                os.close(fd)
            for raw_line in slices:
                turns.append(json.loads(raw_line.decode("utf-8", "surrogatepass")))

        if has_live:
            turns = self._merge_live_tail(thread_id, turns)

        history: dict[str, Any] = copy.deepcopy(meta) if meta is not None else {"id": thread_id}
        history["turns"] = turns
        return history

    def _has_nonempty_live(self, thread_id: str) -> bool:
        try:
            return (self.directory_for(thread_id) / "live.jsonl").stat().st_size > 0
        except OSError:
            return False

    def _merge_live_tail(self, thread_id: str, turns: list[dict]) -> list[dict]:
        base = turns[-1] if turns and turns[-1].get("status") == "inProgress" else None
        reconstructed = self.reconstruct_live_turn(thread_id, base_turn=base)
        if reconstructed is None:
            return turns
        rec_id = reconstructed.get("id")
        for idx, existing in enumerate(turns):
            if existing.get("id") == rec_id:
                if existing.get("status") not in {"completed", "failed", "interrupted"}:
                    turns[idx] = reconstructed
                return turns
        turns.append(reconstructed)
        return turns

    def turn_status(self, thread_id: str, turn_id: str) -> str | None:
        with self.store.reading(), self.store._connection() as db:
            has_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_turns'"
            ).fetchone()
            if not has_table:
                return None
            row = db.execute(
                """SELECT status FROM thread_turns
                   WHERE thread_id = ? AND turn_id = ?
                   ORDER BY turn_seq DESC LIMIT 1""",
                (thread_id, turn_id),
            ).fetchone()
            return row[0] if row else None

    def forget(self, thread_id: str) -> None:
        self._index_cache.pop(thread_id, None)
        self._meta_cache.pop(thread_id, None)
        with self._coverage_lock:
            if self._saved_threads is not None:
                self._saved_threads.discard(thread_id)
            if self._partial_threads is not None:
                self._partial_threads.discard(thread_id)
        self.write_generation += 1


_LEGACY_PREFIXES = (
    "conversation_search_",
    "conversation_substring_",
    "conversation_context_",
    "conversation_routing_",
    "search_publication_",
    "snapshot_cache_",
)
_LEGACY_EXACT_OBJECTS = frozenset(
    {
        "browser_history_chunks",
        "browser_history_chunk_digests",
        "browser_history",
        "coordinator_projection_payloads",
        "transcript_migration_verification",
        "transcript_generations",
        "transcript_legacy_revisions",
        "transcript_storage_control",
        "storage_retirement_generation",
        "snapshot_cache_revisions",
        "search_publications",
    }
)


def _should_drop_object(name: str) -> bool:
    if name in _LEGACY_EXACT_OBJECTS:
        return True
    return name.startswith(_LEGACY_PREFIXES)


def needs_migration(database_path: str | Path) -> bool:
    path = Path(database_path).expanduser().resolve()
    if not path.exists():
        return False
    legacy_flat = Path(str(path) + ".transcripts")
    if legacy_flat.exists():
        return True
    from contextlib import closing
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2.0)) as db:
            rows = db.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view', 'trigger')"
            ).fetchall()
            return any(_should_drop_object(row[0]) for row in rows)
    except sqlite3.Error:
        return False


def migrate_database(
    database_path: str | Path,
    *,
    store: Any = None,
    compact: bool = True,
) -> dict[str, Any]:
    """Idempotently migrate legacy SQLite transcripts/search into Stage 3 JSONL + search.sqlite3."""
    from contextlib import closing
    import shutil
    from .history_persistence import decode_history
    from .search_store import SEARCH_SCHEMA, SearchStore, search_database_path

    path = Path(database_path).expanduser().resolve()
    if not path.exists():
        return {"migrated": False, "threads_migrated": 0, "turns_migrated": 0, "reclaimed_bytes": 0}

    size_before = path.stat().st_size
    wal_before_path = Path(str(path) + "-wal")
    if wal_before_path.exists():
        size_before += wal_before_path.stat().st_size

    lock_path = path.with_name(path.name + ".migrate.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as guard:
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
        try:
            # Discard cached idle handles on the caller's store before exclusive DDL / VACUUM swap.
            if store is not None:
                idle = getattr(store, "_idle_connections", None)
                if idle is not None:
                    for slot in ("reader", "holder"):
                        holder = getattr(idle, slot, None)
                        if holder is not None and not holder.in_use:
                            holder.discard()

            threads_migrated = 0
            turns_migrated = 0
            items_verified = 0

            with closing(sqlite3.connect(path, timeout=30.0)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA journal_mode = WAL")
                db.execute("PRAGMA synchronous = NORMAL")
                db.execute("PRAGMA busy_timeout = 30000")

                tables = {
                    row[0]
                    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }

                # Capture any Build-105 transitional generation + offset metadata BEFORE ensure_schema
                # replaces an incompatible thread_turns table.
                legacy_generations: list[tuple[str, str, int, int]] = []
                legacy_turn_spans: dict[str, list[tuple[int, int, int]]] = {}
                if "transcript_generations" in tables:
                    gen_cols = {r[1] for r in db.execute("PRAGMA table_info(transcript_generations)")}
                    if {"thread_id", "metadata", "turn_count", "item_count"} <= gen_cols:
                        legacy_generations = [
                            (str(r[0]), str(r[1] or "{}"), int(r[2] or 0), int(r[3] or 0))
                            for r in db.execute(
                                "SELECT thread_id, metadata, turn_count, item_count FROM transcript_generations ORDER BY thread_id"
                            ).fetchall()
                        ]
                if "thread_turns" in tables:
                    tt_cols = {r[1] for r in db.execute("PRAGMA table_info(thread_turns)")}
                    if "digest" in tt_cols and {"thread_id", "turn_seq", "full_offset", "full_length"} <= tt_cols:
                        for r in db.execute(
                            "SELECT thread_id, turn_seq, full_offset, full_length FROM thread_turns ORDER BY thread_id, turn_seq ASC"
                        ):
                            legacy_turn_spans.setdefault(str(r[0]), []).append((int(r[1]), int(r[2]), int(r[3])))

                with db:
                    ensure_schema(db)

                # 1. Recover any legacy flat <db>.transcripts directories if present.
                legacy_flat_dir = Path(str(path) + ".transcripts")

                class _MigrationStoreAdapter:
                    def __init__(self, db_file: Path, conn: sqlite3.Connection):
                        self.database_path = db_file
                        self._conn = conn

                    def reading(self):
                        from contextlib import nullcontext
                        return nullcontext()

                    def _connection(self):
                        from contextlib import nullcontext
                        return nullcontext(self._conn)

                adapter = _MigrationStoreAdapter(path, db)
                tstore = TranscriptStore(adapter)
                try:
                    if "browser_history" in tables:
                        cursor = db.execute("SELECT thread_id FROM browser_history ORDER BY thread_id")
                        thread_ids = [row[0] for row in cursor.fetchall()]
                        for tid in thread_ids:
                            row = db.execute(
                                "SELECT history_json FROM browser_history WHERE thread_id = ?",
                                (tid,),
                            ).fetchone()
                            if row is None:
                                continue
                            history = decode_history(row[0], db=db)
                            with db:
                                plan = tstore.prepare_turns_write(tid, history)
                                tstore.commit_turns_index(db, plan)
                            tstore.finalize_turns_commit(plan)

                            # Verify exact turn count and item count parity.
                            if isinstance(history, dict) and isinstance(history.get("turns"), list) and all(
                                isinstance(t, dict) and (t.get("items") is None or isinstance(t.get("items"), list))
                                for t in history["turns"]
                            ):
                                source_turns = history["turns"]
                                restored = tstore.read_full_history(tid)
                                if restored is None or not isinstance(restored.get("turns"), list):
                                    raise RuntimeError(f"Transcript verification failed for {tid}: missing restored history.")
                                restored_turns = restored["turns"]
                                if len(restored_turns) != len(source_turns):
                                    raise RuntimeError(
                                        f"Turn count mismatch for {tid}: {len(restored_turns)} != {len(source_turns)}"
                                    )
                                for seq, (src_t, dst_t) in enumerate(zip(source_turns, restored_turns)):
                                    src_items = len(src_t.get("items") or [])
                                    dst_items = len(dst_t.get("items") or [])
                                    if src_items != dst_items:
                                        raise RuntimeError(
                                            f"Item count mismatch for {tid} turn {seq}: {dst_items} != {src_items}"
                                        )
                                    items_verified += dst_items
                                turns_migrated += len(restored_turns)
                            threads_migrated += 1

                    if legacy_flat_dir.exists() and legacy_flat_dir.is_dir():
                        if legacy_generations:
                            for tid, metadata_raw, expected_turns, expected_items in legacy_generations:
                                if not tid or tstore.has_thread(tid, db=db):
                                    continue
                                try:
                                    meta_obj = json.loads(metadata_raw)
                                except ValueError:
                                    meta_obj = {}
                                digest_dir = legacy_flat_dir / hashlib.sha256(
                                    tid.encode("utf-8", "surrogatepass")
                                ).hexdigest()
                                full_candidate = digest_dir / "turns.full.jsonl"
                                recovered_turns = []
                                spans = legacy_turn_spans.get(tid) or []
                                if spans and full_candidate.exists():
                                    fd = os.open(full_candidate, os.O_RDONLY)
                                    try:
                                        for _, full_off, full_len in spans:
                                            raw_line = _pread_exact(fd, full_len, full_off)
                                            recovered_turns.append(json.loads(raw_line.decode("utf-8", "surrogatepass")))
                                    finally:
                                        os.close(fd)
                                elif full_candidate.exists():
                                    for line in full_candidate.read_bytes().splitlines():
                                        if line.strip():
                                            recovered_turns.append(json.loads(line.decode("utf-8", "surrogatepass")))
                                if expected_turns and len(recovered_turns) != expected_turns:
                                    raise RuntimeError(
                                        f"Transitional transcript turn count mismatch for {tid}: "
                                        f"{len(recovered_turns)} != {expected_turns}"
                                    )
                                actual_items = sum(len(t.get("items") or []) for t in recovered_turns if isinstance(t, dict))
                                if expected_items and actual_items != expected_items:
                                    raise RuntimeError(
                                        f"Transitional transcript item count mismatch for {tid}: "
                                        f"{actual_items} != {expected_items}"
                                    )
                                recovered_history = {
                                    **(meta_obj if isinstance(meta_obj, dict) else {}),
                                    "id": tid,
                                    "turns": recovered_turns,
                                }
                                with db:
                                    plan = tstore.prepare_turns_write(tid, recovered_history)
                                    tstore.commit_turns_index(db, plan)
                                tstore.finalize_turns_commit(plan)
                                restored = tstore.read_full_history(tid)
                                if restored is None or len(restored.get("turns", [])) != len(recovered_turns):
                                    raise RuntimeError(f"Flat transcript verification failed for {tid}")
                                threads_migrated += 1
                                turns_migrated += len(recovered_turns)
                                items_verified += actual_items
                        else:
                            for entry in sorted(legacy_flat_dir.iterdir()):
                                if not entry.is_dir():
                                    continue
                                full_candidate = (
                                    entry / "turns.full.jsonl"
                                    if (entry / "turns.full.jsonl").exists()
                                    else entry / "turns.jsonl"
                                )
                                meta_candidate = entry / "meta.json"
                                if not full_candidate.exists() and not meta_candidate.exists():
                                    continue
                                meta_obj = {}
                                if meta_candidate.exists():
                                    try:
                                        meta_obj = json.loads(meta_candidate.read_text(encoding="utf-8"))
                                    except (OSError, ValueError):
                                        meta_obj = {}
                                tid = (meta_obj.get("id") if isinstance(meta_obj, dict) else None) or entry.name
                                if not tid or tstore.has_thread(tid, db=db):
                                    continue
                                recovered_turns = []
                                if full_candidate.exists():
                                    for line in full_candidate.read_bytes().splitlines():
                                        if line.strip():
                                            recovered_turns.append(json.loads(line.decode("utf-8", "surrogatepass")))
                                recovered_history = {**(meta_obj if isinstance(meta_obj, dict) else {}), "id": tid, "turns": recovered_turns}
                                with db:
                                    plan = tstore.prepare_turns_write(tid, recovered_history)
                                    tstore.commit_turns_index(db, plan)
                                tstore.finalize_turns_commit(plan)
                                restored = tstore.read_full_history(tid)
                                if restored is None or len(restored.get("turns", [])) != len(recovered_turns):
                                    raise RuntimeError(f"Flat transcript verification failed for {tid}")
                                threads_migrated += 1
                                turns_migrated += len(recovered_turns)

                    # 2. Backfill search.sqlite3 cleanly from turns.compact.jsonl + work_threads.
                    search_path = search_database_path(path)
                    rebuild_search = not search_path.exists()
                    if search_path.exists():
                        try:
                            with closing(sqlite3.connect(search_path.as_uri() + "?mode=ro", uri=True)) as sdb:
                                s_objs = {
                                    r[0]
                                    for r in sdb.execute(
                                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
                                    )
                                }
                                if "search_control" in s_objs or "search_turns" not in s_objs or "search_fts" not in s_objs:
                                    rebuild_search = True
                        except sqlite3.Error:
                            rebuild_search = True

                    if rebuild_search:
                        for suffix in ("", "-wal", "-shm"):
                            Path(str(search_path) + suffix).unlink(missing_ok=True)
                        with closing(sqlite3.connect(search_path, timeout=15.0)) as sdb:
                            sdb.execute("PRAGMA journal_mode = WAL")
                            sdb.execute("PRAGMA synchronous = NORMAL")
                            with sdb:
                                sdb.executescript(SEARCH_SCHEMA)

                    sstore = SearchStore(path)
                    try:
                        sstore._ensure_initialized()
                        indexed = sstore.indexed_thread_ids()
                        with closing(sqlite3.connect(sstore.path, timeout=30.0)) as sdb:
                            sdb.execute("PRAGMA journal_mode = WAL")
                            sdb.execute("PRAGMA synchronous = NORMAL")
                            sdb.execute("PRAGMA busy_timeout = 30000")
                            if "work_threads" in tables:
                                wt_cols = {r[1] for r in db.execute("PRAGMA table_info(work_threads)")}
                                domain_expr = "domain" if "domain" in wt_cols else "''"
                                req_expr = "original_request" if "original_request" in wt_cols else "''"
                                wt_rows = db.execute(
                                    f"SELECT thread_id, title, {domain_expr} AS domain, {req_expr} AS original_request FROM work_threads"
                                ).fetchall()
                                with sdb:
                                    for wrow in wt_rows:
                                        sstore._upsert_metadata_on_connection(
                                            sdb,
                                            wrow["thread_id"],
                                            wrow["title"] or "",
                                            wrow["domain"] or "",
                                            wrow["original_request"] or "",
                                        )
                            distinct_tids = [
                                r[0]
                                for r in db.execute("SELECT DISTINCT thread_id FROM thread_turns ORDER BY thread_id")
                            ]
                            for tid in distinct_tids:
                                if tid in indexed and not rebuild_search:
                                    continue
                                compact_file = tstore.directory_for(tid) / "turns.compact.jsonl"
                                if not compact_file.exists():
                                    continue
                                rows = db.execute(
                                    """SELECT turn_seq, compact_offset, compact_length, status
                                       FROM thread_turns WHERE thread_id = ? ORDER BY turn_seq ASC""",
                                    (tid,),
                                ).fetchall()
                                if not rows:
                                    continue
                                fd = os.open(compact_file, os.O_RDONLY)
                                try:
                                    min_off = min(r["compact_offset"] for r in rows)
                                    max_end = max(r["compact_offset"] + r["compact_length"] for r in rows)
                                    buf = _pread_exact(fd, max_end - min_off, min_off)
                                finally:
                                    os.close(fd)
                                pairs = []
                                for r in rows:
                                    if r["status"] == "inProgress":
                                        continue
                                    rel = r["compact_offset"] - min_off
                                    turn_obj = json.loads(
                                        buf[rel : rel + r["compact_length"]].decode("utf-8", "surrogatepass")
                                    )
                                    pairs.append((r["turn_seq"], turn_obj))
                                if pairs:
                                    with sdb:
                                        sstore._upsert_turns_on_connection(sdb, tid, pairs)
                    finally:
                        sstore.close()

                    # 3. Drop legacy history, projection payload, and 24-table search objects.
                    schema_objects = db.execute(
                        """SELECT type, name FROM sqlite_master
                           WHERE name NOT LIKE 'sqlite_%'
                           ORDER BY CASE type WHEN 'trigger' THEN 0 WHEN 'view' THEN 1 WHEN 'index' THEN 2 ELSE 3 END"""
                    ).fetchall()
                    dropped_objects = []
                    with db:
                        for obj_type, obj_name in schema_objects:
                            if _should_drop_object(obj_name):
                                kind_sql = obj_type.upper()
                                if kind_sql in {"TRIGGER", "VIEW", "INDEX", "TABLE"}:
                                    db.execute(f'DROP {kind_sql} IF EXISTS "{obj_name}"')
                                    dropped_objects.append(obj_name)

                    if legacy_flat_dir.exists():
                        shutil.rmtree(legacy_flat_dir, ignore_errors=True)

                    # Prune completed ephemeral read-only model/limit queries, historical wire receipts,
                    # and embedded blob copies inside aged completed submission receipts.
                    import time as _time
                    now_ts = _time.time()
                    with db:
                        if "app_control_requests" in tables:
                            db.execute(
                                """DELETE FROM app_control_requests
                                   WHERE operation IN ('models', 'provider-limits')
                                     AND status IN ('completed', 'failed', 'cancelled')
                                     AND updated_at < ?""",
                                (now_ts - 3600.0,),
                            )
                        if "coordinator_commands" in tables:
                            if "coordinator_channels" in tables:
                                db.execute(
                                    """DELETE FROM coordinator_commands
                                       WHERE outcome IN ('written', 'rejected')
                                         AND channel NOT IN (
                                             SELECT channel FROM coordinator_channels WHERE closed = 0
                                         )"""
                                )
                            db.execute(
                                """DELETE FROM coordinator_commands
                                   WHERE operation = 'write' AND outcome = 'written'
                                     AND rowid <= COALESCE((SELECT MAX(rowid) - 500 FROM coordinator_commands), 0)"""
                            )
                        if "browser_submission_receipts" in tables:
                            bsr_cols = {r[1] for r in db.execute("PRAGMA table_info(browser_submission_receipts)")}
                            if {"state", "updated_at", "payload_json", "result_json"} <= bsr_cols:
                                db.execute(
                                    """UPDATE browser_submission_receipts
                                       SET payload_json = '{}',
                                           result_json = CASE
                                               WHEN result_json IS NOT NULL AND LENGTH(result_json) > 1024 THEN json_object(
                                                   'turn', CASE
                                                       WHEN json_extract(result_json, '$.turn.id') IS NOT NULL
                                                           THEN json_object('id', json_extract(result_json, '$.turn.id'))
                                                       ELSE NULL
                                                   END,
                                                   'turnId', json_extract(result_json, '$.turnId'),
                                                   'acceptedMessage', json_extract(result_json, '$.acceptedMessage')
                                               )
                                               ELSE result_json
                                           END
                                       WHERE state = 'completed' AND updated_at < ?
                                         AND (LENGTH(payload_json) > 256 OR LENGTH(COALESCE(result_json, '')) > 1024)""",
                                    (now_ts - 86400.0,),
                                )

                    # 4. VACUUM INTO '<db>.compacted' + atomic swap under exclusive lock.
                    compacted_path = Path(str(path) + ".compacted")
                    for suffix in ("", "-wal", "-shm"):
                        Path(str(compacted_path) + suffix).unlink(missing_ok=True)
                    if compact:
                        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        escaped = str(compacted_path).replace("'", "''")
                        db.execute(f"VACUUM INTO '{escaped}'")
                finally:
                    tstore.close()

            if compact:
                compacted_path = Path(str(path) + ".compacted")
                with closing(sqlite3.connect(compacted_path, timeout=15.0)) as cdb:
                    cdb.execute("PRAGMA journal_mode = WAL")
                    cdb.execute("PRAGMA synchronous = NORMAL")
                    cdb.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                for suffix in ("-wal", "-shm"):
                    Path(str(compacted_path) + suffix).unlink(missing_ok=True)
                try:
                    os.chmod(compacted_path, 0o600)
                except OSError:
                    pass
                for suffix in ("-wal", "-shm"):
                    Path(str(path) + suffix).unlink(missing_ok=True)
                os.replace(compacted_path, path)
                for suffix in ("", "-wal", "-shm"):
                    Path(str(compacted_path) + suffix).unlink(missing_ok=True)

            if store is not None:
                idle = getattr(store, "_idle_connections", None)
                if idle is not None:
                    for slot in ("reader", "holder"):
                        holder = getattr(idle, slot, None)
                        if holder is not None and not holder.in_use:
                            holder.discard()
                if getattr(store, "_transcript_store", None) is not None:
                    store._transcript_store._index_cache.clear()
                    store._transcript_store._meta_cache.clear()

            size_after = path.stat().st_size
            wal_after_path = Path(str(path) + "-wal")
            if wal_after_path.exists():
                size_after += wal_after_path.stat().st_size

            return {
                "migrated": True,
                "threads_migrated": threads_migrated,
                "turns_migrated": turns_migrated,
                "items_verified": items_verified,
                "dropped_objects": dropped_objects,
                "bytes_before": size_before,
                "bytes_after": size_after,
                "reclaimed_bytes": max(0, size_before - size_after),
            }
        finally:
            fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
            lock_path.unlink(missing_ok=True)


def migrate_if_needed(store: Any) -> dict[str, Any] | None:
    if getattr(store, "read_only", False):
        return None
    if not needs_migration(store.database_path):
        return None
    return migrate_database(store.database_path, store=store, compact=True)

