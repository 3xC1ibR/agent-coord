from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.runtime_message_intake import ComposerNotReserved, IntakeFull, OrdinaryComposerReceiver, RuntimeMessageIntake, canonical_receipt
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.submission_receipts import SubmissionReceipts


class ProcessExited(BaseException):
    pass


class RuntimeMessageIntakeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / "private"
        self.intake = RuntimeMessageIntake(self.directory)
        self.store = CoordinationStore(Path(temp.name) / "backend.sqlite3")
        # The composer receipt tests use this same minimal app registration.
        with self.store._connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS browser_sessions (thread_id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO browser_sessions(thread_id) VALUES ('one'),('two')")
        self.receipts = SubmissionReceipts(self.store)
        self.body = {"message": "one private message", "requestId": "r1", "expectedTurnId": "turn-1",
                     "activity": {"source": "test"}, "windowId": "first-window"}
        self.effects = []

    def lookup(self, thread, body):
        return canonical_receipt(self.store.database_path, thread, body)

    def forward(self, thread, body):
        def deliver():
            self.effects.append((thread, dict(body)))
            return {"turnId": body["expectedTurnId"]}
        return self.receipts.run(thread, body, deliver)

    def test_durable_accept_then_restart_drains_original_request_exactly_once(self):
        first = self.intake.accept("one", self.body)
        self.assertEqual(first["state"], "queued")
        self.assertEqual(os.stat(self.intake.database).st_mode & 0o777, 0o600)
        self.assertEqual(self.intake.get("two", "r1"), None)
        self.assertEqual(self.intake.outstanding("two"), [])
        self.intake = RuntimeMessageIntake(self.directory)
        self.assertEqual(self.intake.accept("one", self.body), first)
        self.assertEqual(self.intake.outstanding("one"), [first])
        self.assertEqual(self.intake.drain_one("one", self.lookup, self.forward)["state"], "committed")
        self.assertEqual(self.intake.accept("one", self.body)["state"], "committed")
        self.assertIsNone(self.intake.drain_one("one", self.lookup, self.forward))
        self.assertEqual(self.effects, [("one", self.body)])

    def test_process_exit_after_backend_commit_recovers_without_another_provider_call(self):
        self.intake.accept("one", self.body)
        code = """import os, sys
from agent_coord.runtime_message_intake import RuntimeMessageIntake, canonical_receipt
from agent_coord.store import CoordinationStore
from agent_coord.submission_receipts import SubmissionReceipts
intake = RuntimeMessageIntake(sys.argv[1])
store = CoordinationStore(sys.argv[2])
receipts = SubmissionReceipts(store)
def forward(thread, body):
    receipts.run(thread, body, lambda: {"turnId": body["expectedTurnId"]})
    os._exit(42)
intake.drain_one("one", lambda t, b: canonical_receipt(store.database_path, t, b), forward)
"""
        backend = str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts")
        child = subprocess.run([sys.executable, "-B", "-c", code, str(self.directory),
                                str(self.store.database_path)], env=dict(os.environ, PYTHONPATH=backend),
                               capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 42, child.stderr)
        self.intake = RuntimeMessageIntake(self.directory)
        self.assertEqual(self.intake.get("one", "r1")["state"], "uncertain")
        self.assertEqual(self.intake.drain_one("one", self.lookup, self.forward)["state"], "committed")
        self.assertEqual(self.effects, [])

    def test_duplicate_conflicts_include_the_expected_turn_and_new_work_fails_closed_at_capacity(self):
        intake = RuntimeMessageIntake(self.directory, max_records=1)
        accepted = intake.accept("one", self.body)
        for field, value in (("expectedTurnId", "next-turn"), ("message", "different")):
            with self.assertRaisesRegex(CoordinationError, "different input"):
                intake.accept("one", {**self.body, field: value})
        with self.assertRaises(IntakeFull):
            intake.accept("two", self.body)
        self.assertEqual(intake.accept("one", self.body), accepted)
        self.assertIsNone(intake.get("two", "r1"))
        byte_limited = RuntimeMessageIntake(self.directory.parent / "tiny", max_bytes=1)
        with self.assertRaises(IntakeFull):
            byte_limited.accept("one", self.body)
        self.assertEqual(byte_limited.pending_threads(), [])

    def test_crash_after_claim_before_provider_effect_remains_uncertain_and_preserves_thread_order(self):
        self.intake.accept("one", self.body)
        self.intake.accept("one", {**self.body, "requestId": "r2", "message": "next"})
        self.intake.accept("two", self.body)
        def crash(*args):
            raise ProcessExited
        with self.assertRaises(ProcessExited):
            self.intake.drain_one("one", self.lookup, crash)
        self.intake = RuntimeMessageIntake(self.directory)
        self.assertEqual(self.intake.drain_one("one", self.lookup, self.forward)["state"], "uncertain")
        self.assertEqual(self.intake.pending_threads(), ["one", "two"])
        self.assertEqual([row["state"] for row in self.intake.outstanding("one")], ["uncertain", "queued"])
        self.assertEqual(self.intake.drain_one("two", self.lookup, self.forward)["state"], "committed")
        self.assertEqual(self.effects, [("two", self.body)])

    def test_crash_after_provider_commit_is_reconciled_from_canonical_receipt(self):
        self.intake.accept("one", self.body)
        def after_delivery(thread, body):
            self.forward(thread, body)
            raise ProcessExited
        with self.assertRaises(ProcessExited):
            self.intake.drain_one("one", self.lookup, after_delivery)
        self.assertEqual(self.intake.get("one", "r1")["state"], "uncertain")
        intake = RuntimeMessageIntake(self.directory)
        self.assertEqual(intake.drain_one("one", self.lookup, self.forward)["state"], "committed")
        self.assertEqual(self.effects, [("one", self.body)])

    def test_failed_forward_response_requires_canonical_evidence(self):
        def lost_response(thread, body):
            self.forward(thread, body)
            raise OSError("connection lost")
        self.intake.accept("one", self.body)
        self.assertEqual(self.intake.drain_one("one", self.lookup, lost_response)["state"], "committed")
        self.assertEqual(self.effects, [("one", self.body)])
        def unavailable(*args):
            raise ConnectionError("backend unavailable")
        self.intake.accept("two", self.body)
        self.assertEqual(self.intake.drain_one("two", self.lookup, unavailable)["state"], "uncertain")
        self.assertEqual(self.intake.drain_one("two", self.lookup, self.forward)["state"], "uncertain")
        self.assertEqual(len(self.effects), 1)

    def receiver(self, forward=None, *, instance="coordinator-one", payload="signed-payload"):
        return OrdinaryComposerReceiver(self.store.database_path, instance, payload, forward or self.forward)

    def test_receipt_first_receiver_recovers_process_exit_before_forward_and_drains_in_order(self):
        self.intake.accept("one", self.body)
        second = {**self.body, "requestId": "second", "message": "after the crash"}
        self.intake.accept("one", second)
        code = """import os, sys
from agent_coord.runtime_message_intake import RuntimeMessageIntake, OrdinaryComposerReceiver
receiver = OrdinaryComposerReceiver(sys.argv[2], "coordinator-one", "signed-payload", lambda *_: os._exit(42))
RuntimeMessageIntake(sys.argv[1]).drain_to("one", receiver)
"""
        backend = str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts")
        child = subprocess.run([sys.executable, "-B", "-c", code, str(self.directory), str(self.store.database_path)],
                               env=dict(os.environ, PYTHONPATH=backend), capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 42, child.stderr)
        self.assertIsNone(self.lookup("one", self.body))
        intake = RuntimeMessageIntake(self.directory)
        for body in (self.body, second):
            receipt = intake.drain_to("one", self.receiver())
            self.assertEqual((receipt["state"], receipt["requestId"]), ("committed", body["requestId"]))
            self.assertEqual(self.lookup("one", body)["state"], "completed")
        self.assertEqual(self.effects, [("one", self.body), ("one", second)])

    def test_verified_receiver_dropped_ack_requires_one_provider_effect(self):
        self.intake.accept("one", self.body)
        def lost_ack(thread, body):
            self.forward(thread, body)
            raise ConnectionError
        result = self.intake.drain_to("one", self.receiver(lost_ack))
        self.assertEqual(result["state"], "committed")
        self.assertIsNone(self.intake.drain_to("one", self.receiver()))
        self.assertEqual(self.effects, [("one", self.body)])

    def test_receipt_observed_accepted_or_uncertain_prevents_replay_even_if_removed(self):
        def crash(*_args):
            raise ProcessExited
        for thread in ("one", "two"):
            with self.subTest(thread=thread):
                self.intake.accept(thread, self.body)
                with self.assertRaises(ProcessExited):
                    self.intake.drain_to(thread, self.receiver(crash))
                # Exercise SubmissionReceipts.run's actual reservation, committed
                # before entering the provider callback, and its post-error state.
                error = ProcessExited if thread == "one" else ConnectionError
                def interrupted():
                    raise error()
                with self.assertRaises(error):
                    self.receipts.run(thread, self.body, interrupted)
                self.assertEqual(self.lookup(thread, self.body)["state"], "accepted" if thread == "one" else "uncertain")
                self.assertEqual(self.intake.drain_to(thread, self.receiver())["state"], "uncertain")
                with self.store._connection() as db:
                    db.execute("DELETE FROM browser_submission_receipts WHERE thread_id=?", (thread,))
                self.assertEqual(self.intake.drain_to(thread, self.receiver())["state"], "uncertain")
        self.assertEqual(self.effects, [])

    def test_authority_change_or_downgrade_cannot_replay_unconfirmed_input(self):
        def crash(*_args):
            raise ProcessExited
        for thread in ("one", "two"):
            self.intake.accept(thread, self.body)
            with self.assertRaises(ProcessExited):
                self.intake.drain_to(thread, self.receiver(crash))
            result = self.intake.drain_one(thread, self.lookup, self.forward)
            self.assertEqual(result["state"], "uncertain")
            changed = self.receiver(instance="new-instance") if thread == "one" else self.receiver(payload="new-payload")
            self.assertEqual(self.intake.drain_to(thread, changed)["reason"], "receiver_changed")
            self.assertEqual(self.intake.drain_to(thread, self.receiver())["state"], "uncertain")
        self.assertEqual(self.effects, [])

    def test_replaced_database_inode_cannot_replay_unconfirmed_input(self):
        def crash(*_args):
            raise ProcessExited
        self.intake.accept("one", self.body)
        receiver = self.receiver(crash)
        with self.assertRaises(ProcessExited):
            self.intake.drain_to("one", receiver)
        path = self.store.database_path
        original = path.with_suffix(".original")
        path.rename(original)
        try:
            path.touch()
            self.assertEqual(self.intake.drain_to("one", receiver)["reason"], "receiver_changed")
            self.assertEqual(self.intake.drain_to("one", self.receiver())["reason"], "receiver_changed")
        finally:
            path.unlink()
            original.rename(path)
        self.assertEqual(self.intake.drain_to("one", self.receiver())["state"], "uncertain")
        self.assertEqual(self.effects, [])

    def test_only_definitive_first_attempt_refusal_finishes_without_a_receipt(self):
        def refused(*_args):
            raise ComposerNotReserved
        self.intake.accept("one", self.body)
        result = self.intake.drain_to("one", self.receiver(refused))
        self.assertEqual((result["state"], result["reason"]), ("failed", "backend_declined_before_reservation"))
        self.intake.accept("two", self.body)
        def crash(*_args):
            raise ProcessExited
        with self.assertRaises(ProcessExited):
            self.intake.drain_to("two", self.receiver(crash))
        result = self.intake.drain_to("two", self.receiver(refused))
        self.assertEqual((result["state"], result["reason"]), ("uncertain", "previous_attempt_unconfirmed"))
        self.assertEqual(self.intake.drain_to("two", self.receiver())["state"], "uncertain")
        self.assertEqual(self.effects, [])

    def test_blocking_verified_transport_excludes_second_process_or_instance_from_forward(self):
        entered, release = threading.Event(), threading.Event()
        other = RuntimeMessageIntake(self.directory)
        calls = []
        def blocked(thread, body):
            calls.append(body["requestId"])
            entered.set()
            if not release.wait(4):
                raise TimeoutError
            return self.forward(thread, body)
        self.intake.accept("one", self.body)
        receiver = self.receiver(blocked)
        worker = threading.Thread(target=other.drain_to, args=("one", receiver))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.intake.drain_to("one", receiver)["state"], "uncertain")
        finally:
            release.set()
            worker.join(5)
        self.assertEqual(calls, ["r1"])
        self.assertEqual(self.effects, [("one", self.body)])

    def test_unavailable_canonical_lookup_prevents_forwarding_and_preserves_queue(self):
        missing = self.directory.parent / "missing.sqlite3"
        self.intake.accept("one", self.body)
        def unavailable(thread, body):
            return canonical_receipt(missing, thread, body)
        with self.assertRaises(FileNotFoundError):
            self.intake.drain_one("one", unavailable, self.forward)
        self.assertEqual(self.intake.get("one", "r1")["state"], "queued")
        self.assertFalse(missing.exists())
        self.assertEqual(self.effects, [])

    def test_canonical_rejection_or_conflict_is_terminal_and_later_request_can_drain(self):
        self.intake.accept("one", self.body)
        self.intake.accept("one", {**self.body, "requestId": "r2"})
        self.forward("one", self.body)
        with self.store._connection() as db:
            db.execute("UPDATE browser_submission_receipts SET state='rejected'")
        row = self.intake.drain_one("one", self.lookup, self.forward)
        self.assertEqual((row["state"], row["reason"]), ("failed", "backend_rejected"))
        self.assertEqual(self.intake.accept("one", self.body)["state"], "failed")
        self.forward("one", {**self.body, "requestId": "r2", "message": "foreign input"})
        old_count = len(self.effects)
        row = self.intake.drain_one("one", self.lookup, self.forward)
        self.assertEqual((row["state"], row["reason"]), ("failed", "request_id_conflict"))
        self.assertEqual(len(self.effects), old_count)

    def test_simultaneous_accepts_and_drainers_never_duplicate_the_forward(self):
        other = RuntimeMessageIntake(self.directory)
        accepted = []
        barrier = threading.Barrier(2)
        def accept(intake):
            barrier.wait(timeout=3)
            accepted.append(intake.accept("one", self.body))
        threads = [threading.Thread(target=accept, args=(i,)) for i in (self.intake, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(4)
        self.assertEqual(len(accepted), 2)
        self.assertEqual(accepted[0], accepted[1])
        entered, release = threading.Event(), threading.Event()
        def blocked_forward(thread, body):
            entered.set()
            self.assertTrue(release.wait(4))
            return self.forward(thread, body)
        worker = threading.Thread(target=other.drain_one, args=("one", self.lookup, blocked_forward))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.intake.drain_one("one", self.lookup, self.forward)["state"], "uncertain")
        finally:
            release.set()
            worker.join(5)
        self.assertEqual(self.intake.get("one", "r1")["state"], "committed")
        self.assertEqual(self.effects, [("one", self.body)])

    def test_locked_intake_cannot_acknowledge_an_uncommitted_request(self):
        with closing(sqlite3.connect(self.intake.database)) as other:
            other.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.OperationalError):
                self.intake.accept("one", self.body)
            other.rollback()
        self.assertIsNone(self.intake.get("one", "r1"))


