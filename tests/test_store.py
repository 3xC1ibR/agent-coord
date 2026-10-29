from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

PLUGIN_SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from agent_coord.store import (
    AmbiguousTargetError,
    ConflictError,
    CoordinationError,
    CoordinationStore,
    InboxTimeoutError,
    path_is_in_scope,
    scopes_overlap,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


class CoordinationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clock = Clock()
        self.store = CoordinationStore(
            self.root / "state.sqlite3",
            stale_after_seconds=60,
            clock=self.clock,
        )

    def register(self, session_id: str, client: str = "codex") -> None:
        self.store.register(
            session_id=session_id,
            client=client,
            cwd=str(self.root),
            name=session_id,
        )

    def legacy_wake(self, session_id: str) -> None:
        self.register(session_id)
        with self.store._connection() as db:
            db.execute("""INSERT INTO zellij_wake_targets
                (session_id, zellij_session, pane_id, enabled, registered_at, updated_at)
                VALUES (?, 'legacy-session', 'terminal_7', 1, 1, 1)""", (session_id,))

    def test_startup_retries_real_migration_lock_before_sending_once(self) -> None:
        from agent_coord.cli import main

        self.register("sender")
        self.legacy_wake("recipient")
        with closing(sqlite3.connect(self.store.database_path)) as writer:
            connect = CoordinationStore._connect
            locked = []

            def trace(sql):
                if "INSERT OR IGNORE INTO wake_targets" in sql and not locked:
                    writer.execute("BEGIN IMMEDIATE")
                    locked.append(True)

            def connection(store):
                db = connect(store)
                # Exercise the actual SQLite BUSY path without a five-second test.
                db.execute("PRAGMA busy_timeout = 1")
                db.set_trace_callback(trace)
                return db

            output = io.StringIO()
            with patch.object(CoordinationStore, "_connect", connection), \
                 patch("agent_coord.store.time.sleep", side_effect=lambda _: writer.rollback()) as retry, \
                 redirect_stdout(output):
                code = main(["--db", str(self.store.database_path), "send", "--from-session", "sender",
                             "--session", "recipient", "--no-reply-required", "Candidate ready"])
            self.assertEqual(code, 0)
            self.assertEqual(len(locked), 1)
            retry.assert_called_once()
        messages = self.store.inbox("recipient", include_delivered=True)
        self.assertEqual([message["body"] for message in messages], ["Candidate ready"])
        self.assertEqual(json.loads(output.getvalue())["id"], messages[0]["id"])
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM wake_targets").fetchone()[0], 1)

    def test_startup_retries_auxiliary_store_contention(self) -> None:
        from agent_coord.asks import AskStore

        with patch("agent_coord.asks.AskStore", side_effect=[
            sqlite3.OperationalError("database is locked"), AskStore(self.store)
        ]) as initialize, patch("agent_coord.store.time.sleep") as retry:
            reopened = CoordinationStore(self.store.database_path)
        self.assertEqual(initialize.call_count, 2)
        retry.assert_called_once()
        self.assertEqual(reopened.asks.list(), [])

    def test_startup_lock_exhaustion_returns_cli_error_without_sending(self) -> None:
        from agent_coord.cli import main

        self.register("sender")
        self.register("recipient")
        error = io.StringIO()
        with patch.object(CoordinationStore, "_initialize", side_effect=sqlite3.OperationalError(
            "database is locked"
        )) as initialize, patch("agent_coord.store.time.sleep") as retry, redirect_stderr(error):
            code = main(["--db", str(self.store.database_path), "send", "--from-session", "sender",
                         "--session", "recipient", "--no-reply-required", "Candidate ready"])
        self.assertEqual(code, 2)
        self.assertEqual(initialize.call_count, 3)
        self.assertEqual(retry.call_count, 2)
        self.assertIn("after 3 attempts", json.loads(error.getvalue())["error"])
        self.assertEqual(self.store.inbox("recipient", include_delivered=True), [])

    def test_startup_does_not_retry_non_lock_errors(self) -> None:
        error = sqlite3.OperationalError("no such table: missing")
        with patch.object(CoordinationStore, "_initialize", side_effect=error) as initialize, \
             patch("agent_coord.store.time.sleep") as retry:
            with self.assertRaises(sqlite3.OperationalError) as raised:
                CoordinationStore(self.store.database_path)
        self.assertIs(raised.exception, error)
        initialize.assert_called_once()
        retry.assert_not_called()

    def test_startup_skips_migrated_wake_targets_and_preserves_current_settings(self) -> None:
        self.legacy_wake("recipient")
        CoordinationStore(self.store.database_path)
        with self.store._connection() as db:
            migrated = db.execute("SELECT * FROM wake_targets WHERE session_id = 'recipient'").fetchone()
            self.assertEqual(json.loads(migrated["endpoint_json"]), {
                "zellij_session": "legacy-session", "pane_id": "terminal_7"
            })
            db.execute("UPDATE wake_targets SET enabled = 0, endpoint_json = ? WHERE session_id = 'recipient'",
                       (json.dumps({"zellij_session": "current", "pane_id": "terminal_9"}),))
        connect = CoordinationStore._connect

        def deny_wake_insert(action, table, *_):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == "wake_targets" else sqlite3.SQLITE_OK

        def connection(store):
            db = connect(store)
            db.set_authorizer(deny_wake_insert)
            return db

        with patch.object(CoordinationStore, "_connect", connection):
            CoordinationStore(self.store.database_path)
        with self.store._connection() as db:
            current = db.execute("SELECT * FROM wake_targets WHERE session_id = 'recipient'").fetchone()
        self.assertEqual(current["enabled"], 0)
        self.assertEqual(json.loads(current["endpoint_json"])["zellij_session"], "current")

    def test_legacy_bead_constraints_migrate_without_losing_history(self) -> None:
        self.register("parent")
        self.register("child")
        message = self.store.send_message(
            sender_session_id="parent", recipient_session_id="child", body="Legacy handoff.",
        )
        with self.store._connection() as connection:
            connection.executescript("""
                DROP TABLE handoffs;
                DROP TABLE delegations;
                CREATE TABLE delegations (
                    delegation_id TEXT PRIMARY KEY, parent_session_id TEXT NOT NULL,
                    child_session_id TEXT, client TEXT NOT NULL, cwd TEXT NOT NULL,
                    bead_id TEXT NOT NULL, write_scope_json TEXT NOT NULL,
                    instructions TEXT NOT NULL, status TEXT NOT NULL,
                    zellij_session TEXT, pane_id TEXT, mode TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    completed_at REAL, result_message TEXT, error TEXT,
                    model TEXT, token_usage_json TEXT,
                    FOREIGN KEY(parent_session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(child_session_id) REFERENCES sessions(session_id)
                );
                CREATE INDEX legacy_delegation_model_idx ON delegations(model);
                CREATE TABLE handoffs (
                    handoff_id TEXT PRIMARY KEY, sender_session_id TEXT NOT NULL,
                    recipient_session_id TEXT NOT NULL, source_bead_id TEXT NOT NULL,
                    target_bead_id TEXT NOT NULL, scope_json TEXT NOT NULL,
                    patch_label TEXT NOT NULL, validation_boundary TEXT NOT NULL,
                    validation_responsibility TEXT NOT NULL, mode TEXT NOT NULL,
                    thread_id TEXT NOT NULL, notification_message_id INTEGER NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(sender_session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(recipient_session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(thread_id) REFERENCES message_threads(thread_id),
                    FOREIGN KEY(notification_message_id) REFERENCES messages(id)
                );
            """)
            connection.execute("""
                INSERT INTO delegations (
                    delegation_id, parent_session_id, child_session_id, client, cwd,
                    bead_id, write_scope_json, instructions, status, mode,
                    created_at, updated_at, model, token_usage_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "legacy-delegation", "parent", "child", "codex", str(self.root.resolve()),
                "work-a", '["src/**"]', "Existing work.", "attached", "reviewed",
                self.clock(), self.clock(), "existing-model", '{"total_tokens":42}',
            ))
            connection.execute("""
                INSERT INTO handoffs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "legacy-handoff", "parent", "child", "work-before", "work-a",
                '["src/**"]', "existing-patch", "unit tests passed", "full suite",
                "validation", message["thread_id"], message["id"], self.clock(),
            ))

        for _ in range(2):
            migrated = CoordinationStore(self.store.database_path, clock=self.clock)
            delegation = migrated.get_delegation("legacy-delegation")
            handoff = migrated.get_handoff("legacy-handoff")
            self.assertEqual(delegation["bead_id"], "work-a")
            self.assertEqual(delegation["model"], "existing-model")
            self.assertEqual(delegation["token_usage"], {"total_tokens": 42})
            self.assertEqual(handoff["source_bead_id"], "work-before")
            self.assertEqual(handoff["target_bead_id"], "work-a")
            self.assertEqual(handoff["notification_message_id"], message["id"])
            with migrated._connection() as connection:
                for table, fields in (
                    ("delegations", {"bead_id"}),
                    ("handoffs", {"source_bead_id", "target_bead_id"}),
                ):
                    columns = list(connection.execute(f"PRAGMA table_info({table})"))
                    self.assertFalse(any(row["notnull"] for row in columns if row["name"] in fields))
                indexes = {row["name"] for row in connection.execute("PRAGMA index_list(delegations)")}
                self.assertIn("legacy_delegation_model_idx", indexes)
                self.assertIn("delegations_active_work_idx", indexes)
                self.assertEqual(list(connection.execute("PRAGMA foreign_key_check")), [])

        with self.assertRaisesRegex(CoordinationError, "active delegation"):
            migrated.create_delegation(
                parent_session_id="parent", cwd=str(self.root), bead_id="work-a",
                scopes=["tests/**"], instructions="Duplicate issue.", mode="reviewed",
            )
        untracked = migrated.create_delegation(
            parent_session_id="parent", cwd=str(self.root), scopes=["docs/**"],
            instructions="Work without an issue.", mode="reviewed",
        )
        self.assertIsNone(untracked["bead_id"])

    def test_existing_database_adds_turn_activity_column(self) -> None:
        database = self.root / "legacy.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(
                """
                CREATE TABLE sessions (
                    session_id TEXT PRIMARY KEY,
                    client TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    name TEXT,
                    activity TEXT NOT NULL,
                    bead_id TEXT,
                    write_scope_json TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    ended_at REAL
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_session_id TEXT NOT NULL,
                    recipient_session_id TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    delivered_at REAL,
                    acknowledged_at REAL
                );
                INSERT INTO sessions VALUES (
                    'legacy-sender', 'codex', '/tmp/repo', NULL, 'idle', NULL,
                    '[]', 1, 1, NULL
                );
                INSERT INTO sessions VALUES (
                    'legacy-recipient', 'claude', '/tmp/repo', NULL, 'idle', NULL,
                    '[]', 1, 1, NULL
                );
                INSERT INTO messages (
                    sender_session_id, recipient_session_id, body, created_at
                ) VALUES ('legacy-sender', 'legacy-recipient', 'legacy body', 1);
                """
            )

        migrated = CoordinationStore(database, clock=self.clock)
        migrated.register(session_id="legacy", client="codex", cwd=str(self.root))

        self.assertFalse(migrated.get_session("legacy")["turn_active"])
        self.assertEqual(migrated.get_session("legacy")["lease_mode"], "write")
        self.assertFalse(migrated.get_session("legacy")["scope_required"])
        legacy_message = migrated.inbox(
            "legacy-recipient", include_delivered=True, mark_delivered=False
        )[0]
        self.assertEqual(legacy_message["classification"], "action_required")
        self.assertEqual(legacy_message["thread_id"], f"legacy:{legacy_message['id']}")
        self.assertTrue(legacy_message["reply_required"])
        self.assertIsNone(legacy_message["in_reply_to"])

        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                """
                INSERT INTO messages (
                    sender_session_id, recipient_session_id, body, created_at
                ) VALUES ('legacy-sender', 'legacy-recipient', 'old writer', 2)
                """
            )
        remigrated = CoordinationStore(database, clock=self.clock)
        history = remigrated.inbox(
            "legacy-recipient", include_delivered=True, mark_delivered=False
        )
        self.assertEqual(
            [message["thread_id"] for message in history],
            [f"legacy:{message['id']}" for message in history],
        )

    def test_existing_delegation_database_adds_runtime_columns(self) -> None:
        database = self.root / "legacy-delegations.sqlite3"
        legacy = CoordinationStore(database, clock=self.clock)
        legacy.register(session_id="parent", client="codex", cwd=str(self.root))
        legacy.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-a",
            scopes=["src/**"],
            instructions="Implement work-a.",
            mode="reviewed",
            delegation_id="delegation-a",
        )
        with closing(sqlite3.connect(database)) as connection:
            for column in (
                "lease_mode",
                "token_usage_json",
                "token_usage_captured_at",
                "token_usage_capture_event",
                "token_usage_artifact_path",
                "token_usage_error",
                "runtime_kind",
                "supervisor_pid",
                "child_pid",
                "output_log_path",
                "runtime_started_at",
                "runtime_exited_at",
                "runtime_exit_code",
                "name",
            ):
                connection.execute(f"ALTER TABLE delegations DROP COLUMN {column}")

        migrated = CoordinationStore(database, clock=self.clock)

        self.assertEqual(
            migrated.get_delegation("delegation-a")["lease_mode"], "write"
        )
        self.assertIsNone(migrated.get_delegation("delegation-a")["token_usage"])
        self.assertEqual(
            migrated.get_delegation("delegation-a")["runtime_kind"], "zellij"
        )

    def test_generic_wake_target_supports_managed_pty(self) -> None:
        self.register("two")
        target = self.store.register_wake_target(
            session_id="two",
            transport="managed-pty",
            endpoint={"delegation_id": "delegation-a", "child_pid": 42},
            watcher_pid=41,
        )

        self.assertEqual(target["transport"], "managed-pty")
        self.assertEqual(target["endpoint"]["child_pid"], 42)
        self.assertEqual(target["watcher_pid"], 41)
        self.assertFalse(self.store.disable_wake("two")["enabled"])

        with self.assertRaisesRegex(CoordinationError, "Unsupported wake transport"):
            self.store.register_wake_target(
                session_id="two",
                transport="screen",
                endpoint={"session": "legacy"},
            )

    def test_session_lifecycle_and_staleness(self) -> None:
        self.register("one")
        session = self.store.touch("one", "planning")
        self.assertEqual(session["presence"], "online")
        self.assertEqual(session["activity"], "planning")

        self.clock.now += 61
        self.assertEqual(self.store.get_session("one")["presence"], "stale")

        self.store.end_session("one")
        self.assertEqual(self.store.get_session("one")["presence"], "offline")

    def test_turn_activity_tracks_model_work_separately(self) -> None:
        self.register("one")

        active = self.store.touch("one", "planning", turn_active=True)
        stopped = self.store.touch("one", "waiting", turn_active=False)

        self.assertTrue(active["turn_active"])
        self.assertFalse(stopped["turn_active"])

    def test_scope_only_declarations_reject_overlaps_without_beads(self) -> None:
        self.register("one")
        self.register("two", "claude")

        first = self.store.begin_work(session_id="one", scopes=["src/**"])

        self.assertIsNone(first["bead_id"])
        self.assertEqual(first["write_scope"], ["src/**"])
        with self.assertRaises(ConflictError):
            self.store.begin_work(session_id="two", scopes=["src/api/**"])

    def test_scope_requests_are_deduplicated_and_resolved_by_declaration(self) -> None:
        self.register("incumbent")
        self.register("newcomer", "claude")
        self.store.touch("incumbent", "implementing", turn_active=False)
        self.store.touch("newcomer", "discussing", turn_active=True)

        blockers = self.store.scope_blocking_peers("newcomer")
        first = self.store.request_scope_declarations(
            "newcomer", [peer["session_id"] for peer in blockers]
        )
        second = self.store.request_scope_declarations(
            "newcomer", [peer["session_id"] for peer in blockers]
        )

        self.assertEqual([peer["session_id"] for peer in blockers], ["incumbent"])
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertTrue(self.store.get_session("incumbent")["scope_required"])
        self.assertFalse(first[0]["reply_required"])
        with self.assertRaisesRegex(CoordinationError, "Wait for the requested"):
            self.store.begin_work(session_id="newcomer", scopes=["tests/**"])

        declared = self.store.begin_work(
            session_id="incumbent", scopes=["src/**"]
        )

        self.assertFalse(declared["scope_required"])
        self.assertEqual(
            self.store.inbox("incumbent", unread_only=True, mark_delivered=False),
            [],
        )
        newcomer = self.store.begin_work(
            session_id="newcomer", scopes=["tests/**"]
        )
        self.assertEqual(newcomer["write_scope"], ["tests/**"])

    def test_validation_lease_is_exclusive_and_sets_validating_activity(self) -> None:
        self.register("one")
        self.register("two", "claude")

        lease = self.store.begin_work(
            session_id="one",
            bead_id="work-a",
            scopes=["src/**"],
            lease_mode="validation",
        )

        self.assertEqual(lease["lease_mode"], "validation")
        self.assertEqual(lease["activity"], "validating")
        with self.assertRaises(ConflictError):
            self.store.begin_work(
                session_id="two", bead_id="work-b", scopes=["src/api/**"]
            )

    def test_overlapping_scope_blocks_second_session(self) -> None:
        self.register("one")
        self.register("two", "claude")
        self.store.begin_work(session_id="one", bead_id="work-a", scopes=["src/**"])

        with self.assertRaises(ConflictError) as caught:
            self.store.begin_work(
                session_id="two", bead_id="work-b", scopes=["src/api/**"]
            )

        self.assertEqual(caught.exception.conflicts[0]["session_id"], "one")
        self.assertTrue(caught.exception.conflicts[0]["overlaps"])
        self.assertIsNone(self.store.get_session("two")["bead_id"])

    def test_same_bead_blocks_disjoint_scope(self) -> None:
        self.register("one")
        self.register("two", "claude")
        self.store.begin_work(session_id="one", bead_id="work-a", scopes=["src/**"])

        with self.assertRaises(ConflictError) as caught:
            self.store.begin_work(
                session_id="two", bead_id="work-a", scopes=["tests/**"]
            )

        self.assertTrue(caught.exception.conflicts[0]["same_bead"])

    def test_disjoint_work_and_relevant_filter(self) -> None:
        self.register("one")
        self.register("two", "claude")
        self.register("reference")
        self.store.begin_work(session_id="one", bead_id="work-a", scopes=["src/**"])
        self.store.begin_work(session_id="two", bead_id="work-b", scopes=["tests/**"])

        relevant = self.store.list_sessions(cwd=str(self.root), relevant_only=True)
        self.assertEqual({item["session_id"] for item in relevant}, {"one", "two"})

    def test_relevant_filter_includes_running_idle_and_discussing_sessions(self) -> None:
        self.register("one")
        self.register("reference")
        for activity in ("idle", "discussing"):
            with self.subTest(activity=activity):
                self.store.touch("one", activity, turn_active=True)
                relevant = self.store.list_sessions(cwd=str(self.root), relevant_only=True)
                self.assertEqual({item["session_id"] for item in relevant}, {"one"})
                self.store.touch("one", turn_active=False)
                self.assertEqual(
                    self.store.list_sessions(cwd=str(self.root), relevant_only=True), []
                )

    def test_relevant_filter_keeps_running_session_after_end_work(self) -> None:
        self.register("one")
        self.store.begin_work(session_id="one", scopes=["dummy-experiment.txt"])
        self.store.touch("one", turn_active=True)
        self.store.end_work("one")

        relevant = self.store.list_sessions(cwd=str(self.root), relevant_only=True)
        self.assertEqual([item["session_id"] for item in relevant], ["one"])
        self.assertEqual(relevant[0]["activity"], "idle")
        self.assertEqual(relevant[0]["write_scope"], [])
        self.assertIsNone(relevant[0]["bead_id"])

    def test_relevant_filter_includes_scope_without_activity_or_bead(self) -> None:
        self.register("one")
        self.store.begin_work(session_id="one", scopes=["src/**"])
        self.store.touch("one", "idle", turn_active=False)

        relevant = self.store.list_sessions(cwd=str(self.root), relevant_only=True)
        self.assertEqual([item["session_id"] for item in relevant], ["one"])
        self.assertEqual(relevant[0]["write_scope"], ["src/**"])
        self.assertIsNone(relevant[0]["bead_id"])
        self.store.end_work("one")
        self.assertEqual(
            self.store.list_sessions(cwd=str(self.root), relevant_only=True), []
        )

    def test_relevant_filter_preserves_cwd_and_offline_filters(self) -> None:
        self.register("one")
        self.store.touch("one", "discussing", turn_active=True)
        self.store.register(session_id="elsewhere", client="codex", cwd=str(self.root / "other"))
        self.store.touch("elsewhere", turn_active=True)
        self.register("ended")
        self.store.begin_work(session_id="ended", scopes=["src/**"])
        self.store.end_session("ended")

        relevant = self.store.list_sessions(cwd=str(self.root), relevant_only=True)
        self.assertEqual({item["session_id"] for item in relevant}, {"one"})
        including_offline = self.store.list_sessions(
            cwd=str(self.root), relevant_only=True, include_offline=True
        )
        self.assertEqual({item["session_id"] for item in including_offline}, {"one", "ended"})

    def test_durable_message_delivery_and_acknowledgement(self) -> None:
        self.register("one")
        self.register("two", "claude")
        self.store.begin_work(session_id="two", bead_id="work-b", scopes=["tests/**"])

        message = self.store.send_message(
            sender_session_id="one",
            recipient_bead_id="work-b",
            body="Are you changing the fixture?",
        )
        first = self.store.inbox("two")
        second = self.store.inbox("two")

        self.assertEqual(first[0]["id"], message["id"])
        self.assertEqual(second, [])
        self.assertEqual(len(self.store.inbox("two", include_delivered=True)), 1)
        acknowledged = self.store.acknowledge("two", message["id"])
        self.assertIsNotNone(acknowledged["acknowledged_at"])

    def test_message_classification_thread_closure_and_reopen(self) -> None:
        self.register("one")
        self.register("two", "claude")
        self.store.register_zellij_wake(
            session_id="two", zellij_session="test-session", pane_id="terminal_7"
        )

        informational = self.store.send_message(
            sender_session_id="one",
            recipient_session_id="two",
            body="FYI",
            classification="informational",
            thread_id="thread-a",
        )
        closure = self.store.send_message(
            sender_session_id="one",
            recipient_session_id="two",
            body="Finished",
            classification="closure",
            thread_id="thread-a",
        )
        repeated = self.store.send_message(
            sender_session_id="one",
            recipient_session_id="two",
            body="Finished again",
            classification="closure",
            thread_id="thread-a",
        )

        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["id"], closure["id"])
        self.assertEqual(self.store.pending_wake_message_ids("two"), [])
        self.assertIsNotNone(closure["delivered_at"])

        reopened = self.store.send_message(
            sender_session_id="one",
            recipient_session_id="two",
            body="Validation failed; act again",
            classification="action_required",
            thread_id="thread-a",
        )
        self.assertEqual(self.store.pending_wake_message_ids("two"), [reopened["id"]])
        history = self.store.inbox(
            "two", include_delivered=True, mark_delivered=False
        )
        self.assertEqual(
            [message["id"] for message in history],
            [informational["id"], closure["id"], reopened["id"]],
        )

    def test_message_reply_requirements_default_by_classification_and_opt_out(self) -> None:
        self.register("one")
        self.register("two", "claude")

        required = self.store.send_message(
            sender_session_id="one", recipient_session_id="two", body="Act"
        )
        optional = self.store.send_message(
            sender_session_id="one",
            recipient_session_id="two",
            body="Act without reply",
            reply_required=False,
        )
        informational = self.store.send_message(
            sender_session_id="one",
            recipient_session_id="two",
            body="FYI",
            classification="informational",
        )
        closure = self.store.send_message(
            sender_session_id="one",
            recipient_session_id="two",
            body="Complete",
            classification="closure",
        )

        self.assertTrue(required["reply_required"])
        self.assertFalse(optional["reply_required"])
        self.assertFalse(informational["reply_required"])
        self.assertFalse(closure["reply_required"])

    def supervised_worker(self):
        from agent_coord.app_control import AppControl
        for identity in ("lead", "successor", "worker"):
            self.register(identity)
        control = AppControl(self.store)
        request = control.request("create", "lead", {"cwd": str(self.root), "name": "Worker", "prompt": "Work"})
        control.claim(request["request_id"], "test")
        control.identified(request["request_id"], "worker")
        control.complete(request["request_id"], result={"thread_id": "worker"})

    def transfer_worker(self):
        from agent_coord.supervision import transfer
        self.store.end_session("lead")
        return transfer(self.store, requester="successor", from_session="lead", to_session="successor",
                        thread_ids=["worker"], request_id="test-transfer", reason="Replace lead", workspace=str(self.root))

    def test_transferred_reply_keeps_assignment_ancestry_and_exchange_pair_guard(self):
        self.supervised_worker()
        original = self.store.send_message(sender_session_id="lead", recipient_session_id="worker",
                                           body="Assignment", reply_required=True)
        self.store.inbox("worker")
        transfer = self.transfer_worker()
        with patch.object(self.store, "_insert_message", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                self.store.reply_message(sender_session_id="worker", message_id=original["id"], body="Done")
        pending = self.store.inbox("worker", unread_only=True, mark_delivered=False)
        self.assertIsNone(pending[0]["acknowledged_at"])
        self.assertEqual(self.store.inbox("successor"), [])
        with patch("agent_coord.sidecars.guard_message") as guard:
            reply = self.store.reply_message(sender_session_id="worker", message_id=original["id"], body="Done")
        self.assertEqual(guard.call_args.args[1:], ("worker", "successor"))
        self.assertEqual(reply["recipient_session_id"], "successor")
        self.assertEqual(reply["recipient_name"], "successor")
        self.assertEqual(reply["in_reply_to"], original["id"])
        self.assertNotEqual(reply["thread_id"], original["thread_id"])
        notices = self.store.inbox("worker", unread_only=True) + self.store.inbox("lead")
        self.assertCountEqual([m["id"] for m in notices], transfer["notice_message_ids"])
        for notice in notices:
            self.assertEqual(notice["classification"], "informational")
            self.assertFalse(notice["reply_required"])
        self.assertEqual([m["id"] for m in self.store.inbox("successor")], [reply["id"]])
        with self.assertRaisesRegex(CoordinationError, "participant pair"):
            self.store.send_message(sender_session_id="successor", recipient_session_id="worker",
                                    body="Unrelated third participant", thread_id=original["thread_id"])
        self.store.send_message(sender_session_id="successor", recipient_session_id="worker",
                                body="Follow up", thread_id=reply["thread_id"])

    def test_retired_supervisor_cannot_bypass_action_guard_with_reply(self):
        self.supervised_worker()
        question = self.store.send_message(sender_session_id="worker", recipient_session_id="lead",
                                          body="What next?", reply_required=True)
        self.transfer_worker()
        with self.assertRaisesRegex(CoordinationError, "now supervised by successor"):
            self.store.send_message(sender_session_id="lead", recipient_session_id="worker", body="Old lead instruction")
        with self.assertRaisesRegex(CoordinationError, "now supervised by successor"):
            self.store.reply_message(sender_session_id="lead", message_id=question["id"], body="Old lead instruction")
        self.assertIsNone(self.store.inbox("lead", unread_only=True, mark_delivered=False)[0]["acknowledged_at"])
        for classification in ("informational", "closure"):
            self.store.send_message(sender_session_id="lead", recipient_session_id="worker",
                                    body="History only", classification=classification, reply_required=False)
        self.store.send_message(sender_session_id="successor", recipient_session_id="worker", body="Current instruction")

    def test_reply_is_actionable_and_acknowledges_only_its_original_request(self) -> None:
        self.register("one")
        self.register("two", "claude")
        original = self.store.send_message(sender_session_id="one", recipient_session_id="two", body="Please confirm")
        other = self.store.send_message(sender_session_id="one", recipient_session_id="two", body="Another request",
                                        thread_id=original["thread_id"])
        reply = self.store.reply_message(sender_session_id="two", message_id=original["id"], body="  Received, thanks!  ")
        self.assertEqual(reply["recipient_session_id"], "one")
        self.assertEqual(reply["thread_id"], original["thread_id"])
        self.assertEqual(reply["in_reply_to"], original["id"])
        self.assertEqual(reply["body"], "Received, thanks!")
        self.assertEqual(reply["classification"], "action_required")
        self.assertFalse(reply["reply_required"])
        self.assertIsNone(reply["delivered_at"])
        self.assertIsNone(reply["acknowledged_at"])
        reopened = CoordinationStore(self.store.database_path, clock=self.clock)
        original_record, other_record = reopened.inbox("two", include_delivered=True, mark_delivered=False)
        self.assertEqual(original_record["acknowledged_at"], reply["created_at"])
        self.assertEqual(original_record["delivered_at"], reply["created_at"])
        self.assertIsNone(other_record["acknowledged_at"])
        self.assertIsNone(other_record["delivered_at"])
        self.assertEqual([m["id"] for m in reopened.inbox("two", unread_only=True, mark_delivered=False)], [other["id"]])
        with self.assertRaisesRegex(CoordinationError, "does not request a reply"):
            self.store.reply_message(sender_session_id="one", message_id=reply["id"], body="Thanks again")
        self.assertEqual(len(self.store.inbox("two", include_delivered=True, mark_delivered=False)), 2)

    def test_reply_preserves_existing_receipt_timestamps(self) -> None:
        self.register("one")
        self.register("two")
        for acknowledge_first in (False, True):
            with self.subTest(acknowledge_first=acknowledge_first):
                original = self.store.send_message(sender_session_id="one", recipient_session_id="two", body="Question")
                self.store.inbox("two")
                if acknowledge_first:
                    self.store.acknowledge("two", original["id"])
                self.clock.now += 10
                reply = self.store.reply_message(sender_session_id="two", message_id=original["id"], body="Answer")
                with self.store._connection() as db:
                    record = db.execute("SELECT delivered_at, acknowledged_at FROM messages WHERE id = ?", (original["id"],)).fetchone()
                self.assertEqual(record["delivered_at"], self.clock.now - 10)
                self.assertEqual(record["acknowledged_at"], self.clock.now - 10 if acknowledge_first else self.clock.now)

    def test_reply_and_acknowledgment_roll_back_together(self) -> None:
        self.register("one")
        self.register("two")
        original = self.store.send_message(sender_session_id="one", recipient_session_id="two", body="Question")
        with self.store._connection() as db:
            db.execute("""
                CREATE TRIGGER reject_ack BEFORE UPDATE OF acknowledged_at ON messages
                BEGIN SELECT RAISE(ABORT, 'ack failed'); END
            """)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "ack failed"):
            self.store.reply_message(sender_session_id="two", message_id=original["id"], body="Answer")
        self.assertEqual(self.store.inbox("one", include_delivered=True, mark_delivered=False), [])
        pending = self.store.inbox("two", unread_only=True, mark_delivered=False)
        self.assertEqual([m["id"] for m in pending], [original["id"]])
        self.assertIsNone(pending[0]["delivered_at"])

    def test_invalid_replies_do_not_write_messages(self) -> None:
        for session in ("one", "two", "three"):
            self.register(session)
        original = self.store.send_message(sender_session_id="one", recipient_session_id="two", body="Please confirm")
        cases = [("three", original["id"], "Wrong recipient"), ("one", original["id"], "Own message"),
                 ("two", 99999, "Unknown"), ("two", 0, "Zero"), ("two", -1, "Negative"),
                 ("two", True, "Boolean"), ("two", "1", "String"), ("two", original["id"], " ")]
        for sender, message_id, body in cases:
            with self.subTest(sender=sender, message_id=message_id, body=body), self.assertRaises(CoordinationError):
                self.store.reply_message(sender_session_id=sender, message_id=message_id, body=body)
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
            record = db.execute("SELECT delivered_at, acknowledged_at FROM messages WHERE id = ?", (original["id"],)).fetchone()
            self.assertIsNone(record["delivered_at"])
            self.assertIsNone(record["acknowledged_at"])

    def test_reply_cannot_reopen_closed_or_superseded_exchange(self) -> None:
        self.register("one")
        self.register("two")
        original = self.store.send_message(sender_session_id="one", recipient_session_id="two", body="Please confirm")
        self.store.send_message(sender_session_id="one", recipient_session_id="two", body="Finished",
                                classification="closure", thread_id=original["thread_id"])
        with self.assertRaisesRegex(CoordinationError, "closed or superseded"):
            self.store.reply_message(sender_session_id="two", message_id=original["id"], body="Too late")
        self.store.send_message(sender_session_id="one", recipient_session_id="two", body="New work",
                                thread_id=original["thread_id"])
        with self.assertRaisesRegex(CoordinationError, "closed or superseded"):
            self.store.reply_message(sender_session_id="two", message_id=original["id"], body="Stale request")
        self.assertEqual(self.store.inbox("one", include_delivered=True, mark_delivered=False), [])

    def test_closure_delivers_older_pending_action_and_later_action_reopens(self) -> None:
        self.register("one")
        self.register("two", "claude")
        self.store.register_zellij_wake(
            session_id="two", zellij_session="test-session", pane_id="terminal_7"
        )
        pending = self.store.send_message(
            sender_session_id="one",
            recipient_session_id="two",
            body="Please act",
            thread_id="thread-a",
        )
        closure = self.store.send_message(
            sender_session_id="two",
            recipient_session_id="one",
            body="Already resolved",
            classification="closure",
            thread_id="thread-a",
        )

        history = self.store.inbox(
            "two", include_delivered=True, mark_delivered=False
        )
        self.assertIsNotNone(history[0]["delivered_at"])
        self.assertEqual(history[0]["id"], pending["id"])
        self.assertTrue(closure["terminal"])
        self.assertEqual(self.store.pending_wake_message_ids("two"), [])

        reopened = self.store.send_message(
            sender_session_id="one",
            recipient_session_id="two",
            body="The issue reopened",
            thread_id="thread-a",
        )
        self.assertEqual(self.store.pending_wake_message_ids("two"), [reopened["id"]])

    def test_thread_reuse_requires_the_same_unordered_participant_pair(self) -> None:
        self.register("one")
        self.register("two", "claude")
        self.register("three")
        self.store.send_message(
            sender_session_id="one",
            recipient_session_id="two",
            body="Question",
            thread_id="thread-a",
        )
        reverse = self.store.send_message(
            sender_session_id="two",
            recipient_session_id="one",
            body="Answer",
            thread_id="thread-a",
        )
        closure = self.store.send_message(
            sender_session_id="two",
            recipient_session_id="one",
            body="Closed",
            classification="closure",
            thread_id="thread-a",
        )
        repeated_closure = self.store.send_message(
            sender_session_id="one",
            recipient_session_id="two",
            body="Also closed",
            classification="closure",
            thread_id="thread-a",
        )

        self.assertEqual(reverse["thread_id"], "thread-a")
        self.assertTrue(repeated_closure["idempotent"])
        self.assertEqual(repeated_closure["id"], closure["id"])
        with self.assertRaisesRegex(CoordinationError, "participant pair"):
            self.store.send_message(
                sender_session_id="three",
                recipient_session_id="two",
                body="Third party",
                thread_id="thread-a",
            )

    def test_explicit_unread_and_bulk_ack_are_transport_silent(self) -> None:
        self.register("one")
        self.register("two", "claude")
        first = self.store.send_message(
            sender_session_id="one", recipient_session_id="two", body="one"
        )
        second = self.store.send_message(
            sender_session_id="one",
            recipient_session_id="two",
            body="two",
            classification="informational",
        )
        self.store.inbox("two")

        unread = self.store.inbox("two", unread_only=True, mark_delivered=False)
        self.assertEqual([message["id"] for message in unread], [first["id"], second["id"]])
        acknowledged = self.store.acknowledge_all_unread("two")

        self.assertEqual(acknowledged["message_ids"], [first["id"], second["id"]])
        self.assertEqual(self.store.inbox("two", unread_only=True), [])
        self.assertEqual(
            len(self.store.inbox("two", include_delivered=True, mark_delivered=False)),
            2,
        )

    def test_atomic_whole_declaration_handoff_to_validation_lease(self) -> None:
        self.register("sender")
        self.register("recipient", "claude")
        self.store.begin_work(
            session_id="sender",
            bead_id="work-a",
            scopes=["src/**", "tests/test_app.py"],
        )

        handoff = self.store.handoff_work(
            sender_session_id="sender",
            recipient_session_id="recipient",
            target_bead_id="work-b",
            scopes=["tests/test_app.py", "src/**"],
            patch_label="adapter-v2",
            validation_boundary="focused tests passed at revision 7",
            validation_responsibility="recipient runs full suite",
            mode="validation",
            handoff_id="handoff-a",
        )

        self.assertEqual(handoff["source_bead_id"], "work-a")
        self.assertEqual(handoff["target_bead_id"], "work-b")
        self.assertEqual(handoff["mode"], "validation")
        self.assertEqual(handoff["notification"]["classification"], "action_required")
        self.assertFalse(handoff["notification"]["reply_required"])
        self.assertIsNone(self.store.get_session("sender")["bead_id"])
        recipient = self.store.get_session("recipient")
        self.assertEqual(recipient["bead_id"], "work-b")
        self.assertEqual(recipient["lease_mode"], "validation")
        self.assertEqual(recipient["activity"], "validating")
        self.assertEqual(len(self.store.inbox("recipient", mark_delivered=False)), 1)
        with self.assertRaises(ConflictError):
            self.store.begin_work(
                session_id="sender", bead_id="work-c", scopes=["src/api.py"]
            )

    def test_atomic_scope_only_handoff_preserves_ownership_and_validation_lease(self) -> None:
        self.register("sender")
        self.register("recipient", "claude")
        self.store.begin_work(session_id="sender", scopes=["src/**", "tests/**"])
        arguments = dict(
            sender_session_id="sender", recipient_session_id="recipient",
            patch_label="feature", validation_boundary="unit tests passed",
            validation_responsibility="run full suite", mode="validation",
        )
        with self.assertRaisesRegex(CoordinationError, "Partial scope"):
            self.store.handoff_work(**arguments, scopes=["src/**"])
        with self.assertRaisesRegex(CoordinationError, "Target Bead ID must not be empty"):
            self.store.handoff_work(**arguments, target_bead_id=" ")
        with patch.object(self.store, "_insert_message", side_effect=CoordinationError("failed notification")):
            with self.assertRaisesRegex(CoordinationError, "failed notification"):
                self.store.handoff_work(**arguments)
        self.assertEqual(self.store.get_session("sender")["write_scope"], ["src/**", "tests/**"])
        self.assertEqual(self.store.get_session("recipient")["write_scope"], [])

        handoff = self.store.handoff_work(**arguments)

        self.assertIsNone(handoff["source_bead_id"])
        self.assertIsNone(handoff["target_bead_id"])
        self.assertEqual(self.store.get_session("sender")["write_scope"], [])
        recipient = self.store.get_session("recipient")
        self.assertEqual(recipient["write_scope"], ["src/**", "tests/**"])
        self.assertEqual(recipient["lease_mode"], "validation")
        self.assertEqual(recipient["activity"], "validating")
        inbox = self.store.inbox("recipient")
        self.assertEqual(len(inbox), 1)
        self.assertIn("from sender to recipient", inbox[0]["body"])
        self.assertNotIn("None", inbox[0]["body"])
        with self.assertRaises(ConflictError):
            self.store.begin_work(session_id="sender", scopes=["src/app.py"])

    def test_handoff_rejects_partial_scope_and_non_idle_recipient_without_mutation(self) -> None:
        self.register("sender")
        self.register("recipient", "claude")
        self.store.begin_work(
            session_id="sender", bead_id="work-a", scopes=["src/**", "tests/**"]
        )

        with self.assertRaisesRegex(CoordinationError, "Partial scope"):
            self.store.handoff_work(
                sender_session_id="sender",
                recipient_session_id="recipient",
                scopes=["src/**"],
                patch_label="partial",
                validation_boundary="none",
                validation_responsibility="recipient",
                mode="write",
            )
        self.assertEqual(self.store.get_session("sender")["bead_id"], "work-a")
        self.assertIsNone(self.store.get_session("recipient")["bead_id"])

        self.store.touch("recipient", "discussing", turn_active=True)
        with self.assertRaisesRegex(CoordinationError, "must be idle"):
            self.store.handoff_work(
                sender_session_id="sender",
                recipient_session_id="recipient",
                patch_label="busy",
                validation_boundary="none",
                validation_responsibility="recipient",
                mode="write",
            )
        self.assertEqual(self.store.get_session("sender")["bead_id"], "work-a")

    def test_handoff_rolls_back_every_effect_on_notification_failure(self) -> None:
        self.register("sender")
        self.register("recipient", "claude")
        self.store.begin_work(
            session_id="sender", bead_id="work-a", scopes=["src/**"]
        )

        with (
            patch.object(
                self.store,
                "_insert_message",
                side_effect=CoordinationError("simulated notification failure"),
            ),
            self.assertRaisesRegex(CoordinationError, "simulated notification"),
        ):
            self.store.handoff_work(
                sender_session_id="sender",
                recipient_session_id="recipient",
                patch_label="rollback",
                validation_boundary="focused tests",
                validation_responsibility="recipient",
                mode="write",
                handoff_id="handoff-fails",
            )

        self.assertEqual(self.store.get_session("sender")["bead_id"], "work-a")
        self.assertIsNone(self.store.get_session("recipient")["bead_id"])
        with self.assertRaises(CoordinationError):
            self.store.get_handoff("handoff-fails")

    def test_bead_address_rejects_zero_or_multiple_live_owners(self) -> None:
        self.register("sender")
        with self.assertRaises(CoordinationError):
            self.store.send_message(
                sender_session_id="sender",
                recipient_bead_id="missing",
                body="hello",
            )

        self.register("one")
        self.register("two")
        with (
            self.store._connection() as connection
        ):  # Deliberately model corrupt ownership.
            connection.execute(
                "UPDATE sessions SET bead_id = 'duplicate', activity = 'waiting' "
                "WHERE session_id IN ('one', 'two')"
            )
        with self.assertRaises(AmbiguousTargetError):
            self.store.send_message(
                sender_session_id="sender",
                recipient_bead_id="duplicate",
                body="hello",
            )

    def test_inbox_wait_returns_immediately_for_unread_message(self) -> None:
        self.register("one")
        self.register("two", "claude")
        self.store.send_message(
            sender_session_id="one", recipient_session_id="two", body="hi"
        )

        sleep_calls: list[float] = []
        messages = self.store.inbox_wait(
            "two", timeout_seconds=5, sleep=sleep_calls.append
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["body"], "hi")
        self.assertEqual(sleep_calls, [])

    def test_inbox_wait_returns_message_delivered_while_waiting(self) -> None:
        self.register("one")
        self.register("two", "claude")
        state = {"sent": False}

        def fake_sleep(duration: float) -> None:
            self.clock.now += duration
            if not state["sent"]:
                state["sent"] = True
                self.store.send_message(
                    sender_session_id="one", recipient_session_id="two", body="hi"
                )

        before = self.store.get_session("two")["last_seen_at"]
        messages = self.store.inbox_wait(
            "two", timeout_seconds=5, poll_interval_seconds=1, sleep=fake_sleep
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["body"], "hi")
        after = self.store.get_session("two")["last_seen_at"]
        self.assertGreater(after, before)

    def test_inbox_wait_returns_new_message_delivered_by_hook(self) -> None:
        self.register("one")
        self.register("two", "claude")
        delivered_by_hook: list[dict[str, object]] = []

        def fake_sleep(duration: float) -> None:
            self.clock.now += duration
            if delivered_by_hook:
                return
            sent = self.store.send_message(
                sender_session_id="one", recipient_session_id="two", body="done"
            )
            delivered_by_hook.extend(self.store.inbox("two"))
            self.assertEqual(delivered_by_hook[0]["id"], sent["id"])

        messages = self.store.inbox_wait(
            "two", timeout_seconds=2, poll_interval_seconds=1, sleep=fake_sleep
        )

        self.assertEqual([message["body"] for message in messages], ["done"])
        self.assertEqual(messages[0]["id"], delivered_by_hook[0]["id"])

    def test_inbox_wait_does_not_replay_old_delivered_message(self) -> None:
        self.register("one")
        self.register("two", "claude")
        self.store.send_message(
            sender_session_id="one", recipient_session_id="two", body="old"
        )
        self.assertEqual([message["body"] for message in self.store.inbox("two")], ["old"])
        state = {"sent": False}

        def fake_sleep(duration: float) -> None:
            self.clock.now += duration
            if state["sent"]:
                return
            state["sent"] = True
            self.store.send_message(
                sender_session_id="one", recipient_session_id="two", body="new"
            )

        messages = self.store.inbox_wait(
            "two", timeout_seconds=2, poll_interval_seconds=1, sleep=fake_sleep
        )

        self.assertEqual([message["body"] for message in messages], ["new"])

    def test_inbox_wait_times_out_with_distinct_error(self) -> None:
        self.register("two", "claude")
        sleeps: list[float] = []

        def fake_sleep(duration: float) -> None:
            sleeps.append(duration)
            self.clock.now += duration

        with self.assertRaises(InboxTimeoutError) as caught:
            self.store.inbox_wait(
                "two", timeout_seconds=2, poll_interval_seconds=1, sleep=fake_sleep
            )

        self.assertEqual(caught.exception.session_id, "two")
        self.assertEqual(caught.exception.timeout_seconds, 2)
        self.assertEqual(sleeps, [1, 1])

    def test_inbox_wait_rejects_non_positive_timeout(self) -> None:
        self.register("two")
        with self.assertRaises(CoordinationError):
            self.store.inbox_wait("two", timeout_seconds=0, sleep=lambda _: None)

    def test_inbox_wait_without_timeout_polls_indefinitely(self) -> None:
        self.register("one")
        self.register("two", "claude")
        state = {"polls": 0}

        def fake_sleep(duration: float) -> None:
            self.clock.now += duration
            state["polls"] += 1
            if state["polls"] == 3:
                self.store.send_message(
                    sender_session_id="one", recipient_session_id="two", body="hi"
                )

        messages = self.store.inbox_wait(
            "two", poll_interval_seconds=1, sleep=fake_sleep
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(state["polls"], 3)

    def test_inbox_wait_rejects_include_delivered(self) -> None:
        self.register("two")
        with self.assertRaises(CoordinationError):
            self.store.inbox_wait("two", include_delivered=True, sleep=lambda _: None)

    def test_zellij_wake_claim_is_atomic_and_requires_an_idle_turn(self) -> None:
        self.register("one")
        self.register("two", "claude")
        self.store.register_zellij_wake(
            session_id="two",
            zellij_session="test-session",
            pane_id="terminal_7",
        )
        message = self.store.send_message(
            sender_session_id="one", recipient_session_id="two", body="wake up"
        )

        self.store.touch("two", "waiting", turn_active=True)
        self.assertEqual(self.store.claim_wake_messages("two", [message["id"]]), [])

        self.store.touch("two", "waiting", turn_active=False)
        self.assertEqual(self.store.pending_wake_message_ids("two"), [message["id"]])
        self.assertEqual(
            self.store.claim_wake_messages("two", [message["id"]]), [message["id"]]
        )
        self.assertEqual(self.store.claim_wake_messages("two", [message["id"]]), [])

        self.store.complete_wake_attempts(
            "two", [message["id"]], outcome="sent", detail="test prompt"
        )
        self.assertEqual(self.store.pending_wake_message_ids("two"), [])
        status = self.store.get_zellij_wake("two")
        self.assertEqual(status["recent_attempts"][0]["outcome"], "sent")

    def test_failed_zellij_wake_attempt_can_be_reclaimed_until_delivery(self) -> None:
        self.register("one")
        self.register("two", "claude")
        self.store.register_zellij_wake(
            session_id="two",
            zellij_session="test-session",
            pane_id="terminal_7",
        )
        message = self.store.send_message(
            sender_session_id="one", recipient_session_id="two", body="wake up"
        )
        self.store.touch("two", "waiting", turn_active=False)
        self.assertEqual(
            self.store.claim_wake_messages("two", [message["id"]]), [message["id"]]
        )
        self.store.complete_wake_attempts(
            "two", [message["id"]], outcome="failed", detail="not submitted"
        )

        self.assertEqual(self.store.pending_wake_message_ids("two"), [message["id"]])
        self.assertEqual(
            self.store.claim_wake_messages("two", [message["id"]]), [message["id"]]
        )
        self.assertEqual(self.store.claim_wake_messages("two", [message["id"]]), [])

    def test_delivered_messages_are_not_wake_candidates(self) -> None:
        self.register("one")
        self.register("two", "claude")
        self.store.register_zellij_wake(
            session_id="two",
            zellij_session="test-session",
            pane_id="terminal_7",
        )
        self.store.send_message(
            sender_session_id="one", recipient_session_id="two", body="already read"
        )

        self.store.inbox("two")

        self.assertEqual(self.store.pending_wake_message_ids("two"), [])

    def test_bead_addressed_message_becomes_a_wake_candidate(self) -> None:
        self.register("one")
        self.register("two", "claude")
        self.store.begin_work(session_id="two", bead_id="work-b", scopes=["tests/**"])
        self.store.touch("two", "waiting", turn_active=False)
        self.store.register_zellij_wake(
            session_id="two",
            zellij_session="test-session",
            pane_id="terminal_7",
        )

        message = self.store.send_message(
            sender_session_id="one",
            recipient_bead_id="work-b",
            body="bead handoff",
        )

        self.assertEqual(self.store.pending_wake_message_ids("two"), [message["id"]])

    def test_delegation_lifecycle_notifies_parent(self) -> None:
        self.register("parent")
        self.register("child")
        delegation = self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-a",
            scopes=["src/**"],
            instructions="Implement work-a.",
            mode="reviewed",
            lease_mode="validation",
            model="gpt-5.6-terra",
            reasoning_effort="high",
            delegation_id="delegation-a",
        )

        self.assertEqual(delegation["status"], "launching")
        self.assertEqual(delegation["lease_mode"], "validation")
        self.assertEqual(delegation["model"], "gpt-5.6-terra")
        self.assertEqual(delegation["reasoning_effort"], "high")
        attached = self.store.attach_delegation("delegation-a", "child")
        self.assertEqual(attached["status"], "attached")
        completed = self.store.finish_delegation(
            "delegation-a",
            child_session_id="child",
            outcome="completed",
            message="Implemented and validated.",
        )

        self.assertEqual(completed["status"], "completed")
        inbox = self.store.inbox("parent")
        self.assertIn("work-a completed", inbox[0]["body"])
        self.assertIn("Implemented and validated", inbox[0]["body"])

    def test_terminal_delegation_records_token_usage_once(self) -> None:
        self.register("parent")
        self.register("child")
        self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-a",
            scopes=["src/**"],
            instructions="Implement work-a.",
            mode="reviewed",
            delegation_id="delegation-a",
        )
        self.store.attach_delegation("delegation-a", "child")
        with self.assertRaisesRegex(CoordinationError, "is not terminal"):
            self.store.record_delegation_token_usage(
                "delegation-a",
                child_session_id="child",
                usage={"normalized": {"total_tokens": 1}},
                capture_event="Stop",
            )
        self.store.finish_delegation(
            "delegation-a",
            child_session_id="child",
            outcome="completed",
            message="Done.",
        )

        captured = self.store.record_delegation_token_usage(
            "delegation-a",
            child_session_id="child",
            usage={"normalized": {"total_tokens": 10}},
            capture_event="Stop",
            artifact_path="/repo/.agent-coord/delegation-a.usage.json",
        )
        repeated = self.store.record_delegation_token_usage(
            "delegation-a",
            child_session_id="child",
            usage={"normalized": {"total_tokens": 20}},
            capture_event="SessionEnd",
        )

        self.assertEqual(captured["token_usage"]["normalized"]["total_tokens"], 10)
        self.assertEqual(repeated["token_usage"]["normalized"]["total_tokens"], 10)
        self.assertEqual(captured["token_usage_capture_event"], "Stop")
        self.assertIsNotNone(captured["token_usage_captured_at"])

    def test_attached_validation_delegation_enforces_bead_scope_and_lease(self) -> None:
        self.register("parent")
        self.register("child")
        with self.assertRaisesRegex(
            CoordinationError, "Validation-only delegation cannot use yolo mode"
        ):
            self.store.create_delegation(
                parent_session_id="parent",
                cwd=str(self.root),
                bead_id="unsafe-validation",
                scopes=["src/**"],
                instructions="Validate unsafely.",
                mode="yolo",
                lease_mode="validation",
            )
        self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="validation-a",
            scopes=["src/**", "tests/**"],
            instructions="Validate the wave.",
            mode="reviewed",
            lease_mode="validation",
            delegation_id="delegation-a",
        )
        self.store.attach_delegation("delegation-a", "child")

        with self.assertRaisesRegex(CoordinationError, "requires Bead validation-a"):
            self.store.begin_work(
                session_id="child",
                bead_id="other",
                scopes=["src/**", "tests/**"],
                lease_mode="validation",
            )
        with self.assertRaisesRegex(CoordinationError, "requires its exact scopes"):
            self.store.begin_work(
                session_id="child",
                bead_id="validation-a",
                scopes=["src/**"],
                lease_mode="validation",
            )
        with self.assertRaisesRegex(CoordinationError, "requires lease mode validation"):
            self.store.begin_work(
                session_id="child",
                bead_id="validation-a",
                scopes=["src/**", "tests/**"],
                lease_mode="write",
            )

        session = self.store.begin_work(
            session_id="child",
            bead_id="validation-a",
            scopes=["src/**", "tests/**"],
            lease_mode="validation",
        )
        self.assertEqual(session["lease_mode"], "validation")
        self.assertEqual(session["activity"], "validating")

    def test_delegation_without_beads_enforces_scope_and_reports_by_delegation_id(self) -> None:
        self.register("parent")
        self.register("child")
        self.store.create_delegation(
            parent_session_id="parent", cwd=str(self.root), scopes=["src/**"],
            instructions="Validate the change.", mode="reviewed", lease_mode="validation",
            delegation_id="untracked",
        )
        self.store.attach_delegation("untracked", "child")
        with self.assertRaisesRegex(CoordinationError, "does not use a Bead"):
            self.store.begin_work(session_id="child", bead_id="invented", scopes=["src/**"], lease_mode="validation")
        with self.assertRaisesRegex(CoordinationError, "requires its exact scopes"):
            self.store.begin_work(session_id="child", scopes=["tests/**"], lease_mode="validation")
        with self.assertRaisesRegex(CoordinationError, "requires lease mode validation"):
            self.store.begin_work(session_id="child", scopes=["src/**"])
        self.store.begin_work(session_id="child", scopes=["src/**"], lease_mode="validation")
        self.store.end_work("child")

        result = self.store.finish_delegation(
            "untracked", child_session_id="child", outcome="completed", message="All checks passed.",
        )

        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["bead_id"])
        self.assertEqual(self.store.inbox("parent")[0]["body"], "Delegation untracked completed: All checks passed.")

    def test_claude_delegation_records_and_matches_child_client(self) -> None:
        self.register("parent")
        self.register("claude-child", client="claude")
        delegation = self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-a",
            scopes=["src/**"],
            instructions="Implement work-a.",
            mode="reviewed",
            client="claude",
            model="opus",
            reasoning_effort="high",
            delegation_id="delegation-claude",
        )

        attached = self.store.attach_delegation(
            delegation["delegation_id"], "claude-child"
        )

        self.assertEqual(attached["client"], "claude")
        self.assertEqual(attached["model"], "opus")
        self.assertEqual(attached["reasoning_effort"], "high")

    def test_delegation_rejects_a_child_from_the_wrong_client(self) -> None:
        self.register("parent")
        self.register("codex-child")
        self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-a",
            scopes=["src/**"],
            instructions="Implement work-a.",
            mode="reviewed",
            client="claude",
            delegation_id="delegation-claude",
        )

        with self.assertRaisesRegex(CoordinationError, "targets claude"):
            self.store.attach_delegation("delegation-claude", "codex-child")

    def test_duplicate_active_delegation_is_rejected(self) -> None:
        self.register("parent")
        self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-a",
            scopes=["src/**"],
            instructions="First worker.",
            mode="reviewed",
            delegation_id="delegation-a",
        )

        with self.assertRaisesRegex(CoordinationError, "active delegation"):
            self.store.create_delegation(
                parent_session_id="parent",
                cwd=str(self.root),
                bead_id="work-a",
                scopes=["tests/**"],
                instructions="Second worker.",
                mode="reviewed",
                delegation_id="delegation-b",
            )

    def test_delegation_rejects_a_second_child(self) -> None:
        self.register("parent")
        self.register("child-one")
        self.register("child-two")
        self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-a",
            scopes=["src/**"],
            instructions="Implement work-a.",
            mode="reviewed",
            delegation_id="delegation-a",
        )
        self.store.attach_delegation("delegation-a", "child-one")

        with self.assertRaisesRegex(CoordinationError, "already attached"):
            self.store.attach_delegation("delegation-a", "child-two")

    def test_unfinished_child_exit_marks_delegation_failed(self) -> None:
        self.register("parent")
        self.register("child")
        self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-a",
            scopes=["src/**"],
            instructions="Implement work-a.",
            mode="reviewed",
            delegation_id="delegation-a",
        )
        self.store.attach_delegation("delegation-a", "child")

        failed = self.store.fail_active_delegations_for_child(
            "child", "Child process exited."
        )

        self.assertEqual(failed[0]["status"], "failed")
        self.assertIn("Child process exited", self.store.inbox("parent")[0]["body"])

    def test_parent_can_cancel_an_unattached_delegation(self) -> None:
        self.register("parent")
        self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-a",
            scopes=["src/**"],
            instructions="Implement work-a.",
            mode="reviewed",
            delegation_id="delegation-a",
        )

        cancelled = self.store.cancel_delegation(
            "delegation-a",
            parent_session_id="parent",
            reason="The child hook did not attach.",
        )

        self.assertEqual(cancelled["status"], "failed")
        self.assertIn("did not attach", cancelled["error"])

    def test_non_parent_cannot_cancel_a_delegation(self) -> None:
        self.register("parent")
        self.register("other")
        self.store.create_delegation(
            parent_session_id="parent",
            cwd=str(self.root),
            bead_id="work-a",
            scopes=["src/**"],
            instructions="Implement work-a.",
            mode="reviewed",
            delegation_id="delegation-a",
        )

        with self.assertRaisesRegex(CoordinationError, "is not the parent"):
            self.store.cancel_delegation(
                "delegation-a",
                parent_session_id="other",
                reason="Cancel it.",
            )

    def test_parallel_script_scopes_can_be_declared_and_checked(self):
        self.register("one")
        self.register("two", "claude")
        self.store.begin_work(session_id="one", scopes=["scripts/evolution_*.py"])
        self.store.begin_work(session_id="two", scopes=[
            "scripts/campaign_health_check.py", "scripts/wake_duty_officer.py"])
        self.assertFalse(self.store.check_conflicts("one"))
        self.assertFalse(self.store.check_conflicts("two"))
        from agent_coord.hook import handle
        self.assertEqual(handle({
            "hook_event_name": "PreToolUse", "session_id": "one", "cwd": str(self.root),
            "tool_name": "apply_patch", "tool_input": {"command":
                "*** Begin Patch\n*** Update File: scripts/evolution_search.py\n*** End Patch"},
        }, self.store), {})

    def test_scope_glob_does_not_conflict_with_unrelated_literal_files(self):
        for literal in ("scripts/campaign_health_check.py", "scripts/wake_duty_officer.py"):
            with self.subTest(literal=literal):
                self.assertFalse(scopes_overlap("scripts/evolution_*.py", literal))
                self.assertFalse(scopes_overlap(literal, "scripts/evolution_*.py"))
        self.assertFalse(scopes_overlap("private/evolution/population.*", "private/evolution/generator.py"))
        self.assertTrue(scopes_overlap("private/evolution/population.*", "private/evolution/population.json"))

    def test_scope_intersections_preserve_directories_and_recursive_globs(self):
        cases = [
            ("scripts/evolution_*.py", "scripts/evolution_search.py", True),
            ("scripts/evolution_*.py", "scripts", True),
            ("scripts/evolution_*.py", "scripts/other", False),
            ("scripts/evolution_*.py", "scripts/evolution_*.json", False),
            ("scripts/evolution_*.py", "scripts/campaign_*.py", False),
            ("scripts/evolution_*.py", "scripts/*_search.py", True),
            ("scripts/evolution_*.py", "scripts/evolution_?earch.py", True),
            ("scripts/a*.py", "scripts/*a/b*.py", True),
            ("scripts/*/b.py", "scripts/a*/c/*.py", True),
            ("src/**/app.py", "src/app.py", True),
            ("src/**/app.py", "src/deep/nested/app.py", True),
            ("src/**/app.py", "tests/app.py", False),
            ("src/**/app.py", "src/pkg", True),
            ("src/*/app.py", "src/pkg", True),
            ("src/*/app.py", "src/pkg/other.py", False),
            ("src/*", "src/pkg/file.py", True),
            ("src/**", "src", True),
            ("src/**", "src-other/file.py", False),
            ("src/**/a*.py", "src/**/ab*.py", True),
            ("src/[ab]*/file.py", "src/alpha/file.py", True),
            ("src/[ab]*/file.py", "src/charlie/file.py", False),
            (".", "src/file.py", True),
            ("/", "/src/file.py", True),
            ("//server/share/src/**", r"\\server\share\src\app.py", True),
            ("**", "src/file.py", True),
            ("**/*", "file.py", True),
            ("src/", "src/file.py", True),
            ("src/./pkg/../*.py", "src/app.py", True),
            (r"scripts\evolution_*.py", "scripts/campaign_health_check.py", False),
            (r"scripts\evolution_*.py", r"scripts\evolution_search.py", True),
            ("C:/repo/src/**", "C:/repo/src/app.py", True),
            ("C:/repo/src/**", "D:/repo/src/app.py", False),
            ("Src/*.py", "src/app.py", False),
        ]
        for left, right, expected in cases:
            with self.subTest(left=left, right=right):
                self.assertEqual(scopes_overlap(left, right), expected)
                self.assertEqual(scopes_overlap(right, left), expected)

    def test_scope_globs_preserve_concrete_fnmatch_intersections(self):
        import fnmatch
        from itertools import combinations, product
        patterns = ["scripts/a*.py", "scripts/b*.py", "scripts/*/b.py",
                    "scripts/a*/c/*.py", "scripts/*a/b*.py", "scripts/a*b/c*.py",
                    "scripts/**/a.py", "scripts/*/*.json", "scripts/**", "scripts/*.py"]
        parts = ["a", "b", "ab", "c", "a.py", "b.py", "a.json"]
        paths = ["scripts/" + "/".join(p) for n in range(1, 4) for p in product(parts, repeat=n)]
        for left, right in combinations(patterns, 2):
            witness = next((p for p in paths if fnmatch.fnmatchcase(p, left)
                            and fnmatch.fnmatchcase(p, right)), None)
            if witness:
                with self.subTest(left=left, right=right, witness=witness):
                    self.assertTrue(scopes_overlap(left, right))

    def test_scope_helpers_are_conservative(self) -> None:
        self.assertTrue(scopes_overlap("src/**", "src/api/**"))
        self.assertFalse(scopes_overlap("src/**", "tests/**"))
        self.assertTrue(path_is_in_scope("src/api/app.py", "src/**"))
        self.assertFalse(path_is_in_scope("tests/test_app.py", "src/**"))


