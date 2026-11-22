"""Durable external assignments. Only the original submitter may cross the launch boundary."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from contextlib import contextmanager

from .codex_app_server import BrowserBusyError, BrowserNotSubmittedError
from .execution_state import RUNTIME_STALE_SECONDS, _alive, read_many
from .history_persistence import HistoryUnavailableError, read_history
from .permission_modes import requested_mode
from .store import CoordinationError

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
SETTING_NAMES = {"cwd", "client", "model", "effort", "service_tier", "permission_mode", "yolo"}
ACTIVE = ("pending", "running")
RECEIPT_COLUMNS = ("source,request_id,state,thread_id,turn_id,result_json,error,"
                   "created_at,updated_at,cancel_requested,owner,resolution_method")
RESULT_LIMIT = 64_000
# A reused browser request ID is evidence only for the same literal assignment.
ACKNOWLEDGED_TURN = """(SELECT json_extract(r.result_json,'$.turn.id') FROM browser_submission_receipts r
    WHERE w.attempted=1 AND r.thread_id=w.thread_id AND r.request_id=w.request_id AND r.state='completed'
    AND json_type(r.result_json,'$.turn.id')='text' AND length(json_extract(r.result_json,'$.turn.id'))>0
    AND json_extract(r.payload_json,'$.queued')=0
    AND json_extract(r.payload_json,'$.body.message')=json_extract(w.input_json,'$.prompt')
    AND NOT EXISTS (SELECT 1 FROM json_each(r.payload_json,'$.body') WHERE key!='message'))"""
LOST_OWNER = "The owning runtime ended before saving a confirmed outcome. Inspect the conversation before requesting new work."


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise CoordinationError("Use a 1–200 character identifier beginning with a letter or digit and containing letters, digits, dot, underscore, colon or hyphen.")
    return value


def terminal_result(text):
    if not isinstance(text, str) or not text.strip():
        raise CoordinationError("The assigned turn ended without a final assistant response. Inspect the conversation.")
    if len(text) > RESULT_LIMIT:
        raise CoordinationError("The final response exceeds 64000 characters. Inspect the complete response in the conversation.")
    if text.lstrip().startswith("{"):
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            value = None
        if isinstance(value, dict) and set(value) == {"text", "artifacts"}:
            refs, prose = value["artifacts"], value["text"]
            if (not isinstance(prose, str) or not isinstance(refs, list) or len(refs) > 32
                    or any(not isinstance(ref, str) or not ref.strip() or len(ref) > 2048 for ref in refs)
                    or not (prose.strip() or refs)):
                raise CoordinationError("Invalid structured result. Inspect the final response and artifact references.")
            return value
    return {"text": text, "artifacts": []}


class ExternalWorkRuntime:
    def __init__(self, sessions):
        self.sessions = sessions
        self.store = sessions.store
        self.owner = sessions.execution.runtime_id
        # In-process clients can construct a facade over an already serving
        # BrowserSessions. Native events use the session manager's observer map.
        current = getattr(sessions, "external_work", None)
        if current is not None:
            self.lock, self.observers = current.lock, current.observers
            return
        self.lock = threading.Lock()
        self.observers = {}
        with self._write("initialize") as db:
            db.execute("""CREATE TABLE IF NOT EXISTS external_work (
                source TEXT NOT NULL, request_id TEXT NOT NULL, digest TEXT NOT NULL, input_json TEXT NOT NULL,
                owner TEXT NOT NULL, state TEXT NOT NULL, thread_id TEXT UNIQUE, turn_id TEXT, attempted INTEGER DEFAULT 0,
                cancel_requested INTEGER DEFAULT 0, result_json TEXT, error TEXT, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, PRIMARY KEY(source, request_id))""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(external_work)")}
            for name in ("evidence_state", "evidence_result_json", "evidence_error", "resolution_digest", "resolution_method"):
                if name not in columns:
                    db.execute(f"ALTER TABLE external_work ADD COLUMN {name} TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS external_work_order ON external_work(created_at DESC, source, request_id)")
            db.execute("CREATE INDEX IF NOT EXISTS external_work_source ON external_work(source, created_at DESC, request_id)")
            db.execute("""CREATE INDEX IF NOT EXISTS external_work_active_owner ON external_work(owner)
                WHERE state IN ('pending','running')""")
        self._recover_owners()
        with self.store.reading(), self.store._connection() as db:
            for row in db.execute("""SELECT thread_id,turn_id FROM external_work WHERE state='uncertain'
                    AND turn_id IS NOT NULL AND evidence_state IS NULL"""):
                self.observers[row["thread_id"]] = {"turn": row["turn_id"], "final": None}
        sessions.external_work = self

    @contextmanager
    def _write(self, name):
        with self.store.database_operation("external_work." + name), self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _recover_owners(self):
        with self.store.reading(), self.store._connection() as db:
            owners = db.execute("""SELECT DISTINCT w.owner,r.pid,r.closed_at,r.heartbeat_at FROM external_work w
                LEFT JOIN execution_runtimes r ON w.owner=r.runtime_id
                WHERE w.state IN ('pending','running') AND w.owner != ?""", (self.owner,)).fetchall()
        for row in owners:
            if (row["closed_at"] is not None or row["pid"] is None or not _alive(row["pid"])
                    or self.store.clock() - row["heartbeat_at"] > RUNTIME_STALE_SECONDS):
                with self._write("recover") as db:
                    db.execute(f"""UPDATE external_work AS w SET state='uncertain',error=?,updated_at=?,
                        turn_id=COALESCE(w.turn_id, {ACKNOWLEDGED_TURN})
                        WHERE owner=? AND state IN ('pending','running') AND NOT EXISTS
                            (SELECT 1 FROM execution_runtimes r WHERE r.runtime_id=w.owner
                                AND r.closed_at IS NULL AND r.heartbeat_at>?)""",
                        (LOST_OWNER, self.store.clock(), row["owner"], row["heartbeat_at"] or 0))

    @staticmethod
    def _public(row):
        if row is None:
            return None
        value = {k: row[k] for k in ("source", "request_id", "state", "thread_id", "turn_id", "error",
                                     "created_at", "updated_at")}
        value["result"] = json.loads(row["result_json"]) if row["result_json"] else None
        value["cancel_requested"] = bool(row["cancel_requested"])
        if row["resolution_method"]:
            value["resolution_method"] = row["resolution_method"]
        return value

    def _row(self, key):
        with self.store.reading(), self.store._connection() as db:
            return db.execute(f"SELECT {RECEIPT_COLUMNS} FROM external_work WHERE source=? AND request_id=?", key).fetchone()

    def get(self, source, request_id):
        key = identifier(source), identifier(request_id)
        row = self._row(key)
        if row and row["owner"] != self.owner and row["state"] in ACTIVE:
            self._recover_owners()
            row = self._row(key)
        return self._public(row)

    def list(self, source=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise CoordinationError("Choose a limit between 1 and 200.")
        args = [] if source is None else [identifier(source)]
        where = " WHERE source=?" if args else ""
        self._recover_owners()
        with self.store.reading(), self.store._connection() as db:
            rows = db.execute(f"SELECT {RECEIPT_COLUMNS} FROM external_work" + where +
                              " ORDER BY created_at DESC,source,request_id LIMIT ?", (*args, limit)).fetchall()
        return [self._public(row) for row in rows]

    def submit(self, source, request_id, settings, prompt, name=None, *, request_text=None):
        key = identifier(source), identifier(request_id)
        if not isinstance(settings, dict) or set(settings) - SETTING_NAMES:
            raise CoordinationError("Supply a settings object with supported provider creation fields.")
        if any(not isinstance(v, str) or not v.strip() or len(v) > 4096 for k, v in settings.items() if k != "yolo"):
            raise CoordinationError("Provider setting values must be nonempty strings.")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100_000:
            raise CoordinationError("Supply 1–100000 characters of assignment text.")
        if name is not None:
            self.sessions._text(name, "Session name", 160)
        mode = requested_mode(settings.get("client", "codex"), settings)
        inputs = {"settings": settings, "prompt": prompt, "name": name}
        if request_text is not None:
            # Adapters may wrap the saved request in execution context.
            self.sessions._text(request_text, "Original request", 100_000)
            inputs["request_text"] = request_text
        encoded = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.sessions.admission.enter() as admitted:
            if not admitted:
                raise BrowserBusyError(self.sessions.admission.message("Runtime maintenance is pausing new work."))
            now = self.store.clock()
            with self._write("reserve") as db:
                inserted = db.execute("""INSERT INTO external_work
                    (source,request_id,digest,input_json,owner,state,created_at,updated_at)
                    VALUES (?,?,?,?,?,'pending',?,?) ON CONFLICT(source,request_id) DO NOTHING""",
                    (*key, digest, encoded, self.owner, now, now)).rowcount
                if not inserted and db.execute("SELECT digest FROM external_work WHERE source=? AND request_id=?", key).fetchone()[0] != digest:
                    raise BrowserBusyError("This source and request ID were already used with different input.")
            if inserted:
                self._launch(key, {**settings, "permission_mode": mode, "name": name}, prompt, request_text)
        return self.get(*key)

    def _launch(self, key, settings, prompt, request_text):
        thread_id = None
        stage = "validate"
        expected = {}

        def validated(record):
            nonlocal stage
            expected.update(record)
            stage = "create"

        def created(identity):
            nonlocal thread_id
            thread_id = identity
            with self._write("bind_thread") as db:
                db.execute("""UPDATE external_work SET thread_id=?,updated_at=? WHERE source=? AND request_id=?
                    AND resolution_digest IS NULL""",
                           (identity, self.store.clock(), *key))

        try:
            # Validate before the native creation boundary. An invalid selection
            # is a durable rejection even when the client retries after catalog changes.
            self.sessions.create(settings, on_created=created, on_validated=validated)
            with self.sessions._thread_lock(thread_id):
                stage = "check"
                actual = self.sessions._record(thread_id)
                for field in ("client", "cwd", "model", "effort", "service_tier", "permission_mode"):
                    if expected[field] is not None and actual[field] != expected[field]:
                        raise CoordinationError(f"The provider selected a different {field}. Inspect the new conversation.")
                effective = actual.get("permission_mode_effective")
                if effective is not None and effective != expected["permission_mode"]:
                    raise CoordinationError("The provider selected different effective permissions.")
                with self._write("admit") as db:
                    used = db.execute("SELECT turn_id FROM work_threads WHERE thread_id=?", (thread_id,)).fetchone()
                    if used and used["turn_id"]:
                        raise BrowserBusyError("Another turn already used this conversation. Inspect it before requesting new work.")
                    if not self.sessions.inbox_wake._eligible(db, thread_id, cleanup=False):
                        raise BrowserBusyError("The conversation is paused or unavailable. Review its status.")
                    admitted = db.execute("""UPDATE external_work SET attempted=1,updated_at=? WHERE source=?
                        AND request_id=? AND state='pending' AND cancel_requested=0""",
                        (self.store.clock(), *key)).rowcount
                if not admitted:
                    return
                if request_text is not None:
                    self.store.threads.capture_request(thread_id, request_text)
                with self.lock:
                    self.observers[thread_id] = {"turn": None, "final": None, "early": {}}
                body = {"message": prompt, "requestId": key[1]}
                stage = "submit"
                receipt = self.sessions.submissions.run(thread_id, body, lambda: self.sessions.send(
                    thread_id, body, start_only=True, literal=True))
                turn_id = (receipt.get("turn") or {}).get("id")
                if not isinstance(turn_id, str) or not turn_id:
                    raise CoordinationError("The provider did not return an assignment turn ID.")
                with self._write("bind_turn") as db:
                    db.execute("""UPDATE external_work SET turn_id=?,state=CASE WHEN state='pending' THEN 'running' ELSE state END,
                        updated_at=? WHERE source=? AND request_id=? AND state IN ('pending','uncertain')""", (turn_id, self.store.clock(), *key))
                with self.lock:
                    watch = self.observers[thread_id]
                    watch["turn"] = turn_id
                    early = watch.pop("early").get(turn_id)
                if early:
                    self._finish(thread_id, turn_id, *early)
                if self._row(key)["cancel_requested"]:
                    self._interrupt(thread_id, turn_id)
        except Exception as exc:
            known = stage in {"validate", "create", "check"} or isinstance(exc, (BrowserBusyError, BrowserNotSubmittedError))
            state = "failed" if known else "uncertain"
            detail = str(exc)[:1000] or type(exc).__name__
            if stage == "create":
                detail += " No assignment was sent. Check for an empty provider conversation."
            if not known:
                detail += " Inspect the conversation before requesting new work. The submission will not be replayed."
            with self._write("launch_error") as db:
                db.execute("""UPDATE external_work SET state=?,error=?,updated_at=? WHERE source=? AND request_id=?
                    AND state IN ('pending','running')""", (state, detail, self.store.clock(), *key))
            with self.lock:
                self.observers.pop(thread_id, None)

    def observe(self, method, params, *, pending_requests=()):
        """Copy only bounded, conclusive provider evidence on the event reader."""
        thread = params.get("threadId")
        turn = params.get("turn") or {}
        identity = params.get("turnId") or turn.get("id")
        with self.lock:
            watch = self.observers.get(thread)
            if watch is None or not identity or watch["turn"] not in (None, identity):
                return
            if method == "item/completed":
                item = params.get("item") or {}
                # A later tool or commentary item invalidates a previous
                # candidate when the provider omits its terminal item list.
                if item.get("type") != "userMessage":
                    watch["final"] = None
                if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                    text = item.get("text")
                    watch["final"] = (identity, text[:RESULT_LIMIT+1]) if isinstance(text, str) else None
                return
            if method == "disconnect":
                outcome = ("uncertain", None, "Provider connection lost. Inspect the assigned turn before requesting new work.")
            elif method == "turn/completed":
                final = watch["final"]
                text = final[1] if final and final[0] == identity else None
                items = turn.get("items") or []
                if items:
                    last = items[-1]
                    text = None
                    if (last.get("type") == "agentMessage" and last.get("phase") in (None, "final_answer")
                            and last.get("status") in (None, "completed")):
                        text = last.get("text")
                    elif len(items) == 1 and last.get("type") == "userMessage" and final and final[0] == identity:
                        # Some native completions repeat only the initial input.
                        text = final[1]
                error = turn.get("error")
                if (turn.get("termination") or {}).get("origin") == "user" or turn.get("status") in {"cancelled", "canceled"}:
                    outcome = ("cancelled", None, "The assigned turn was stopped.")
                elif (error or turn.get("status") != "completed"
                        or any(r["params"].get("threadId") == thread for r in pending_requests)):
                    message = error.get("message") if isinstance(error, dict) else error
                    outcome = ("failed", None, str(message or "The assigned turn stopped before successful completion.")[:1000])
                else:
                    # Parsing/encoding happens outside the provider's lock.
                    outcome = ("succeeded", text[:RESULT_LIMIT+1] if isinstance(text, str) else None, None)
            else:
                return
            if watch["turn"] is None:
                watch["early"].setdefault(identity, outcome)
                return
        self.sessions.persistence.submit(lambda: self._finish(thread, identity, *outcome), key=thread)

    def _finish(self, thread, turn, state, text, error):
        result = None
        if state == "succeeded":
            try:
                result = json.dumps(terminal_result(text))
            except CoordinationError as exc:
                state, error = "failed", str(exc)
        with self._write("finish") as db:
            row = db.execute("""SELECT state,cancel_requested FROM external_work
                WHERE thread_id=? AND turn_id=?""", (thread, turn)).fetchone()
            if row and row["state"] == "running":
                state, result, error = self._guard_outcome(db, row, thread, turn, state, result, error)
                db.execute("""UPDATE external_work SET state=?,result_json=?,error=?,updated_at=?
                    WHERE thread_id=? AND turn_id=?""", (state, result, error, self.store.clock(), thread, turn))
            elif row and row["state"] == "uncertain" and state != "uncertain":
                # A late native completion supplies evidence. It takes a deliberate
                # resolution request to publish a terminal outcome for uncertain work.
                state, result, error = self._guard_outcome(db, row, thread, turn, state, result, error)
                db.execute("""UPDATE external_work SET evidence_state=?,evidence_result_json=?,evidence_error=?
                    WHERE thread_id=? AND turn_id=? AND evidence_state IS NULL""", (state, result, error, thread, turn))
        if state != "uncertain":
            with self.lock:
                self.observers.pop(thread, None)

    @staticmethod
    def _guard_outcome(db, row, thread, turn, state, result, error):
        stopped = db.execute("SELECT 1 FROM turn_stop_requests WHERE thread_id=? AND turn_id=?", (thread, turn)).fetchone()
        if (row["cancel_requested"] or stopped) and state != "uncertain":
            return "cancelled", None, "The assigned turn was stopped."
        if state == "succeeded" and db.execute("""SELECT 1 FROM asks
                WHERE session_id=? AND status='open' AND blocking=1 LIMIT 1""", (thread,)).fetchone():
            return "failed", None, "The assigned turn ended awaiting user input."
        return state, result, error

    def _archived_outcome(self, thread, turn):
        if not thread or not turn:
            return None
        with self.store.reading(), self.store._connection() as db:
            observed = db.execute("""SELECT w.evidence_state,x.state,x.source FROM external_work w
                LEFT JOIN execution_snapshots x ON x.thread_id=w.thread_id AND x.turn_id=w.turn_id
                WHERE w.thread_id=? AND w.turn_id=?""", (thread, turn)).fetchone()
        if observed and observed["evidence_state"]:
            return None  # The bounded exact-turn event is already durable.
        if observed and observed["source"] == "provider_event" and observed["state"] == "failed":
            return "failed", None, "The provider reported failure for the assigned turn."
        try:
            history = read_history(self.store, thread)
        except (HistoryUnavailableError, json.JSONDecodeError):
            return None
        if not history or history.get("id") != thread:
            return None
        record = next((entry for entry in history.get("turns", []) if entry.get("id") == turn), {})
        if record.get("status") not in {"completed", "failed", "interrupted", "cancelled", "canceled"}:
            return None
        if record.get("status") in {"cancelled", "canceled"} or (record.get("termination") or {}).get("origin") == "user":
            return "cancelled", None, "The assigned turn was stopped."
        if record.get("error") or record["status"] != "completed":
            error = record.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            return "failed", None, str(message or "The assigned turn ended unsuccessfully.")[:1000]
        items = record.get("items") or []
        last = items[-1] if items else {}
        # A legacy display mirror can contain partial deltas even after a
        # terminal event. Require a completed item for automatic recovery.
        if (last.get("type") == "agentMessage" and last.get("status") == "completed"
                and last.get("phase") in {None, "final_answer"}):
            try:
                return "succeeded", json.dumps(terminal_result(last.get("text"))), None
            except CoordinationError as exc:
                return "failed", None, str(exc)
        return None

    def resolve(self, source, request_id, review=None):
        key = identifier(source), identifier(request_id)
        review = {} if review is None else review
        if not isinstance(review, dict):
            raise CoordinationError("Resolve accepts an empty object or a reviewed result or error.")
        manual = None
        if review:
            if set(review) not in ({"thread_id", "turn_id", "error"}, {"thread_id", "turn_id", "result"}):
                raise CoordinationError("Supply the inspected thread_id and turn_id with exactly one result or error.")
            if "error" in review:
                error = review["error"]
                if not isinstance(error, str) or not error.strip() or len(error) > 1000:
                    raise CoordinationError("A reviewed error must contain 1–1000 characters.")
                manual = ("failed", None, error)
            else:
                value = review["result"]
                if not isinstance(value, dict) or set(value) != {"text", "artifacts"} or not review["thread_id"]:
                    raise CoordinationError("A reviewed success requires a known conversation and a result with text and artifacts.")
                result = terminal_result(json.dumps(value, ensure_ascii=False))
                manual = ("succeeded", json.dumps(result), None)
        digest = hashlib.sha256(json.dumps(review, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        receipt = self.get(*key)
        if not receipt:
            return None
        if receipt["state"] == "uncertain" and receipt["thread_id"] and receipt["turn_id"] is None:
            with self._write("recover_binding") as db:
                db.execute(f"""UPDATE external_work AS w SET turn_id={ACKNOWLEDGED_TURN} WHERE source=?
                    AND request_id=? AND state='uncertain' AND turn_id IS NULL""", key)
            receipt = self.get(*key)
        thread, turn = receipt["thread_id"], receipt["turn_id"]
        if review and (review["thread_id"], review["turn_id"]) != (thread, turn):
            raise BrowserBusyError("The reviewed thread and turn do not match this assignment.")
        live = False
        archived = None
        if receipt["state"] == "uncertain" and thread:
            self.sessions._await_persistence(thread)
            with self.sessions.lock:
                live = thread in self.sessions.active and (turn is None or self.sessions.active[thread] == turn)
            archived = self._archived_outcome(thread, turn)
            if manual and not live and not archived:
                with self.store.reading(), self.store._connection() as db:
                    execution = read_many(self.store, db, [thread]).get(thread, {})
                live = (execution.get("freshness") == "live" and execution.get("state") in
                        {"running", "waiting_approval", "waiting_answer"}
                        and (turn is None or turn == execution.get("turn_id")))
        with self._write("resolve") as db:
            row = db.execute("""SELECT state,thread_id,turn_id,resolution_digest,cancel_requested,
                    evidence_state,evidence_result_json,evidence_error
                FROM external_work WHERE source=? AND request_id=?""", key).fetchone()
            if row["state"] != "uncertain":
                if row["resolution_digest"] != digest:
                    raise BrowserBusyError("Only uncertain external work can be resolved.")
            else:
                if (row["thread_id"], row["turn_id"]) != (thread, turn):
                    raise BrowserBusyError("The assignment binding changed. Review the receipt again.")
                evidence = ((row["evidence_state"], row["evidence_result_json"], row["evidence_error"])
                            if row["evidence_state"] else archived)
                if not evidence and manual and live:
                    raise BrowserBusyError("The provider still has an active assignment. Inspect it after it stops.")
                if not evidence and not manual:
                    raise BrowserBusyError("No confirmed terminal result for the assigned turn. Inspect the conversation and supply a reviewed result or error.")
                if manual and manual[0] == "succeeded" and turn is None and db.execute("""
                        SELECT 1 FROM turn_stop_requests WHERE thread_id=? LIMIT 1""", (thread,)).fetchone():
                    raise BrowserBusyError("Cannot associate this result with a turn after a recorded Stop.")
                # The actual native result wins over an operator's alternative.
                state, result, error = self._guard_outcome(db, row, thread, turn, *(evidence or manual))
                db.execute("""UPDATE external_work SET state=?,result_json=?,error=?,updated_at=?,resolution_digest=?,
                    resolution_method=? WHERE source=? AND request_id=?""", (state, result, error, self.store.clock(),
                    digest, "provider_evidence" if evidence else "operator_review", *key))
        with self.lock:
            self.observers.pop(thread, None)
        return self.get(*key)

    def cancel(self, source, request_id):
        key = identifier(source), identifier(request_id)
        current = self.get(*key)
        if not current or current["state"] not in ACTIVE:
            return current
        with self._write("cancel") as db:
            row = db.execute("SELECT * FROM external_work WHERE source=? AND request_id=?", key).fetchone()
            if row["owner"] != self.owner:
                raise BrowserBusyError("Cancel the assignment on the Ribbon Field runtime that owns it.")
            db.execute("""UPDATE external_work SET cancel_requested=1,
                state=CASE WHEN attempted=0 THEN 'cancelled' ELSE state END,
                error=CASE WHEN attempted=0 THEN 'Cancelled before submission.' ELSE error END,updated_at=?
                WHERE source=? AND request_id=? AND state IN ('pending','running')""", (self.store.clock(), *key))
        if row["state"] == "running":
            self._interrupt(row["thread_id"], row["turn_id"])
        return self.get(*key)

    def _interrupt(self, thread, turn):
        with self.sessions._thread_lock(thread):
            with self.sessions.lock:
                active = self.sessions.active.get(thread)
            if active != turn:
                # Completion may already be queued for asynchronous persistence.
                # A missing active turn alone cannot confirm a successful Stop.
                self.sessions._await_persistence(thread)
                self._finish(thread, turn, "uncertain", None,
                             "The assigned turn is no longer active and its terminal outcome could not be confirmed.")
                return
            if self.store.threads.stop_requested(thread, turn):
                return
            try:
                self.sessions.interrupt(thread)
            except Exception as exc:
                self._finish(thread, turn, "uncertain", None, "Stop could not be confirmed. Inspect the conversation. " + str(exc)[:500])