class OrderedTerminalDrainTests(unittest.TestCase):
    setUp = RuntimeMessageIntakeTests.setUp
    forward = RuntimeMessageIntakeTests.forward
    receiver = RuntimeMessageIntakeTests.receiver

    def batch(self, forward, completed=lambda: None, stopped=lambda: False, limit=16):
        return self.intake.drain_batch("one", self.receiver(forward), preflight=lambda *_: None,
                                       completed=completed, stopped=stopped, limit=limit)

    def enqueue(self, count):
        for i in range(count):
            self.intake.accept("one", {**self.body, "requestId": str(i)})

    def test_ordered_batch_has_individual_commits_and_observes_stop_between_messages(self):
        self.enqueue(8)
        observations = []
        def completed():
            key = str(len(observations))
            observations.append(RuntimeMessageIntake(self.directory).get("one", key)["state"])
        self.batch(self.forward, completed, stopped=lambda: len(observations) == 3)
        self.assertEqual(observations, ["committed"] * 3)
        self.assertEqual([entry["requestId"] for entry in self.intake.outstanding("one")], list("34567"))
        self.batch(self.forward, completed, limit=2)
        self.assertEqual([entry[1]["requestId"] for entry in self.effects], list("01234"))
        self.assertEqual(observations, ["committed"] * 5)

    def test_lost_ack_and_uncertain_head_stop_tail_and_reopen_without_replay(self):
        self.enqueue(4)
        def forward(thread, body):
            if body["requestId"] == "1":
                def unavailable():
                    self.effects.append((thread, body))
                    raise RuntimeError("The provider disconnected.")
                self.receipts.run(thread, body, unavailable)
            self.forward(thread, body)
            raise ConnectionError("The response was lost")
        self.batch(forward)
        self.assertEqual([self.intake.get("one", str(i))["state"] for i in range(4)],
                         ["committed", "uncertain", "queued", "queued"])
        self.intake = RuntimeMessageIntake(self.directory)
        self.batch(self.forward)
        self.assertEqual([body["requestId"] for _, body in self.effects], ["0", "1"])
        self.assertEqual(self.intake.get("one", "1")["state"], "uncertain")

    def test_other_writers_can_accept_while_receiver_runs_and_tail_includes_the_arrival(self):
        self.enqueue(1)
        started, release = threading.Event(), threading.Event()
        def forward(thread, body):
            if body["requestId"] == "0":
                started.set()
                self.assertTrue(release.wait(5))
            return self.forward(thread, body)
        worker = threading.Thread(target=self.batch, args=(forward,))
        worker.start()
        try:
            self.assertTrue(started.wait(5))
            other = RuntimeMessageIntake(self.directory)
            other.accept("one", {**self.body, "requestId": "later"})
            result = other.drain_to("one", self.receiver(self.forward))
            self.assertEqual(result["state"], "uncertain")
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual([body["requestId"] for _, body in self.effects], ["0", "later"])

    def test_one_terminal_scheduling_pass_dispatches_consecutive_receipts_in_order(self):
        from agent_coord.runtime_interaction_gateway import InteractionGateway
        self.enqueue(8)
        gateway = InteractionGateway(self.store.database_path, self.directory, self.directory)
        self.addCleanup(gateway.delivery_pool.shutdown, wait=True)
        gateway.fence = {"state": "draining"}
        gateway.backend = ("fixture", "token", "coordinator-one", "signed-payload")
        gateway.receiver = self.receiver(self.forward)
        gateway._verify_delivery = lambda *_: None
        gateway.resume()
        gateway.draining_threads["one"].result(timeout=10)
        self.assertEqual([body["requestId"] for _, body in self.effects], list("01234567"))
        self.assertEqual([self.intake.get("one", str(i))["state"] for i in range(8)], ["committed"] * 8)
        self.assertEqual(gateway.revision, 8)
