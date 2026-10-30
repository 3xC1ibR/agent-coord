"""Supervise owned app turns independently of model turns and UI polling.

Silence is suspicion, not proof of failure. Read authoritative provider state
first; optionally steer the same turn once; alert without replaying a prompt,
terminating a tool, releasing a lease, or manufacturing a completion.
"""
from __future__ import annotations

import logging
import threading
import time

from .app_agent_failures import notify_agent_stall, process_failure_followups


TERMINAL = {"completed", "failed", "interrupted", "cancelled", "canceled"}
NON_TOOLS = {"userMessage", "agentMessage", "reasoning", "plan"}
RECOVERY_PROMPT = (
    "Runtime watchdog: this turn has emitted no progress for five minutes. "
    "Continue the existing assignment from its current state if you can. "
    "Check the results of earlier operations before proceeding; do not repeat "
    "side effects whose outcome is unknown. If blocked, report the blocker."
)


class TurnWatchdog:
    probe_after = 300
    alert_after = 600
    tool_alert_after = 1200
    probe_interval = 60
    interval = 15

    def __init__(self, sessions, *, clock=time.monotonic):
        self.sessions, self.store, self.clock = sessions, sessions.store, clock
        # Accessed under sessions.lock; provider readers never write SQLite here.
        self.observed = {}
        self.stopped = threading.Event()
        self.worker = None
        with self.store._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS browser_turn_health (
                thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                recovery_at REAL, alerted_at REAL, detail TEXT,
                PRIMARY KEY (thread_id, turn_id)
            )""")

    def _new(self, turn_id):
        return {"turn_id": turn_id, "progress": self.clock(), "probe": None,
                "state": "healthy", "detail": None, "tools": set()}

    def observe(self, method, params):
        """Only work events count; polling, metadata and steering echoes do not."""
        tid = params.get("threadId")
        turn_id = params.get("turnId") or params.get("turn", {}).get("id")
        if method == "bridge/disconnected":
            for identity in list(self.observed):
                if self.sessions.loaded_clients.get(identity) == "codex":
                    self.observed.pop(identity, None)
            return
        if method in {"turn/completed", "thread/closed"}:
            self.observed.pop(tid, None)
            return
        if method == "turn/started":
            self.observed[tid] = self._new(turn_id)
            return
        state = self.observed.get(tid)
        if not state or turn_id != state["turn_id"] or not method.startswith("item/"):
            return
        item = params.get("item") or {}
        if item.get("type") == "userMessage":
            return  # Accepting a recovery nudge is not evidence of recovery.
        if item.get("id") and item.get("type") not in NON_TOOLS:
            if item.get("status") in TERMINAL or (method == "item/completed" and item.get("status") != "inProgress"):
                state["tools"].discard(item["id"])
            else:
                state["tools"].add(item["id"])
        was_stalled = state["state"] in {"stalled", "recovering"}
        state.update(progress=self.clock(), state="healthy", detail=None)
        if was_stalled:
            self.sessions._publish("browser/changed", {"threadId": tid})

    def snapshot(self, tid):
        with self.sessions.lock:
            state = self.observed.get(tid)
            if not state:
                return None
            return {"turn_id": state["turn_id"], "state": state["state"],
                    "quiet_seconds": max(0, int(self.clock() - state["progress"])),
                    "detail": state["detail"]}

    def _gated(self, tid):
        with self.store._connection() as db:
            row = db.execute("SELECT paused FROM browser_inbox_wake WHERE thread_id = ?", (tid,)).fetchone()
            if row and row[0]:
                return "paused"
            if db.execute("SELECT 1 FROM browser_message_queue WHERE thread_id = ?", (tid,)).fetchone():
                return "queue"
            if db.execute("SELECT 1 FROM work_threads WHERE thread_id = ? AND attention = 'archived'", (tid,)).fetchone():
                return "closed"
            if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'thread_close_requests'").fetchone():
                if db.execute("SELECT 1 FROM thread_close_requests WHERE thread_id = ? AND status IN ('queued', 'closing')", (tid,)).fetchone():
                    return "closing"
        return False

    def _claim_recovery(self, tid, turn_id):
        with self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # Only delegated app agents get automatic input. Ordinary user
            # conversations still get reconciliation and visible stall status.
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'app_control_requests'").fetchone():
                return False
            if not db.execute("SELECT 1 FROM app_control_requests WHERE operation = 'create' AND thread_id = ? AND status = 'completed'", (tid,)).fetchone():
                return False
            db.execute("INSERT OR IGNORE INTO browser_turn_health (thread_id, turn_id) VALUES (?, ?)", (tid, turn_id))
            return bool(db.execute("""UPDATE browser_turn_health SET recovery_at = ?
                WHERE thread_id = ? AND turn_id = ? AND recovery_at IS NULL AND alerted_at IS NULL""",
                (self.store.clock(), tid, turn_id)).rowcount)

    def _alert(self, tid, state, detail):
        if state["state"] == "stalled":
            return
        turn_id = state["turn_id"]
        # The notification is durable and idempotent. Retrying after a database
        # failure never sends a second alert or repeats the recovery action.
        notify_agent_stall(self.store, tid, turn_id=turn_id, error=detail)
        with self.store._connection() as db:
            db.execute("""INSERT INTO browser_turn_health (thread_id, turn_id, alerted_at, detail)
                VALUES (?, ?, ?, ?) ON CONFLICT(thread_id, turn_id) DO UPDATE SET
                alerted_at = COALESCE(alerted_at, excluded.alerted_at), detail = excluded.detail""",
                (tid, turn_id, self.store.clock(), detail))
        with self.sessions.lock:
            if self.observed.get(tid) is state:
                state.update(state="stalled", detail=detail)
        self.sessions._publish("browser/changed", {"threadId": tid})

    def _request(self, tid, method, params):
        from .codex_app_server import CodexRPC
        rpc = self.sessions._rpc(tid)
        # Native adapters read local state; Codex reads over a shared transport.
        # Bound its probe so one silent thread cannot monopolize supervision.
        return rpc.request(method, params, timeout=5) if isinstance(rpc, CodexRPC) else rpc.request(method, params)

    def _check(self, tid):
        s = self.sessions
        if self.stopped.is_set() or s.closed:
            return
        with s.lock:
            if tid not in s.loaded:
                return  # Never take ownership from another runtime.
            turn_id = s.active.get(tid)
            if tid in s.active and turn_id is None:
                return  # A bounded turn/start RPC is still in flight.
            current = self.store.threads.get(tid, history=False)
            turn_id = turn_id or current["turn_id"]
            if not turn_id:
                return
            state = self.observed.get(tid)
            if not state or state["turn_id"] != turn_id:
                state = self.observed[tid] = self._new(turn_id)
                for turn in s._history(tid).get("turns", []):
                    if turn.get("id") == turn_id:
                        state["tools"] = {i["id"] for i in turn.get("items", [])
                                          if i.get("type") not in NON_TOOLS and i.get("status") not in TERMINAL}
            if s.pending_requests(tid):
                state.update(progress=self.clock(), state="waiting", detail=None)
                return
            quiet = self.clock() - state["progress"]
            ghost = tid not in s.active
            if not ghost and quiet < self.probe_after:
                return
            if state["probe"] is not None and self.clock() - state["probe"] < self.probe_interval:
                return
            state["probe"] = self.clock()
            revision = s.turn_revisions.get(tid, 0)
            progress = state["progress"]
        error = None
        try:
            try:
                thread = self._request(tid, "thread/read", {"threadId": tid, "includeTurns": True})["thread"]
            except Exception as exc:
                if "list_turns is not supported" not in str(exc):
                    raise
                thread = self._request(tid, "thread/read", {"threadId": tid, "includeTurns": False})["thread"]
            s._reconcile_turn(tid, thread, revision)
        except Exception as exc:
            error = str(exc)
        with s.lock:
            if (s.closed or s.turn_revisions.get(tid, 0) != revision
                    or self.observed.get(tid) is not state or state["progress"] != progress):
                return
            if s.pending_requests(tid):
                return
            active = s.active.get(tid) == turn_id
            tools = bool(state["tools"])
        gate = self._gated(tid)
        if gate and gate != "queue":
            return
        if quiet >= (self.tool_alert_after if tools else self.alert_after):
            detail = (f"No observed progress for {int(quiet)} seconds. " +
                      ("A tool may still be running; its outcome is unknown. No automatic input or cancellation was sent."
                       if tools else "Provider state reconciliation did not establish completion or restored progress.") +
                      (f" Last probe error: {error[:1000]}" if error else ""))
            self._alert(tid, state, detail)
        elif active and not gate and not tools and not error and quiet >= self.probe_after:
            if s.loaded_clients.get(tid) not in {"codex", "claude"}:
                return  # ACP providers do not support exact-turn steering.
            if not self._claim_recovery(tid, turn_id):
                return
            with s.lock:
                if (s.active.get(tid) != turn_id or state["progress"] != progress
                        or state["tools"] or s.pending_requests(tid) or s.closed):
                    return
                state.update(state="recovering", detail="Checking the provider and requesting one continuation of the same turn.")
            try:
                self._request(tid, "turn/steer", {"threadId": tid, "expectedTurnId": turn_id,
                    "input": [{"type": "text", "text": RECOVERY_PROMPT}]})
            except Exception as exc:
                # Acceptance may be uncertain. The durable claim is retained.
                with s.lock:
                    if self.observed.get(tid) is state:
                        state["detail"] = f"Recovery acceptance is uncertain: {str(exc)[:1000]}"

    def process_once(self):
        s = self.sessions
        if s.closed:
            return
        try:
            process_failure_followups(self.store, s.cwd)
        except Exception:
            logging.getLogger(__name__).exception("Agent recovery deadline scan failed")
        with s.lock:
            candidates = set(s.active)
            loaded = set(s.loaded)
        # Find stale durable flags too, including flags rewritten by late hooks
        # after a provider completion. Do not rely on tool age as turn liveness.
        with self.store._connection() as db:
            candidates.update(r[0] for r in db.execute("SELECT session_id FROM sessions WHERE turn_active = 1") if r[0] in loaded)
        for tid in sorted(candidates):
            if not s.persistence.flush(timeout=0, key=tid):
                continue
            lock = s._thread_lock(tid)
            if not lock.acquire(blocking=False):
                continue  # Stop, send, and other user operations take priority.
            try:
                self._check(tid)
            except Exception:
                logging.getLogger(__name__).exception("Turn watchdog failed for %s", tid)
            finally:
                lock.release()

    def start(self):
        if self.worker is None:
            self.worker = threading.Thread(target=self._run, name="turn-watchdog", daemon=True)
            self.worker.start()

    def _run(self):
        while not self.stopped.is_set():
            try:
                self.process_once()
            except Exception:
                logging.getLogger(__name__).exception("Turn watchdog scan failed")
            self.stopped.wait(self.interval)

    def close(self):
        self.stopped.set()
        if self.worker is not None:
            self.worker.join(timeout=6)
