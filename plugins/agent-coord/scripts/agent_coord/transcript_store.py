"""Immutable transcript generations and an independently durable event journal.

Files are durable before an index is published. Old generations are retained;
readers need no Python lock and remain valid across replacement or rollback.
No method deletes legacy history. Activation is an explicit, verified operation.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
import shutil
from pathlib import Path
from contextlib import contextmanager, nullcontext

from .history_window import recent_view


def encode(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS transcript_generations (
        thread_id TEXT PRIMARY KEY, generation TEXT NOT NULL, metadata TEXT NOT NULL,
        turn_count INTEGER NOT NULL, item_count INTEGER NOT NULL,
        source_root TEXT, active INTEGER NOT NULL DEFAULT 0, checkpoint INTEGER NOT NULL DEFAULT 0, source_instance TEXT)""")
    if "source_instance" not in {row[1] for row in db.execute("PRAGMA table_info(transcript_generations)")}:
        db.execute("ALTER TABLE transcript_generations ADD COLUMN source_instance TEXT")
    db.execute("""CREATE TABLE IF NOT EXISTS thread_turns (
        thread_id TEXT NOT NULL, turn_seq INTEGER NOT NULL, turn_id TEXT,
        compact_offset INTEGER NOT NULL, compact_length INTEGER NOT NULL,
        full_offset INTEGER NOT NULL, full_length INTEGER NOT NULL, digest TEXT NOT NULL, item_count INTEGER NOT NULL,
        PRIMARY KEY(thread_id, turn_seq)) WITHOUT ROWID""")
    db.execute("CREATE INDEX IF NOT EXISTS thread_turn_identity ON thread_turns(thread_id, turn_id)")
    db.execute("""CREATE TABLE IF NOT EXISTS transcript_migration_verification (
        thread_id TEXT PRIMARY KEY, generation TEXT NOT NULL, source_revision TEXT NOT NULL,
        turn_count INTEGER NOT NULL, item_count INTEGER NOT NULL) WITHOUT ROWID""")
    db.execute("""CREATE TABLE IF NOT EXISTS transcript_legacy_revisions (
        thread_id TEXT PRIMARY KEY, revision INTEGER NOT NULL) WITHOUT ROWID""")
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='browser_history'").fetchone():
        for event in ("INSERT", "UPDATE", "DELETE"):
            identity = "OLD" if event == "DELETE" else "NEW"
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS transcript_legacy_{event.lower()}
                AFTER {event} ON browser_history BEGIN
                INSERT OR IGNORE INTO transcript_legacy_revisions VALUES ({identity}.thread_id, 0);
                UPDATE transcript_legacy_revisions SET revision=revision+1 WHERE thread_id={identity}.thread_id;
                END""")


def legacy_revision(db, thread_id):
    row = db.execute("SELECT revision FROM transcript_legacy_revisions WHERE thread_id=?", (thread_id,)).fetchone()
    return "legacy:" + str(row[0] if row else 0)


class TranscriptStore:
    def __init__(self, store, *, minimum_free_bytes=0, publication_guard=None):
        self.store = store
        self.minimum_free_bytes = minimum_free_bytes
        self.publication_guard = publication_guard or nullcontext
        self.root = Path(str(store.database_path) + ".transcripts")

    def directory(self, thread_id):
        return self.root / hashlib.sha256(thread_id.encode()).hexdigest()

    @contextmanager
    def _locked(self, thread_id, *, filename="writer.lock"):
        directory = self.directory(thread_id)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.mkdir(mode=0o700, exist_ok=True)
        with os.fdopen(os.open(directory / filename, os.O_CREAT | os.O_RDWR, 0o600), "a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield directory

    @staticmethod
    def _sync_directory(directory):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def save(self, thread_id, history, *, source_root=None, activate=False, checkpoint=None, on_publish=None, source_instance=None, expected_source_instance=None):
        """Publish a complete generation. Caller must serialize logical revisions.

        A journal consumer supplies its monotonically increasing checkpoint;
        stale checkpoint saves are rejected. File preparation precedes SQLite.
        """
        turns = history.get("turns") or []
        return self.save_turns(thread_id, dict(enumerate(turns)),
                               metadata={k: v for k, v in history.items() if k != "turns"},
                               turn_count=len(turns), source_root=source_root, activate=activate,
                               checkpoint=checkpoint, on_publish=on_publish, source_instance=source_instance,
                               expected_source_instance=expected_source_instance)

    def save_turns(self, thread_id, turns, *, metadata, turn_count, source_root=None,
                   activate=False, checkpoint=None, on_publish=None, source_instance=None, expected_source_instance=None):
        """Publish only explicitly changed turns (mapping of sequence to turn).

        Use this at finalization; save() is the complete-reconciliation interface.
        Unchanged rows never append bytes. Immutable log spans keep old snapshots
        readable; interrupted appends remain harmless, unreferenced tail records.
        """
        prepared = {}
        for seq, turn in turns.items():
            if not isinstance(seq, int) or not 0 <= seq < turn_count:
                raise ValueError("Invalid turn sequence")
            full = encode(turn) + b"\n"
            short = encode(recent_view({"turns": [turn]})["turns"][0]) + b"\n"
            prepared[seq] = (turn.get("id"), short, full, hashlib.sha256(full).hexdigest(), len(turn.get("items") or []))
        metadata = encode(metadata).decode()
        with self._locked(thread_id) as directory:
            with self.publication_guard(), self.store._connection() as db:
                ensure_schema(db)
            with self.store.reading(), self.store._connection() as db:
                old = db.execute("SELECT generation, active, checkpoint, source_instance, turn_count, item_count, metadata FROM transcript_generations WHERE thread_id=?", (thread_id,)).fetchone()
                previous = {}
                sequences = list(prepared)
                for offset in range(0, len(sequences), 200):
                    batch = sequences[offset:offset + 200]
                    query = "SELECT * FROM thread_turns WHERE thread_id=? AND turn_seq IN (" + ",".join("?" for _ in batch) + ")"
                    previous.update({r[1]: tuple(r) for r in db.execute(query, [thread_id, *batch])})
                removed_items = db.execute("SELECT COALESCE(sum(item_count),0) FROM thread_turns WHERE thread_id=? AND turn_seq>=?",
                                           (thread_id, turn_count)).fetchone()[0] if old and turn_count < old[4] else 0
            previous_instance = old[3] if old else None
            if source_instance is None:
                source_instance = previous_instance
            changed_instance = source_instance != previous_instance
            if changed_instance and expected_source_instance != previous_instance:
                raise ValueError("Transcript source instance fence changed")
            if checkpoint is None:
                checkpoint = old[2] if old and not changed_instance else 0
            if old and not changed_instance and checkpoint < old[2]:
                raise ValueError("Transcript checkpoint would move backwards")
            changed = {seq: value for seq, value in prepared.items()
                       if seq not in previous or previous[seq][7] != value[3]}
            old_count = old[4] if old else 0
            if any(seq not in changed for seq in range(old_count, turn_count)):
                raise ValueError("Transcript publication has missing turn sequences")
            item_count = (old[5] if old else 0) - removed_items
            item_count += sum(value[4] - (previous[seq][8] if seq in previous else 0)
                              for seq, value in changed.items())
            required_bytes = sum(len(value[1]) + len(value[2]) for value in changed.values())
            if shutil.disk_usage(directory).free < required_bytes + self.minimum_free_bytes:
                raise OSError("Insufficient free space for durable transcript publication")
            offsets = []
            with os.fdopen(os.open(directory / "turns.compact.jsonl", os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600), "ab") as compact:
                with os.fdopen(os.open(directory / "turns.full.jsonl", os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600), "ab") as complete:
                    a, b = compact.tell(), complete.tell()
                    for seq, (identity, short, full, digest, changed_item_count) in sorted(changed.items()):
                        offsets.append((thread_id, seq, identity, a, len(short), b, len(full), digest, changed_item_count))
                        compact.write(short)
                        complete.write(full)
                        a += len(short)
                        b += len(full)
                    for output in (compact, complete):
                        output.flush()
                        os.fsync(output.fileno())
            self._sync_directory(directory)
            self._sync_directory(self.root)
            self._sync_directory(self.root.parent)
            generation = (old[0] if old and not changed and turn_count == old[4] and metadata == old[6]
                          else uuid.uuid4().hex)
            with self.publication_guard(), self.store.database_operation("transcript.publish", priority="background"), self.store._connection() as db:
                if source_root is not None:
                    row = db.execute("SELECT history_json FROM browser_history WHERE thread_id=?", (thread_id,)).fetchone()
                    if not row or row[0] != source_root:
                        raise ValueError("Legacy history changed during transcript preparation")
                    source_root = legacy_revision(db, thread_id)
                db.execute("DELETE FROM thread_turns WHERE thread_id=? AND turn_seq>=?", (thread_id, turn_count))
                db.executemany("""INSERT INTO thread_turns VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(thread_id,turn_seq) DO UPDATE SET turn_id=excluded.turn_id,
                    compact_offset=excluded.compact_offset, compact_length=excluded.compact_length,
                    full_offset=excluded.full_offset, full_length=excluded.full_length, digest=excluded.digest, item_count=excluded.item_count""", offsets)
                db.execute("""INSERT INTO transcript_generations VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(thread_id) DO UPDATE SET generation=excluded.generation,
                    metadata=excluded.metadata, turn_count=excluded.turn_count,
                    item_count=excluded.item_count, source_root=excluded.source_root,
                    active=excluded.active, checkpoint=excluded.checkpoint, source_instance=excluded.source_instance
                    WHERE generation != excluded.generation OR source_root IS NOT excluded.source_root
                       OR active != excluded.active OR checkpoint != excluded.checkpoint
                       OR source_instance IS NOT excluded.source_instance""",
                    (thread_id, generation, metadata, turn_count,
                     item_count,
                     source_root, int(activate or bool(old and old[1])), checkpoint, source_instance))
                if on_publish is not None:
                    on_publish(db)
        return generation

    def _read(self, thread_id, *, before_seq=None, limit=None, turn_id=None, full=False, require_active=True, with_checkpoint=False, target_turn=None, serialized=False):
        with self.store.reading(), self.store._connection() as db:
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='transcript_generations'").fetchone():
                return None
            row = db.execute("SELECT * FROM transcript_generations WHERE thread_id=?", (thread_id,)).fetchone()
            if row is None or (require_active and not row[6]):
                return None
            if require_active and row[5] is not None:
                if legacy_revision(db, thread_id) != row[5]:
                    return None  # An older build wrote the retained legacy generation.
            if target_turn is not None:
                target = db.execute("SELECT turn_seq FROM thread_turns WHERE thread_id=? AND turn_id=?",
                                    (thread_id, target_turn)).fetchone()
                if target is None:
                    return None
                before_seq = target[0] + 1
            query = "SELECT * FROM thread_turns WHERE thread_id=?"
            args = [thread_id]
            if before_seq is not None:
                query += " AND turn_seq < ?"
                args.append(before_seq)
            if turn_id is not None:
                query += " AND turn_id=?"
                args.append(turn_id)
            query += " ORDER BY turn_seq DESC"
            if limit is not None:
                query += " LIMIT ?"
                args.append(limit)
            entries = list(reversed(db.execute(query, args).fetchall()))
        result = json.loads(row[2])
        result["turns"] = []
        raw_turns = []
        if entries:
            column = 5 if full else 3
            path = self.directory(thread_id) / ("turns.full.jsonl" if full else "turns.compact.jsonl")
            fd = os.open(path, os.O_RDONLY)
            try:
                # Consecutive append spans share one pread; amended turns can
                # fragment a page but never cause unrelated history to be read.
                groups = []
                for entry in entries:
                    if groups and groups[-1][-1][column] + groups[-1][-1][column + 1] == entry[column]:
                        groups[-1].append(entry)
                    else:
                        groups.append([entry])
                for group in groups:
                    start = group[0][column]
                    end = group[-1][column] + group[-1][column + 1]
                    data = os.pread(fd, end - start, start)
                    if len(data) != end - start:
                        raise ValueError("Truncated transcript generation")
                    for entry in group:
                        raw = data[entry[column]-start:entry[column]-start+entry[column+1]]
                        if full and hashlib.sha256(raw).hexdigest() != entry[7]:
                            raise ValueError("Transcript turn digest mismatch")
                        if serialized:
                            raw_turns.append(raw[:-1])
                        else:
                            result["turns"].append(json.loads(raw))
            finally:
                os.close(fd)
        if not full:
            result.update(view="recent", earlierTurns=entries[0][1] if entries else 0,
                          beforeSeq=entries[0][1] if entries else None, totalTurns=row[3])
        if serialized:
            header = encode({key: value for key, value in result.items() if key != "turns"})[:-1]
            body = header + (b"," if header != b"{" else b"") + b'"turns":[' + b",".join(raw_turns) + b"]}"
            if serialized == "projection":
                # Receipt projection needs visible identities, not item bodies.
                # Both are from the same publication and SQLite read snapshot.
                result["turns"] = [{"id": entry[2]} for entry in entries]
                return result, body
            return body
        if with_checkpoint:
            return {"generation": row[1], "checkpoint": row[7], "source_instance": row[8] if len(row) > 8 else None, "history": result}
        return result

    def read_checkpoint(self, thread_id):
        """Read a coherent transcript and cursor for projection recovery."""
        return self._read(thread_id, full=True, with_checkpoint=True)

    def read_window(self, thread_id, *, before_seq=None, limit=40, turn_id=None):
        if not 1 <= limit <= 200 or (before_seq is not None and before_seq < 0):
            raise ValueError("Invalid transcript window")
        return self._read(thread_id, before_seq=before_seq, limit=limit, target_turn=turn_id)

    def read_window_json(self, thread_id, *, before_seq=None, limit=40, turn_id=None):
        """Return pre-serialized compact JSON for a response without item parsing."""
        if not 1 <= limit <= 200 or (before_seq is not None and before_seq < 0):
            raise ValueError("Invalid transcript window")
        return self._read(thread_id, before_seq=before_seq, limit=limit, target_turn=turn_id, serialized=True)

    def read_window_projection(self, thread_id):
        """Return receipt identities and serialized window from one publication."""
        return self._read(thread_id, limit=40, serialized="projection")

    def read_full(self, thread_id, *, require_active=True):
        return self._read(thread_id, full=True, require_active=require_active)

    def read_full_turn(self, thread_id, turn_id):
        result = self._read(thread_id, turn_id=turn_id, full=True)
        return next(iter(result["turns"]), None) if result is not None else None

    def append_live(self, thread_id, frame):
        """Durably append a transport event without touching SQLite.

        Frames must carry the transport sequence. Consumers discard sequences
        through their published checkpoint, making interrupted replay idempotent.
        """
        with self._locked(thread_id, filename="journal.lock") as directory:
            path = directory / "live.jsonl"
            with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), "r+b") as output:
                end = output.seek(0, os.SEEK_END)
                if end:
                    output.seek(end - 1)
                    if output.read(1) != b"\n":
                        position = end
                        while position:
                            start = max(0, position - 65536)
                            output.seek(start)
                            block = output.read(position - start)
                            newline = block.rfind(b"\n")
                            if newline >= 0:
                                output.truncate(start + newline + 1)
                                break
                            position = start
                        else:
                            output.truncate(0)
                output.seek(0, os.SEEK_END)
                output.write(encode(frame) + b"\n")
                output.flush()
                os.fsync(output.fileno())
            self._sync_directory(directory)
            self._sync_directory(self.root)
            self._sync_directory(self.root.parent)

    def replay_live(self, thread_id, *, after_seq=0):
        path = self.directory(thread_id) / "live.jsonl"
        if not path.exists():
            return
        with path.open("rb") as source:
            for line in source:
                if not line.endswith(b"\n"):
                    break
                frame = json.loads(line)
                if frame["sequence"] > after_seq:
                    yield frame

    def deactivate(self):
        """Revert to retained legacy roots only while they remain equivalent."""
        with self.publication_guard(), self.store._connection() as db:
            rows = db.execute("SELECT thread_id, source_root FROM transcript_generations WHERE active=1").fetchall()
            for identity, revision in rows:
                if revision is None or legacy_revision(db, identity) != revision:
                    raise ValueError("Rollback requires an equivalent retained legacy history")
            db.execute("UPDATE transcript_generations SET active=0")

    def migrate(self, *, activate=False, on_progress=None, force_verify=False, thread_ids=None):
        """Copy, verify exact content, then optionally activate each stable root."""
        from .history_persistence import decode_history
        with self.store.reading(), self.store._connection() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='browser_history'").fetchone()
            identities = (list(thread_ids) if thread_ids is not None else
                          [r[0] for r in db.execute("SELECT thread_id FROM browser_history ORDER BY thread_id")] if exists else [])
        reports = []
        with self.publication_guard(), self.store._connection() as db:
            ensure_schema(db)
        for identity in identities:
            with self.store.reading(), self.store._connection() as db:
                db.execute("BEGIN")
                revision = legacy_revision(db, identity)
                verified = None if force_verify else db.execute("""SELECT v.turn_count,v.item_count
                    FROM transcript_migration_verification v JOIN transcript_generations g USING(thread_id)
                    WHERE v.thread_id=? AND v.generation=g.generation AND v.source_revision=? AND g.source_root=?""",
                    (identity, revision, revision)).fetchone()
                if verified is None:
                    source_row = db.execute("SELECT history_json FROM browser_history WHERE thread_id=?", (identity,)).fetchone()
                    if source_row is None:
                        continue
                    source = source_row[0]
                    history = decode_history(source, db=db)
            if verified is None:
                generation = self.save(identity, history, source_root=source)
                if self.read_full(identity, require_active=False) != history:
                    raise ValueError("Transcript migration content mismatch: " + identity)
                verified = (len(history.get("turns") or []), sum(len(t.get("items") or []) for t in history.get("turns") or []))
                with self.publication_guard(), self.store._connection() as db:
                    db.execute("""INSERT INTO transcript_migration_verification VALUES (?,?,?,?,?)
                        ON CONFLICT(thread_id) DO UPDATE SET generation=excluded.generation,
                        source_revision=excluded.source_revision, turn_count=excluded.turn_count,item_count=excluded.item_count""",
                        (identity, generation, revision, *verified))
            if activate:
                with self.publication_guard(), self.store._connection() as db:
                    if legacy_revision(db, identity) != revision:
                        raise ValueError("Legacy history changed before activation")
                    changed = db.execute("""UPDATE transcript_generations SET active=1 WHERE thread_id=? AND source_root=?
                        AND generation=(SELECT generation FROM transcript_migration_verification WHERE thread_id=?)""",
                        (identity, revision, identity)).rowcount
                    if changed != 1:
                        raise ValueError("Transcript changed before verified activation")
            reports.append({"thread_id": identity, "turns": verified[0], "items": verified[1]})
            if on_progress is not None:
                on_progress(reports[-1])
        return reports
