"""Supervise owned app turns independently of model turns and UI polling.

Silence is suspicion, not proof of failure. Read authoritative provider state
first; optionally steer the same turn once; alert without replaying a prompt,
terminating a tool, releasing a lease, or manufacturing a completion.
"""
from __future__ import annotations

from functools import lru_cache
import logging
import threading
import time

from . import claude_progress, stall_notifications
from .claude_usage import native_id
from .timeline_clock import seconds
from .app_agent_failures import process_failure_followups


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
        self.last_progress_flush = None
        with self.store.database_operation("watchdog.initialize", priority="background"), self.store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS browser_turn_health (
                thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                recovery_at REAL, alerted_at REAL, detail TEXT, last_progress_at REAL,
                PRIMARY KEY (thread_id, turn_id)
            )""")

            columns = {r[1] for r in db.execute("PRAGMA table_info(browser_turn_health)")}
            if "last_progress_at" not in columns:
                db.execute("ALTER TABLE browser_turn_health ADD COLUMN last_progress_at REAL")

    def _new(self, turn_id):
        return {"turn_id": turn_id, "progress": self.clock(), "probe": None,
                "progress_at": self.store.clock(),
                "state": "healthy", "detail": None, "tools": set(), "transcripts": None,
                "tool_cpu": None, "classification": None, "waiting_since": None,
                "alert_pending": False, "next_alert_at": None, "saved_progress": None}

    def restore_observations(self, observations):
        """Extend retained records from older coordinators without resetting their progress clocks."""
        self.observed = observations
        for state in observations.values():
            defaults = self._new(state["turn_id"])
            # Compatible handovers run on the same boot and share the monotonic clock.
            defaults["progress_at"] -= max(0, defaults["progress"] - state["progress"])
            for name, value in defaults.items():
                state.setdefault(name, value)

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
            current = self.observed.get(tid)
            if current and (method == "thread/closed" or current["turn_id"] == turn_id):
                self.observed.pop(tid, None)
            elif current:
                return
            if not current or current["alert_pending"] or current["next_alert_at"] is not None:
                terminal_id = current["turn_id"] if current else turn_id
                if terminal_id:
                    self.sessions.persistence.submit(
                        lambda: stall_notifications.clear_stall(self.store, tid, terminal_id, reason=method), key=tid)
            return
        if method == "turn/started":
            if self.observed.get(tid, {}).get("turn_id") != turn_id:
                self.observed[tid] = self._new(turn_id)
            return
        state = self.observed.get(tid)
        if not state or turn_id != state["turn_id"] or not method.startswith("item/"):
            return
        if method.endswith(("/requestApproval", "/requestUserInput")):
            return  # The retained request records its exact received_at timestamp.
        item = params.get("item") or {}
        if item.get("type") == "userMessage":
            return  # Accepting a recovery nudge is not evidence of recovery.
        if item.get("id") and item.get("type") not in NON_TOOLS:
            if item.get("status") in TERMINAL or (method == "item/completed" and item.get("status") != "inProgress"):
                state["tools"].discard(item["id"])
            else:
                state["tools"].add(item["id"])
        if state["waiting_since"] is None:
            self._progress(tid, state, self.clock(), self.store.clock())

    def _progress(self, tid, state, observed, wall):
        had_alert = state["alert_pending"] or state["next_alert_at"] is not None
        was_stalled = had_alert or state["state"] == "recovering"
        if had_alert:
            turn_id = state["turn_id"]
            self.sessions.persistence.submit(
                lambda: stall_notifications.record_progress(self.store, tid, turn_id, progress_at=wall), key=tid)
        state.update(progress=observed, progress_at=wall, state="healthy", detail=None,
                     classification=None, next_alert_at=None)
        if was_stalled:
            self.sessions._publish("browser/changed", {"threadId": tid})

    @staticmethod
    def _snapshot(state, now):
        return {"turn_id": state["turn_id"], "state": state["state"],
                "quiet_seconds": max(0, int(now - state["progress"])),
                "detail": state["detail"], "classification": state["classification"]}

    def snapshot(self, tid):
        with self.sessions.lock:
            state = self.observed.get(tid)
            return self._snapshot(state, self.clock()) if state else None

    def snapshots(self):
        with self.sessions.lock:
            now = self.clock()
            return {tid: self._snapshot(state, now) for tid, state in self.observed.items()}

    def _gated(self, tid):
        with self.store.reading(), self.store._connection() as db:
            row = db.execute("SELECT pause_kind FROM browser_inbox_wake WHERE thread_id = ? AND paused=1", (tid,)).fetchone()
            if row:
                return "stop" if row[0] in {"stop", "supervisor"} else "paused"
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

    @staticmethod
    def _health_state(classification):
        return {"waiting_for_approval": "waiting", "long_running_tool": "long_running_tool"}.get(classification, "stalled")

    def _alert(self, tid, state, detail, classification, expected_progress):
        turn_id = state["turn_id"]
        with self.sessions.lock:
            if self.observed.get(tid) is not state or state["progress"] != expected_progress:
                return
            if (state["next_alert_at"] is not None and self.store.clock() < state["next_alert_at"]
                    and state["classification"] == classification):
                return
            state["alert_pending"] = True
            wall = state["progress_at"]
        try:
            alert = stall_notifications.record_stall(self.store, tid, turn_id, progress_at=wall,
                                                     detail=detail, classification=classification)
        finally:
            with self.sessions.lock:
                state["alert_pending"] = False
        if alert:
            with self.sessions.lock:
                if self.observed.get(tid) is not state or state["progress"] != expected_progress:
                    return
                state.update(state=self._health_state(classification), detail=detail, classification=classification,
                             next_alert_at=alert["next_alert_at"])
            self.sessions._publish("browser/changed", {"threadId": tid})

    def _request(self, tid, method, params):
        from .codex_app_server import CodexRPC
        rpc = self.sessions._rpc(tid)
        # Native adapters read local state; Codex reads over a shared transport.
        # Bound its probe so one silent thread cannot monopolize supervision.
        return rpc.request(method, params, timeout=5) if isinstance(rpc, CodexRPC) else rpc.request(method, params)

    def _restore(self, tid, turn_id):
        state = self._new(turn_id)
        wall = self.store.clock()
        with self.store.reading(), self.store._connection() as db:
            row = db.execute("SELECT last_progress_at FROM browser_turn_health WHERE thread_id=? AND turn_id=?",
                             (tid, turn_id)).fetchone()
        saved = row[0] if row else None
        previous = stall_notifications.snapshot(self.store, tid, turn_id)
        # Existing installations have provider turn/item timestamps even before
        # the first periodic checkpoint of native output progress.
        last_evidence = saved
        for turn in self.sessions._history(tid).get("turns", []):
            if turn.get("id") != turn_id:
                continue
            items = turn.get("items", [])
            state["tools"] = {i["id"] for i in items if i.get("id") and i.get("type") not in NON_TOOLS
                              and i.get("status") not in TERMINAL}
            if last_evidence is None:
                times = [seconds(turn.get("startedAt"))] + [seconds(i.get("timelineAt")) for i in items
                                                           if i.get("type") != "userMessage"]
                last_evidence = max((t for t in times if t is not None), default=None)
            break
        if previous:
            classification = previous["classification"]
            last_evidence = previous["progress_at"]
            state.update(state=self._health_state(classification), classification=classification,
                         detail=previous["detail"], next_alert_at=previous["next_alert_at"])
        if last_evidence is not None:
            state.update(progress=self.clock() - max(0, wall - last_evidence), progress_at=min(wall, last_evidence),
                         saved_progress=saved)
        if state["classification"] == "waiting_for_approval":
            state["waiting_since"] = state["progress"]
        return state

    def _sample_claude(self, tid, state, processes):
        cpu = None
        if state["transcripts"] is None:
            with self.store.reading():
                state["transcripts"] = claude_progress.TranscriptProgress(native_id(self.store, tid))
        latest = state["transcripts"].sample()
        connection = self.sessions.claude.connections.get(tid)
        pid = getattr(getattr(connection, "process", None), "pid", None)
        if pid:
            cpu = claude_progress.tool_cpu_times(processes(), pid)
        return latest, cpu

    def _check(self, tid, processes):
        s = self.sessions
        if self.stopped.is_set() or s.closed:
            return
        with s.lock:
            if tid not in s.loaded:
                return  # Never take ownership from another runtime.
            turn_id = s.active.get(tid)
            if tid in s.active and turn_id is None:
                return  # A bounded turn/start RPC is still in flight.
            turn_id = turn_id or self.store.threads.get(tid, history=False)["turn_id"]
            if not turn_id:
                return
            state = self.observed.get(tid)
            if not state or state["turn_id"] != turn_id:
                state = self.observed[tid] = self._restore(tid, turn_id)
            pending = [request for request in s.pending_requests(tid)
                       if request.get("params", {}).get("turnId") in (None, turn_id)]
            if pending:
                wall = self.store.clock()
                oldest = min((r["received_at"] for r in pending if isinstance(r.get("received_at"), (int, float))),
                             default=wall)
                wait_age = max(0, wall - oldest)
                if state["waiting_since"] is None:
                    since = self.clock() - wait_age
                    if state["next_alert_at"] is not None:
                        self._progress(tid, state, since, oldest)
                    state.update(waiting_since=since, progress=since, progress_at=oldest, state="waiting", detail=None)
            elif state["waiting_since"] is not None:
                state["waiting_since"] = None
                self._progress(tid, state, self.clock(), self.store.clock())
            quiet = self.clock() - state["progress"]
            ghost = tid not in s.active
            if not ghost and quiet < self.probe_after:
                return
            if state["probe"] is not None and self.clock() - state["probe"] < self.probe_interval:
                return
            state["probe"] = self.clock()
            revision = s.turn_revisions.get(tid, 0)
            progress = state["progress"]
        if pending:
            if wait_age >= self.alert_after and self._gated(tid) not in {"stop", "closed", "closing"}:
                self._alert(tid, state, f"The provider has waited {int(wait_age)} seconds for an approval or answer.",
                            "waiting_for_approval", progress)
            return

        cpu = None
        cpu_delta = None
        if s.loaded_clients.get(tid) == "claude":
            latest, cpu = self._sample_claude(tid, state, processes)
            with s.lock:
                if self.observed.get(tid) is not state or state["progress"] != progress:
                    return
                if cpu is not None:
                    previous = state["tool_cpu"]
                    state["tool_cpu"] = cpu
                    if previous is not None:
                        if cpu.keys() == previous.keys():
                            cpu_delta = sum(max(0, value - previous[pid]) for pid, value in cpu.items())
                        elif not cpu.keys() & previous.keys():
                            # An observed start or exit changes the evidence. Give
                            # the new condition its own observation interval.
                            self._progress(tid, state, self.clock(), self.store.clock())
                            if not ghost:
                                return
                            progress = state["progress"]
                            quiet = 0
                if latest is not None:
                    wall = self.store.clock()
                    observed = self.clock() - max(0, wall - latest)
                    if observed > progress:
                        self._progress(tid, state, observed, min(wall, latest))
                        progress = observed
                        quiet = self.clock() - observed
                        if not ghost and quiet < self.probe_after:
                            return
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
        if gate in {"stop", "closed", "closing"}:
            return
        threshold = self.alert_after
        if cpu:
            classification = "long_running_tool"
            detail = f"Long-running tool with {len(cpu)} live tool process(es)."
            if cpu_delta is None:
                detail += f" Their cumulative CPU time is {sum(cpu.values()):.2f} seconds."
            else:
                detail += f" They used {cpu_delta:.2f} CPU seconds since the previous sample."
            threshold = self.tool_alert_after
        elif cpu == {}:
            classification = "stalled"
            detail = "Claude has no live tool process and no observed native transcript growth."
        elif tools:
            classification = "unverified_tool"
            detail = "A tool may still be running; its outcome is unknown."
            threshold = self.tool_alert_after
        else:
            classification = "stalled"
            detail = "Provider state reconciliation did not establish completion or restored progress."
        if quiet >= threshold:
            detail = f"No observed output for {int(quiet)} seconds. {detail}"
            if error:
                detail += f" Last probe error: {error[:1000]}"
            self._alert(tid, state, detail, classification, progress)
        elif active and not gate and not tools and not cpu and not error and quiet >= self.probe_after:
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

    def _save_progress(self):
        now = self.clock()
        if self.last_progress_flush is not None and now - self.last_progress_flush < self.probe_interval:
            return
        with self.sessions.lock:
            changed = [(tid, state, state["progress_at"]) for tid, state in self.observed.items()
                       if state["turn_id"] and state["saved_progress"] != state["progress_at"]]
        if changed:
            with self.store.database_operation("watchdog.save_progress", priority="background"), self.store._connection() as db:
                db.executemany("""INSERT INTO browser_turn_health (thread_id, turn_id, last_progress_at)
                    VALUES (?, ?, ?) ON CONFLICT(thread_id, turn_id) DO UPDATE SET last_progress_at=excluded.last_progress_at
                    WHERE last_progress_at IS NULL OR last_progress_at < excluded.last_progress_at""",
                    [(tid, state["turn_id"], wall) for tid, state, wall in changed])
            with self.sessions.lock:
                for _, state, wall in changed:
                    state["saved_progress"] = wall
        self.last_progress_flush = now

    def process_once(self):
        s = self.sessions
        if s.closed:
            return
        try:
            process_failure_followups(self.store, s.cwd)
        except Exception:
            logging.getLogger(__name__).exception("Agent recovery deadline scan failed")
        try:
            from .workspace_roles import process as process_supervision
            if process_supervision(self.store, s.cwd):
                s._publish("browser/changed", {})
        except Exception:
            logging.getLogger(__name__).exception("Supervisor succession scan failed")
        with s.lock:
            candidates = set(s.active)
            loaded = set(s.loaded)
        # Find stale durable flags too, including flags rewritten by late hooks
        # after a provider completion. Do not rely on tool age as turn liveness.
        with self.store.reading(), self.store._connection() as db:
            candidates.update(r[0] for r in db.execute("SELECT session_id FROM sessions WHERE turn_active = 1") if r[0] in loaded)
        @lru_cache(maxsize=1)
        def processes():
            connections = s.claude.connections.copy()
            roots = {getattr(connection.process, "pid", None) for tid, connection in connections.items() if tid in candidates}
            roots.discard(None)
            return claude_progress.read_process_table(roots)

        for tid in sorted(candidates):
            if not s.persistence.flush(timeout=0, key=tid):
                continue
            lock = s._thread_lock(tid)
            if not lock.acquire(blocking=False):
                continue  # Stop, send, and other user operations take priority.
            try:
                self._check(tid, processes)
            except Exception:
                logging.getLogger(__name__).exception("Turn watchdog failed for %s", tid)
            finally:
                lock.release()

        self._save_progress()

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
