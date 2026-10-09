"""Isolated routing receipts and deferred dispatcher context.

Planning does not deliver work. Keep this database separate from the app store.
The future app adapter must confirm_seen only after provider acceptance, and
must recheck live delivery guards before executing any saved decision.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .dispatch_routing import RoutingPolicy, classify, route, validate_request


class RoutingHistory:
    def __init__(self, path):
        self.path = Path(path)
        with self._db() as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if names - {"dispatch_receipts", "dispatch_cursors"}:
                raise ValueError("Routing history requires a separate database")
            db.executescript("""CREATE TABLE IF NOT EXISTS dispatch_receipts (
                id INTEGER PRIMARY KEY, dispatcher_id TEXT NOT NULL, request_id TEXT NOT NULL,
                request_hash TEXT NOT NULL, request_json TEXT NOT NULL, decision_json TEXT NOT NULL,
                created_at REAL NOT NULL, UNIQUE(dispatcher_id, request_id));
                CREATE TABLE IF NOT EXISTS dispatch_cursors (
                dispatcher_id TEXT PRIMARY KEY, seen_id INTEGER NOT NULL DEFAULT 0);
            """)

    def _db(self):
        # closing also closes on exceptions; sqlite's context manager only commits.
        return closing(sqlite3.connect(self.path, timeout=10, isolation_level=None))

    def record(self, request, decision):
        validate_request(request)
        payload = json.dumps(request, sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id,request_hash,decision_json FROM dispatch_receipts WHERE dispatcher_id=? AND request_id=?",
                                  (request["dispatcher_id"], request["request_id"])).fetchone()
            if existing:
                if existing[1] != fingerprint:
                    raise ValueError("Request ID already belongs to different input")
                return {"id": existing[0], "decision": json.loads(existing[2]), "replayed": True}
            cursor = db.execute("""INSERT INTO dispatch_receipts
                (dispatcher_id,request_id,request_hash,request_json,decision_json,created_at) VALUES (?,?,?,?,?,?)""",
                (request["dispatcher_id"], request["request_id"], fingerprint, payload, json.dumps(decision), time.time()))
            db.execute("COMMIT")
            return {"id": cursor.lastrowid, "decision": decision, "replayed": False}

    def recent_routes(self, dispatcher_id, *, limit=20):
        """Selected plans inform later input even before a destination has run."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Route limit must be 1–100")
        with self._db() as db:
            rows = db.execute("""SELECT request_json,decision_json,created_at FROM dispatch_receipts
                WHERE dispatcher_id=? ORDER BY id DESC LIMIT ?""", (dispatcher_id, limit)).fetchall()
        result = []
        for payload, selected, created_at in reversed(rows):
            request, decision = json.loads(payload), json.loads(selected)
            if decision["action"] not in {"new", "reuse"}:
                continue
            result.append({"request_id": request["request_id"], "message": request["message"],
                           "cwd": request.get("cwd"), "thread_id": decision.get("thread_id"),
                           "creation_request_id": decision.get("creation_request_id"),
                           "timestamp": created_at, "status": "planned"})
        return result

    def plan(self, snapshot, request, config=None, policy=RoutingPolicy(), *, classifier=classify):
        """End-to-end preview: catch up routing evidence, decide once, save receipt.

        Repeated requests return their selected plan without another Jev call.
        Concurrent classifiers may finish, but the first committed decision wins.
        This method cannot deliver a message or create an app conversation.
        """
        validate_request(request)
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self._db() as db:
            existing = db.execute("SELECT id,request_hash,decision_json FROM dispatch_receipts WHERE dispatcher_id=? AND request_id=?",
                                  (request["dispatcher_id"], request["request_id"])).fetchone()
        if existing:
            if existing[1] != fingerprint:
                raise ValueError("Request ID already belongs to different input")
            return {"request_id": request["request_id"], "receipt_id": existing[0],
                    "decision": json.loads(existing[2]), "replayed": True}
        evaluation = {**request, "recent_routes": self.recent_routes(request["dispatcher_id"]) + request.get("recent_routes", [])}
        result = route(snapshot, evaluation, config, policy, classifier=classifier)
        if result["decision"]["action"] == "new":
            identity = json.dumps([request["dispatcher_id"], request["request_id"]]).encode()
            result["decision"]["creation_request_id"] = "dispatch-" + hashlib.sha256(identity).hexdigest()[:32]
        receipt = self.record(request, result["decision"])
        return {**result, "decision": receipt["decision"], "receipt_id": receipt["id"], "replayed": receipt["replayed"]}

    def catch_up(self, dispatcher_id, *, limit=20):
        """Preparing a batch does not consume it. Page until has_more is false."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Catch-up limit must be 1–100")
        with self._db() as db:
            cursor = db.execute("SELECT seen_id FROM dispatch_cursors WHERE dispatcher_id=?", (dispatcher_id,)).fetchone()
            seen = cursor[0] if cursor else 0
            rows = db.execute("""SELECT id,request_json,decision_json FROM dispatch_receipts
                WHERE dispatcher_id=? AND id>? ORDER BY id LIMIT ?""", (dispatcher_id, seen, limit + 1)).fetchall()
        return {"after_id": seen, "through_id": rows[min(len(rows), limit) - 1][0] if rows else seen,
                "has_more": len(rows) > limit,
                "instruction": "These are already decided routing records, not new work to dispatch. Delivery is not implied by a plan.",
                "events": [{"id": r[0], "request": json.loads(r[1]), "decision": json.loads(r[2])} for r in rows[:limit]]}

    def confirm_seen(self, dispatcher_id, through_id):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM dispatch_receipts WHERE dispatcher_id=? AND id=?", (dispatcher_id, through_id)).fetchone():
                raise ValueError("Unknown catch-up boundary")
            db.execute("""INSERT INTO dispatch_cursors VALUES (?,?) ON CONFLICT(dispatcher_id)
                DO UPDATE SET seen_id=MAX(seen_id,excluded.seen_id)""", (dispatcher_id, through_id))
            db.execute("COMMIT")
