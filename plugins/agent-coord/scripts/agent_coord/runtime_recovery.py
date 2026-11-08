"""Durable, conservative continuation after an explicitly requested update."""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

PROMPT = ("[Automatic continuation after a Ribbon Field backend update]\n"
          "Continue the unfinished assignment from the saved conversation. The update interrupted execution. "
          "Before repeating any action that changes state, inspect whether it completed or is still running. "
          "If the outcome cannot be established, report the uncertainty instead of repeating the action.")


class RuntimeRecovery:
    def __init__(self, sessions, *, permitted=lambda: True):
        self.sessions, self.store = sessions, sessions.store
        self.permitted = permitted
        with self.store._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS runtime_update_recoveries (
                update_id TEXT NOT NULL, thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                turn_key TEXT, settings TEXT NOT NULL, state TEXT NOT NULL, reason TEXT,
                result_turn_id TEXT, created_at REAL NOT NULL,
                PRIMARY KEY(update_id, thread_id))""")

    def settings(self, thread_id):
        record = self.sessions._record(thread_id)
        return json.dumps({key: record.get(key) for key in
            ("client", "cwd", "model", "effort", "yolo", "permission_mode", "service_tier")}, sort_keys=True)

    def reason(self, thread_id, turn_id, *, turn_key=None, settings=None, receipt=None):
        sessions = self.sessions
        if sessions.closed or not self.permitted():
            return "Runtime recovery was stopped."
        thread = self.store.threads.get(thread_id)
        if thread["turn_id"] != turn_id or (turn_key is not None and thread["turn_key"] != turn_key):
            return "A newer turn superseded the interrupted work."
        if thread["attention"] == "archived" or thread.get("snoozed"):
            return "The conversation is archived or snoozed."
        if self.store.threads.stop_requested(thread_id, turn_id):
            return "The user stopped this turn."
        if settings is not None and self.settings(thread_id) != settings:
            return "Conversation settings changed."
        with sessions.lock:
            if any((value.get("params") or {}).get("threadId") == thread_id for value in sessions.requests.values()):
                return "An approval or answer requires the user."
        with self.store.reading(), self.store._connection() as db:
            client = sessions._record(thread_id)["client"]
            if client == "claude":
                row = db.execute("SELECT started FROM claude_sessions WHERE thread_id=?", (thread_id,)).fetchone()
                if not row or not row[0]:
                    return "No saved native Claude session is available."
            elif client in {"kimi", "antigravity", "muse"}:
                return "This provider cannot verify native turn recovery; continue it explicitly."
            if db.execute("SELECT 1 FROM turn_completions WHERE thread_id=? AND turn_key=? AND status='completed'",
                          (thread_id, thread["turn_key"])).fetchone():
                return "The original turn completed."
            if db.execute("SELECT 1 FROM browser_message_queue WHERE thread_id=?", (thread_id,)).fetchone():
                return "Queued user input takes precedence."
            if db.execute("SELECT 1 FROM browser_submission_receipts WHERE thread_id=? AND state IN ('accepted','uncertain') AND request_id != ?",
                          (thread_id, receipt or "")).fetchone():
                return "A previous submission has an uncertain outcome."
            if db.execute("SELECT 1 FROM browser_inbox_wake WHERE thread_id=? AND paused=1 AND pause_kind='stop'",
                          (thread_id,)).fetchone():
                return "The user stopped this conversation."
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='thread_close_requests'").fetchone():
                if db.execute("SELECT 1 FROM thread_close_requests WHERE thread_id=? AND status IN ('queued','closing')",
                              (thread_id,)).fetchone():
                    return "The conversation is closing."
            if db.execute("SELECT 1 FROM browser_inbox_wake WHERE thread_id=? AND (dispatching=1 OR paused=1)", (thread_id,)).fetchone():
                return "An inbox submission requires review."
            if db.execute("SELECT 1 FROM message_wake_attempts WHERE session_id=? AND outcome IN ('claimed','uncertain')", (thread_id,)).fetchone():
                return "An automatic submission has an uncertain outcome."
            if db.execute("SELECT 1 FROM dispatch_requests WHERE (target_id=? OR dispatcher_id=?) AND state IN ('creating','sending','uncertain')", (thread_id, thread_id)).fetchone():
                return "A routed submission has an uncertain outcome."
            if db.execute("SELECT COUNT(*) FROM runtime_update_recoveries WHERE thread_id=? AND state='continued' AND created_at>?",
                          (thread_id, self.store.clock() - 300)).fetchone()[0] >= 3:
                return "Repeated automatic recovery requires review."
        if sessions.reviews.active(thread_id):
            return "Reviewed work requires explicit continuation."
        return None

    def prepare(self, update_id):
        sessions = self.sessions
        if not sessions.persistence.flush():
            raise RuntimeError("Conversation bookkeeping could not be saved; update deferred.")
        with sessions.lock:
            active = dict(sessions.active)
            # Requests arriving during shutdown are retained as recovery exclusions.
            sessions.runtime_update_requests = {
                (value.get("params") or {}).get("threadId") for value in sessions.requests.values()}
        for thread_id, turn_id in active.items():
            if not turn_id:
                raise RuntimeError("A provider submission is still starting; update deferred.")
            with sessions._thread_lock(thread_id):
                thread = self.store.threads.get(thread_id)
                reason = self.reason(thread_id, turn_id)
                settings = self.settings(thread_id)
                with self.store._connection() as db:
                    db.execute("INSERT OR IGNORE INTO runtime_update_recoveries VALUES (?,?,?,?,?,?,?,NULL,?)",
                        (update_id, thread_id, turn_id, thread["turn_key"], settings,
                         "skipped" if reason else "prepared", reason, self.store.clock()))

    def finalize(self, update_id):
        with self.store._connection() as db:
            for thread_id in getattr(self.sessions, "runtime_update_requests", set()):
                db.execute("UPDATE runtime_update_recoveries SET state='skipped', reason=? WHERE update_id=? AND thread_id=?",
                           ("An approval or answer requires the user.", update_id, thread_id))

    def recover(self, update_id):
        with self.store._connection() as db:
            # A process may have died after provider acceptance. Never replay it.
            db.execute("UPDATE runtime_update_recoveries SET state='uncertain', reason=? WHERE update_id=? AND state='submitting'",
                       ("Continuation delivery was interrupted; inspect the conversation.", update_id))
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM runtime_update_recoveries WHERE update_id=? AND (state='prepared' OR (state='skipped' AND reason='Queued user input takes precedence.'))", (update_id,))]
        for row in rows:
            self.sessions.runtime_recovery_turns = getattr(self.sessions, "runtime_recovery_turns", {})
            self.sessions.runtime_recovery_turns[row["thread_id"]] = row["turn_id"]
        for row in rows:
            if row["state"] == "prepared":
                self._recover_one(row)

    def verify(self, row, *, receipt=None):
        from .codex_app_server import BrowserBusyError
        identity = row["thread_id"]
        reason = self.reason(identity, row["turn_id"], turn_key=row["turn_key"], settings=row["settings"], receipt=receipt)
        if reason:
            raise BrowserBusyError(reason)
        provider = self.sessions._rpc(identity)
        # Do not validate against the display cache: it deliberately preserves
        # old turns when a native provider returns an empty response.
        history = self.sessions._query_thread(provider, identity, include_turns=True)
        turns = history.get("turns") or []
        if history.get("historyUnavailable") or not turns or turns[-1].get("id") != row["turn_id"]:
            raise BrowserBusyError("Native history could not confirm the interrupted turn.")
        if turns[-1].get("status") == "completed":
            raise BrowserBusyError("The original native turn completed.")
        if history.get("status", {}).get("type") == "active" or turns[-1].get("status") == "inProgress":
            raise BrowserBusyError("Native execution is still active; automatic continuation withheld.")
        if self.sessions._record(identity)["client"] == "claude":
            self.verify_claude(identity, turns[-1])
        with self.sessions.lock:
            if identity in self.sessions.active:
                raise BrowserBusyError("Native execution is still active; automatic continuation withheld.")

    def verify_claude(self, identity, turn):
        """Claude thread/read is a mirror; verify its native transcript as well."""
        from .codex_app_server import BrowserBusyError
        with self.store.reading(), self.store._connection() as db:
            row = db.execute("SELECT native_id FROM claude_sessions WHERE thread_id=?", (identity,)).fetchone()
        native = str(uuid.UUID(row[0] or identity))
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
        paths = list(root.glob(f"*/{native}.jsonl"))
        if len(paths) != 1:
            raise BrowserBusyError("The native Claude transcript is missing or ambiguous.")
        try:
            entries = [json.loads(line) for line in paths[0].read_bytes().splitlines() if line.strip()]
        except (ValueError, OSError) as exc:
            raise BrowserBusyError("The native Claude transcript is incomplete.") from exc
        prompt = next((i["id"] for i in reversed(turn.get("items", [])) if i.get("type") == "userMessage"), None)
        source = next((n for n, entry in enumerate(entries) if entry.get("uuid") == prompt and prompt), None)
        if source is None:
            raise BrowserBusyError("The native Claude transcript does not confirm the source input.")
        for entry in entries[source + 1:]:
            if entry.get("isSidechain") or entry.get("isMeta"):
                continue
            message = entry.get("message") or {}
            if entry.get("type") == "assistant":
                if message.get("stop_reason") not in {"tool_use"}:
                    raise BrowserBusyError("Native Claude completion is finished or uncertain; review before continuing.")
            elif entry.get("type") == "user":
                content = message.get("content")
                if not isinstance(content, list) or any(item.get("type") != "tool_result" for item in content):
                    raise BrowserBusyError("Newer native Claude input superseded the interrupted work.")

    def _recover_one(self, row):
        sessions = self.sessions
        thread_id, update_id = row["thread_id"], row["update_id"]
        state, reason, result_turn = "skipped", None, None
        try:
            with sessions._thread_lock(thread_id):
                reason = self.reason(thread_id, row["turn_id"], turn_key=row["turn_key"], settings=row["settings"])
                if not reason:
                    sessions.read(thread_id)
                    sessions._await_persistence(thread_id)
                    self.verify(row)
                if not reason:
                    with self.store._connection() as db:
                        claimed = db.execute("UPDATE runtime_update_recoveries SET state='submitting' WHERE update_id=? AND thread_id=? AND state='prepared'",
                                             (update_id, thread_id)).rowcount
                    if not claimed:
                        return
                    state = "uncertain"
                    body = {"message": PROMPT, "requestId": "runtime-update:" + update_id + ":" + thread_id}
                    result = sessions.submissions.run(thread_id, body, lambda: sessions.send(
                        thread_id, body, start_only=True, recovery=lambda: self.verify(row, receipt=body["requestId"])))
                    result_turn = (result.get("turn") or {}).get("id")
                    state = "continued"
        except Exception as error:
            from .codex_app_server import BrowserBusyError, BrowserNotSubmittedError
            if isinstance(error, (BrowserBusyError, BrowserNotSubmittedError)):
                state = "skipped"
            reason = str(error)[:2000]
        with self.store._connection() as db:
            db.execute("UPDATE runtime_update_recoveries SET state=?, reason=?, result_turn_id=? WHERE update_id=? AND thread_id=? AND state IN ('prepared','submitting')",
                       (state, reason, result_turn, update_id, thread_id))

    def summary(self, update_id):
        with self.store.reading(), self.store._connection() as db:
            return dict(db.execute("SELECT state, COUNT(*) FROM runtime_update_recoveries WHERE update_id=? GROUP BY state", (update_id,)))


def status(store, update_id):
    with store.reading(), store._connection() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_update_recoveries'").fetchone():
            return {"counts": {}, "conversations": []}
        rows = [dict(row) for row in db.execute(
            "SELECT thread_id,state,reason FROM runtime_update_recoveries WHERE update_id=?", (update_id,))]
    counts = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return {"counts": counts, "conversations": rows}
