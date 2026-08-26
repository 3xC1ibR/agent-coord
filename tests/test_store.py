from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

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
                """
            )

        migrated = CoordinationStore(database, clock=self.clock)
        migrated.register(session_id="legacy", client="codex", cwd=str(self.root))

        self.assertFalse(migrated.get_session("legacy")["turn_active"])

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
        status = self.store.get_zellij_wake("two")
        self.assertEqual(status["recent_attempts"][0]["outcome"], "sent")

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
            model="gpt-5.6-terra",
            reasoning_effort="high",
            delegation_id="delegation-a",
        )

        self.assertEqual(delegation["status"], "launching")
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

    def test_scope_helpers_are_conservative(self) -> None:
        self.assertTrue(scopes_overlap("src/**", "src/api/**"))
        self.assertFalse(scopes_overlap("src/**", "tests/**"))
        self.assertTrue(path_is_in_scope("src/api/app.py", "src/**"))
        self.assertFalse(path_is_in_scope("tests/test_app.py", "src/**"))


if __name__ == "__main__":
    unittest.main()
