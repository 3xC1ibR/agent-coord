"""Private, durable composer intake for one local coordinator transition.

The caller authenticates the client and verifies workspace/thread access before
acceptance or disclosure. Only ordinary composer submissions are eligible;
dispatch-enabled conversations have a different receipt authority. Drainers
run only against the single authorized, committed backend for this database. No coordination database writes occur here.
"""

from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time

from .image_inputs import MAX_MESSAGE_BODY_BYTES, message_images
from .store import CoordinationError


class IntakeFull(CoordinationError):
    """The request was not accepted. Retain the client's draft."""


class TransportNotAttempted(CoordinationError):
    """Local ownership or admission failed before issuing a transport request."""


class ComposerNotReserved(Exception):
    """An authenticated receiving coordinator explicitly declined before receipt reservation."""


class OrdinaryComposerReceiver:
    """One authenticated, receipt-first ordinary composer on one canonical database.

    The supplied transport must bind the receiving coordinator's instance, payload,
    database identity and ordinary-only protocol before any provider operation.
    A generic browser messages route does not satisfy this contract.
    """

    def __init__(self, database, instance, payload, forward):
        self.database = Path(database).resolve(strict=True)
        _identifier(instance, "Coordinator instance")
        if not isinstance(payload, str) or not payload:
            raise ValueError("A verified terminal payload is required.")
        self.instance = instance
        self.payload = payload
        file = self.database.stat()
        self.device, self.inode = file.st_dev, file.st_ino
        self.authority = json.dumps([str(self.database), self.device, self.inode, instance, payload],
                                    separators=(",", ":"))
        self.forward = forward
        self.readers = threading.local()

    def current_database(self):
        file = self.database.stat()
        return (file.st_dev, file.st_ino) == (self.device, self.inode)

    @contextmanager
    def reading_receipts(self):
        # Each SELECT uses a new SQLite snapshot. Do not hold a read transaction
        # while waiting for the provider, even when forwarding consecutive inputs.
        if not self.current_database():
            raise CoordinationError("The receiving database changed.")
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as db:
            if not self.current_database():
                raise CoordinationError("The receiving database changed.")
            self.readers.connection = db
            try:
                yield
            finally:
                del self.readers.connection

    def lookup(self, thread_id, body, *, include_result=False):
        if not self.current_database():
            raise CoordinationError("The receiving database changed.")
        db = getattr(self.readers, "connection", None)
        receipt = (_read_receipt(db, thread_id, body, include_result=include_result) if db is not None else
                   canonical_receipt(self.database, thread_id, body, include_result=include_result))
        if not self.current_database():
            raise CoordinationError("The receiving database changed.")
        return receipt

    def completed_result(self, thread_id, body):
        receipt = self.lookup(thread_id, body, include_result=True)
        if not receipt or _receipt_state(receipt, body)[0] != "committed":
            raise RuntimeError("The receiving coordinator has not confirmed this submission.")
        return receipt["result"]


def canonical_receipt(database, thread_id, body, *, include_result=False):
    """Read the ordinary composer's authoritative receipt without creating a DB."""
    uri = Path(database).resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        return _read_receipt(db, thread_id, body, include_result=include_result)


def _read_receipt(db, thread_id, body, *, include_result=False):
    result_column = ", result_json" if include_result else ""
    row = db.execute(f"""SELECT state, payload_hash{result_column} FROM browser_submission_receipts
                         WHERE thread_id=? AND request_id=?""",
                     (thread_id, body["requestId"])).fetchone()
    if row is None:
        return None
    result = dict(state=row[0], payload_hash=row[1])
    if include_result and row[0] == "completed":
        result["result"] = json.loads(row[2])
    return result