if __name__ == "__main__":
    unittest.main()


class ToolActivityStoreTests(unittest.TestCase):
    """The latest tool call is a small, best-effort record on the session."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = CoordinationStore(self.root / "state.sqlite3")

    def test_record_tool_is_serialized_with_the_session(self) -> None:
        self.store.register(session_id="one", client="claude", cwd=str(self.root), name="One")
        self.assertIsNone(self.store.get_session("one")["last_tool"])
        self.assertTrue(self.store.record_tool("one", "Bash", "  python3   -m unittest\n-q "))
        tool = self.store.get_session("one")["last_tool"]
        self.assertEqual((tool["name"], tool["kind"], tool["label"]), ("Bash", "run", "python3 -m unittest -q"))
        self.assertIsInstance(tool["at"], float)
        self.assertTrue(self.store.record_tool("one", "fileChange", "x" * 500, kind="edit"))
        self.assertEqual(len(self.store.get_session("one")["last_tool"]["label"]), 200)
        self.assertEqual(self.store.get_session("one")["last_tool"]["kind"], "edit")

    def test_unknown_sessions_and_empty_names_are_ignored(self) -> None:
        self.assertFalse(self.store.record_tool("missing", "Bash", "ls"))
        self.store.register(session_id="one", client="codex", cwd=str(self.root))
        self.assertFalse(self.store.record_tool("one", "   ", "ls"))
        self.assertIsNone(self.store.get_session("one")["last_tool"])

    def test_tool_kinds_cover_both_providers(self) -> None:
        from agent_coord.store import tool_kind
        self.assertEqual([tool_kind(n) for n in ("Bash", "commandExecution", "Edit", "fileChange", "Read", "webSearch", "mcpToolCall")],
                         ["run", "run", "edit", "edit", "read", "read", "tool"])
