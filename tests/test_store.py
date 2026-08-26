from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
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
        legacy_message = migrated.inbox(
            "legacy-recipient", include_delivered=True, mark_delivered=False
        )[0]
        self.assertEqual(legacy_message["classification"], "action_required")
        self.assertEqual(legacy_message["thread_id"], f"legacy:{legacy_message['id']}")
        self.assertTrue(legacy_message["reply_required"])

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