def _identifier(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise CoordinationError(f"{label} must contain 1–200 characters.")


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _receipt_state(receipt, body):
    # The ordinary submission receipt binds this representation. The separate
    # intake digest also binds the expected turn and the original routing data.
    semantic = {k: v for k, v in body.items()
                if k not in {"requestId", "windowId", "activity", "expectedTurnId"}}
    expected = hashlib.sha256(_encoded({"body": semantic, "queued": False})).hexdigest()
    if receipt["payload_hash"] != expected:
        return "failed", "request_id_conflict"
    states = {"completed": ("committed", None), "rejected": ("failed", "backend_rejected"),
              "accepted": ("uncertain", "confirmation_pending"),
              "uncertain": ("uncertain", "confirmation_pending")}
    try:
        return states[receipt["state"]]
    except KeyError:
        raise CoordinationError("Unrecognized canonical submission state.") from None


class RuntimeMessageIntake:
    """Retain receipts for the entire transition, including after delivery."""

    def __init__(self, directory, *, max_records=4096, max_bytes=128 * 1024 * 1024):
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_stat = directory.lstat()
        if (not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != os.getuid()
                or directory_stat.st_mode & 0o077):
            raise PermissionError("Message intake requires a private directory owned by this user.")
        self.database = directory / "message-intake.sqlite3"
        fd = os.open(self.database, os.O_RDWR | os.O_NOFOLLOW | os.O_CREAT, 0o600)
        try:
            file_stat = os.fstat(fd)
            if (not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != os.getuid()
                    or file_stat.st_mode & 0o077):
                raise PermissionError("Message intake requires a private database owned by this user.")
        finally:
            os.close(fd)
        self.workers = threading.local()
        self.max_records = max_records
        self.max_bytes = max_bytes
        with self._connection() as db:
            # Keep full power-loss durability and allow receipt reads to proceed
            # while a later acceptance, dispatch intent or completion commits.
            if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise CoordinationError("The message intake requires a durable WAL journal.")
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS intake (
                    sequence INTEGER PRIMARY KEY, thread_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    digest BLOB NOT NULL, display TEXT NOT NULL, created_at REAL NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('queued','uncertain','committed','failed')),
                    reason TEXT, body BLOB NOT NULL, UNIQUE(thread_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS pending_intake ON intake(thread_id, sequence)
                    WHERE state IN ('queued','uncertain');
                CREATE TABLE IF NOT EXISTS intake_changes (
                    revision INTEGER PRIMARY KEY AUTOINCREMENT, sequence INTEGER UNIQUE NOT NULL, thread_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS thread_changes ON intake_changes(thread_id,revision);
                CREATE TRIGGER IF NOT EXISTS intake_inserted AFTER INSERT ON intake BEGIN
                    INSERT INTO intake_changes(sequence,thread_id) VALUES(NEW.sequence,NEW.thread_id);
                END;
                CREATE TRIGGER IF NOT EXISTS intake_updated AFTER UPDATE OF state,reason ON intake
                    WHEN OLD.state != NEW.state OR OLD.reason IS NOT NEW.reason BEGIN
                    DELETE FROM intake_changes WHERE sequence=NEW.sequence;
                    INSERT INTO intake_changes(sequence,thread_id) VALUES(NEW.sequence,NEW.thread_id);
                END;
                CREATE TABLE IF NOT EXISTS ordinary_transfers (
                    sequence INTEGER PRIMARY KEY REFERENCES intake(sequence),
                    authority TEXT NOT NULL, replay_blocked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS serving_inputs (
                    sequence INTEGER PRIMARY KEY REFERENCES intake(sequence), authority TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capacity (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), records INTEGER NOT NULL, bytes INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO capacity VALUES (1,0,0);
                COMMIT;
            """)

    @contextmanager
    def _connection(self):
        existing = getattr(self.workers, "connection", None)
        if existing is not None:
            with existing:
                yield existing
            return
        with closing(sqlite3.connect(self.database, timeout=1)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=EXTRA")
            db.execute("PRAGMA fullfsync=ON")
            with db:
                yield db

    @staticmethod
    def _record(row):
        display = json.loads(row["display"])
        return {"sequence": row["sequence"], "threadId": row["thread_id"], "requestId": row["request_id"],
                "state": row["state"], "reason": row["reason"], "createdAt": row["created_at"],
                **display}

    def accept(self, thread_id, body, *, serving_authority=None):
        """Commit intake before acknowledgment. An exact duplicate uses its first receipt."""
        _identifier(thread_id, "Thread ID")
        if not isinstance(body, dict):
            raise CoordinationError("A composer request must be an object.")
        if body.get("review") is not None:
            raise CoordinationError("Reviewed requests require the review receipt authority.")
        _identifier(body.get("requestId"), "Submission request ID")
        if "expectedTurnId" in body:
            _identifier(body["expectedTurnId"], "Expected turn ID")
        encoded = _encoded(body)
        if len(encoded) > MAX_MESSAGE_BODY_BYTES:
            raise IntakeFull("The submission exceeds the message size limit.")
        message_images(body)
        key = (thread_id, body["requestId"])
        digest = hashlib.sha256(encoded).digest()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM intake WHERE thread_id=? AND request_id=?", key).fetchone()
            if row is not None:
                if row["digest"] != digest:
                    raise CoordinationError("Submission request ID was already used for different input.")
                return self._record(row)
            target = body.get("expectedTurnId")
            if target and db.execute("""SELECT 1 FROM intake WHERE thread_id=?
                    AND state IN ('queued','uncertain')
                    AND json_extract(display,'$.expectedTurnId') IS NOT ? LIMIT 1""",
                                     (thread_id, target)).fetchone():
                raise CoordinationError("An earlier message can change the active turn. Your steering was not accepted.")
            # Assign a foreground handoff only when it is the next input. This
            # check and the durable insertion share the same writer transaction.
            if serving_authority and db.execute("""SELECT 1 FROM intake WHERE thread_id=?
                    AND state IN ('queued','uncertain') LIMIT 1""", (thread_id,)).fetchone():
                return None
            reserved = db.execute("""UPDATE capacity SET records=records+1, bytes=bytes+?
                                     WHERE singleton=1 AND records<? AND bytes+?<=?""",
                                  (len(encoded), self.max_records, len(encoded), self.max_bytes))
            if not reserved.rowcount:
                raise IntakeFull("Message intake is full. This submission was not accepted.")
            display = json.dumps({"message": body.get("message", ""), "imageCount": len(body.get("images", [])),
                                  "expectedTurnId": body.get("expectedTurnId")})
            cursor = db.execute("""INSERT INTO intake(thread_id,request_id,digest,body,display,created_at,state)
                                   VALUES (?,?,?,?,?,?,'queued')""", (*key, digest, encoded, display, time.time()))
            if serving_authority:
                db.execute("INSERT INTO serving_inputs VALUES (?,?)", (cursor.lastrowid, serving_authority))
            row = db.execute("SELECT * FROM intake WHERE sequence=?", (cursor.lastrowid,)).fetchone()
        return self._record(row)

    def serving_authority(self, sequence):
        with self._connection() as db:
            row = db.execute("SELECT authority FROM serving_inputs WHERE sequence=?", (sequence,)).fetchone()
        return row[0] if row else None

    def get(self, thread_id, request_id):
        with self._connection() as db:
            row = db.execute("SELECT * FROM intake WHERE thread_id=? AND request_id=?",
                             (thread_id, request_id)).fetchone()
        return self._record(row) if row else None

    def existing(self, thread_id, body, authorize):
        key = body.get("requestId") if isinstance(body, dict) else None
        if not isinstance(key, str) or not key:
            return None
        with self._connection() as db:
            row = db.execute("""SELECT sequence,thread_id,request_id,state,reason,created_at,display,digest
                FROM intake WHERE thread_id=? AND request_id=?""", (thread_id,key)).fetchone()
        if row is None:
            return None
        authorize(thread_id)
        if row["digest"] != hashlib.sha256(_encoded(body)).digest():
            raise CoordinationError("Submission request ID was already used for different input.")
        return self._record(row)

    def outstanding(self, thread_id):
        """Pending messages for this authorized conversation, in acceptance order."""
        with self._connection() as db:
            rows = db.execute("""SELECT * FROM intake WHERE thread_id=?
                                 AND state IN ('queued','uncertain') ORDER BY sequence""", (thread_id,)).fetchall()
        return [self._record(row) for row in rows]

    def thread_changes(self, thread_id, after):
        """Send only changed receipts, without reading attachment blobs or resending old inputs."""
        with self._connection() as db:
            rows = db.execute("""SELECT c.revision,i.sequence,i.thread_id,i.request_id,i.state,i.reason,i.created_at,i.display
                FROM intake_changes c JOIN intake i ON i.sequence=c.sequence
                WHERE c.thread_id=? AND c.revision>? ORDER BY c.revision LIMIT 16""", (thread_id, after)).fetchall()
        return (rows[-1]["revision"] if rows else after), [self._record(row) for row in rows]

    def visible(self, thread_id):
        with self._connection() as db:
            rows = db.execute("""SELECT sequence,thread_id,request_id,state,reason,created_at,display FROM intake
                WHERE thread_id=? AND state!='committed' ORDER BY sequence""", (thread_id,)).fetchall()
        return [self._record(row) for row in rows]

    def pending_threads(self):
        """Internal scheduler enumeration. Do not expose it through a browser response."""
        with self._connection() as db:
            return [row[0] for row in db.execute("""SELECT thread_id FROM intake
                        WHERE state IN ('queued','uncertain') GROUP BY thread_id ORDER BY min(sequence)""")]

    def legacy_attempts(self):
        """Legacy submissions already dispatched can only be observed on their original authority."""
        with self._connection() as db:
            return [(row["thread_id"], json.loads(row["body"])) for row in db.execute(
                """SELECT i.thread_id,i.body FROM intake i LEFT JOIN ordinary_transfers t USING(sequence)
                   WHERE i.state='uncertain' AND t.sequence IS NULL ORDER BY i.sequence""")]

    def reconcile_legacy(self, database, *, lookup=None):
        unresolved = False
        for thread_id, body in self.legacy_attempts():
            receipt = lookup(thread_id, body) if lookup else canonical_receipt(database, thread_id, body)
            if not receipt:
                unresolved = True
                continue
            state, reason = _receipt_state(receipt, body)
            with self._connection() as db:
                db.execute("""UPDATE intake SET state=?,reason=? WHERE thread_id=? AND request_id=? AND state='uncertain'
                              AND (state!=? OR reason IS NOT ?)""",
                           (state, reason, thread_id, body["requestId"], state, reason))
            unresolved |= state == "uncertain"
        return not unresolved

    def drain_one(self, thread_id, lookup, forward, *, preflight=None):
        """A generic receiver gets a single transport attempt, including across restarts."""
        return self._drain(thread_id, lookup, forward, receiver=None, preflight=preflight)

    def drain_to(self, thread_id, receiver, *, preflight=None):
        """Recover delivery only through the pinned receiver's ordinary composer contract."""
        if not isinstance(receiver, OrdinaryComposerReceiver):
            raise TypeError("An authenticated ordinary composer receiver is required.")
        return self._drain(thread_id, receiver.lookup, receiver.forward, receiver, preflight)

    def drain_batch(self, thread_id, receiver, *, preflight, completed, stopped, limit=16):
        """Forward bounded FIFO work on one thread, stopping at the first unknown outcome."""
        if not isinstance(receiver, OrdinaryComposerReceiver):
            raise TypeError("An authenticated ordinary composer receiver is required.")
        with self._connection() as local, receiver.reading_receipts():
            self.workers.connection = local
            try:
                return self._drain(thread_id, receiver.lookup, receiver.forward, receiver, preflight,
                                   completed=completed, stopped=stopped, limit=limit)
            finally:
                del self.workers.connection

    def _drain(self, thread_id, lookup, forward, receiver, preflight, *, completed=None, stopped=None, limit=1):
        # The kernel releases this lock on process exit. No database transaction
        # spans a transport call; other threads can accept, stream and drain.
        name = "drain-" + hashlib.sha256(thread_id.encode()).hexdigest() + ".lock"
        fd = os.open(self.database.parent / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                with self._connection() as db:
                    row = db.execute("""SELECT sequence,thread_id,request_id,state,reason,created_at,display
                        FROM intake WHERE thread_id=? AND state IN ('queued','uncertain')
                        ORDER BY sequence LIMIT 1""", (thread_id,)).fetchone()
                return self._record(row) if row else None
            result = None
            for _ in range(limit):
                if stopped and stopped():
                    break
                result = self._drain_locked(thread_id, lookup, forward, receiver, preflight)
                if not result or result["state"] not in {"committed", "failed"}:
                    break
                if completed:
                    completed()
            return result
        finally:
            os.close(fd)

    def _drain_locked(self, thread_id, lookup, forward, receiver, preflight):
        with self._connection() as db:
            row = db.execute("""SELECT i.*, t.authority, t.replay_blocked FROM intake i
                LEFT JOIN ordinary_transfers t USING(sequence) WHERE thread_id=?
                AND state IN ('queued','uncertain') ORDER BY sequence LIMIT 1""", (thread_id,)).fetchone()
        if row is None:
            return None
        sequence = row["sequence"]
        key = thread_id, row["request_id"]
        body = json.loads(row["body"])
        if row["authority"] and receiver is None:
            return self._record(row)
        if receiver and ((row["authority"] and row["authority"] != receiver.authority)
                         or not receiver.current_database()):
            if row["authority"]:
                with self._connection() as db:
                    db.execute("UPDATE ordinary_transfers SET replay_blocked=1 WHERE sequence=?", (sequence,))
                    db.execute("UPDATE intake SET reason='receiver_changed' WHERE sequence=?", (sequence,))
            return self.get(*key)

        if preflight:
            # Check ownership before consulting its receipt table or claiming
            # the request. Temporary source failures must preserve queued work.
            preflight(thread_id, body)
        receipt = lookup(thread_id, body)
        fresh = row["state"] == "queued"
        retry = receiver and row["authority"] == receiver.authority and not row["replay_blocked"]
        if receipt is None and (fresh or retry):
            if fresh:
                with self._connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    if receiver:
                        db.execute("INSERT INTO ordinary_transfers(sequence,authority) VALUES (?,?)",
                                   (sequence, receiver.authority))
                    db.execute("""UPDATE intake SET state='uncertain',reason='confirmation_pending'
                                  WHERE sequence=?""", (sequence,))
            definitive_rejection = False
            try:
                forward(thread_id, body)
            except TransportNotAttempted:
                # Only an explicitly checked transport may attest that no
                # request crossed the boundary. Preserve any earlier attempt.
                if preflight and fresh:
                    with self._connection() as db:
                        db.execute("DELETE FROM ordinary_transfers WHERE sequence=?", (sequence,))
                        db.execute("UPDATE intake SET state='queued',reason=NULL WHERE sequence=?", (sequence,))
                return self.get(*key)
            except ComposerNotReserved:
                # Only the contracted receiver may attest to pre-reservation refusal.
                definitive_rejection = receiver is not None
            except Exception:
                # Receipt reconciliation is the only proof of the provider effect.
                pass
            receipt = lookup(thread_id, json.loads(row["body"]))
            if receipt is None and definitive_rejection:
                with self._connection() as db:
                    db.execute("UPDATE ordinary_transfers SET replay_blocked=1 WHERE sequence=?", (sequence,))
                    # A refusal on a repeated attempt cannot disprove a delayed
                    # earlier request. Continue to observe its canonical receipt.
                    state, reason = (("failed", "backend_declined_before_reservation") if fresh else
                                     ("uncertain", "previous_attempt_unconfirmed"))
                    db.execute("UPDATE intake SET state=?, reason=? WHERE sequence=?", (state, reason, sequence))
        if receipt is not None:
            state, reason = _receipt_state(receipt, json.loads(row["body"]))
            with self._connection() as db:
                db.execute("UPDATE ordinary_transfers SET replay_blocked=1 WHERE sequence=? AND replay_blocked=0",
                           (sequence,))
                db.execute("""UPDATE intake SET state=?, reason=? WHERE sequence=?
                              AND (state!=? OR reason IS NOT ?)""", (state, reason, sequence, state, reason))
        return self.get(*key)
