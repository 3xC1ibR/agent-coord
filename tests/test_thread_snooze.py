from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))

from agent_coord.codex_app_server import BrowserSessions
from agent_coord.store import CoordinationError, CoordinationStore
from test_codex_app_server import FakeCodex
import test_web_push as push_fixtures


class ThreadSnoozeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 1000.0
        self.store = CoordinationStore(self.root / "coord.sqlite3", clock=lambda: self.now)
        self.store.register(session_id="thread", client="codex", cwd=str(self.root))
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)

    def thread(self):
        return self.sessions.work_thread("thread")

    def update(self, **body):
        return self.sessions.update_work_thread("thread", body)

    def finish(self, *, actor="nobody", status="completed", turn="one"):
        self.store.threads.start_turn("thread", turn_id=turn)
        self.store.threads.checkpoint("thread", {"phase": "implementation", "summary": "Working."})
        self.store.threads.checkpoint("thread", {"phase": "finished", "summary": "Delivered.",
            "next_actor": actor, "next_action": "Approve the result." if actor == "user" else ""})
        self.store.threads.finish_turn("thread", status=status)
        self.store.touch("thread", turn_active=False)
        return self.thread()

    def test_read_result_wakes_after_restart_and_requires_explicit_resume(self):
        self.finish()
        before = self.update(seen=True, pinned=True)
        self.assertFalse(before["needs_attention"])
        parked = self.update(snoozed_until=1100)
        self.assertEqual(parked["attention"], "later")
        self.assertTrue(parked["snoozed"])
        self.assertFalse(parked["needs_attention"])
        for key in ("work_phase", "updated_at", "seen_completion_id", "handled_completion_id", "pinned", "checkpoint"):
            self.assertEqual(parked[key], before[key], key)
        self.now = 1100
        restarted = CoordinationStore(self.store.database_path, clock=lambda: self.now)
        self.assertEqual(restarted.threads.list()[0]["attention"], "now")
        due = self.thread()
        self.assertTrue(due["snooze_due"])
        self.assertTrue(due["needs_attention"])
        self.assertEqual(due["attention_reason"], "snooze")
        self.assertEqual(due["attention_since"], 1100)
        self.assertTrue(self.update(seen=True)["needs_attention"])
        resumed = self.update(resume_snooze=1100)
        self.assertFalse(resumed["needs_attention"])
        self.assertIsNone(resumed["snoozed_until"])
        self.assertEqual(resumed["work_phase"], before["work_phase"])

    def test_required_input_and_failure_survive_snooze_resume(self):
        for status, actor in (("completed", "user"), ("failed", "nobody")):
            with self.subTest(status=status):
                self.finish(actor=actor, status=status, turn=status)
                self.update(snoozed_until=1200)
                self.assertFalse(self.update(seen=True)["needs_attention"])
                self.now = 1200
                self.assertTrue(self.thread()["snooze_due"])
                resumed = self.update(resume_snooze=1200)
                self.assertTrue(resumed["needs_attention"])
                self.assertEqual(resumed["response_state"], "input" if actor == "user" else "failed")
                self.now = 1000

    def test_new_activity_does_not_wake_snooze_but_user_message_does(self):
        self.finish()
        self.update(snoozed_until=1200)
        self.finish(turn="background")
        self.assertTrue(self.thread()["snoozed"])
        self.assertFalse(self.thread()["needs_attention"])
        self.store.threads.user_message("thread")
        self.assertEqual(self.thread()["attention"], "now")
        self.assertIsNone(self.thread()["snoozed_until"])
        self.update(snoozed_until=1200)
        self.now = 1201
        self.assertTrue(self.thread()["snooze_due"])
        self.store.threads.user_message("thread")
        self.assertFalse(self.thread()["snooze_due"])

    def test_early_resume_reading_resnooze_and_stale_tab(self):
        self.finish()
        self.update(snoozed_until=1200)
        self.assertTrue(self.update(seen=True)["snoozed"])
        self.assertFalse(self.update(resume_snooze=1200)["snoozed"])
        self.update(snoozed_until=1200)
        self.now = 1200
        self.assertTrue(self.thread()["needs_attention"])
        replacement = self.update(snoozed_until=1400)
        with self.assertRaisesRegex(CoordinationError, "changed"):
            self.update(resume_snooze=1200, title="Stale edit")
        self.assertEqual(self.thread(), replacement)

    def test_invalid_times_and_conflicting_operations_are_atomic(self):
        self.finish()
        before = self.thread()
        bodies = [{"snoozed_until": value} for value in (None, True, "1200", [], {}, -1, 0, 1000,
                  float("nan"), float("inf"), 253402300800, 10 ** 1000)]
        bodies += [{"resume_snooze": value} for value in (None, True, "1200", float("nan"), 1200)]
        bodies += [{"snoozed_until": 1200, "attention": "later"}, {"snoozed_until": 1200, "resume_snooze": 1100}]
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(CoordinationError):
                self.update(title="Invalid edit", **body)
            self.assertEqual(self.thread(), before)

    def test_manual_placement_cancels_snooze_and_closed_threads_never_wake(self):
        self.finish()
        for placement in ("now", "later", "archived"):
            self.update(attention="now")
            self.update(snoozed_until=1200)
            changed = self.update(attention=placement)
            self.assertIsNone(changed["snoozed_until"])
            self.now = 1300
            self.assertEqual(self.thread()["attention"], placement)
            self.assertFalse(self.thread()["snooze_due"])
            self.now = 1000
        with self.assertRaisesRegex(CoordinationError, "Reopen"):
            self.update(snoozed_until=1200)

    def test_old_database_migrates_without_changing_placement_or_read_markers(self):
        self.finish()
        before = self.update(seen=True, attention="later")
        with self.store._connection() as db:
            db.execute("ALTER TABLE work_threads DROP COLUMN snoozed_until")
        upgraded = CoordinationStore(self.store.database_path, clock=lambda: self.now)
        thread = upgraded.threads.get("thread")
        for key in ("attention", "seen_completion_id", "handled_completion_id", "work_phase"):
            self.assertEqual(thread[key], before[key])
        self.assertIsNone(thread["snoozed_until"])

    def test_completion_notifications_suppressed_and_cursor_still_advances(self):
        item = self.finish()
        completion = item["turn_completion"]["id"]
        self.update(snoozed_until=1200)
        batch = self.sessions.completions_after(0)
        self.assertEqual(batch["events"], [])
        self.assertEqual(batch["seq"], completion)
        self.assertFalse(self.sessions.claim_notification(completion))
        self.now = 1200
        self.assertTrue(self.sessions.claim_notification(completion))

    def test_expiry_creates_one_reminder_without_changing_turn_or_read_receipts(self):
        before = self.finish()
        self.update(seen=True, snoozed_until=1200)
        self.assertEqual(self.sessions.snooze_notifications(), [])
        self.now = 1200
        event, = self.sessions.snooze_notifications()
        self.assertEqual((event["thread_id"], event["status"], event["snoozed_until"]), ("thread", "snooze", 1200))
        self.assertEqual(self.thread()["attention"], "now")
        self.assertEqual(self.thread()["turn_completion"], before["turn_completion"])
        self.assertEqual(self.thread()["seen_completion_id"], before["turn_completion"]["id"])
        self.assertEqual(self.store.threads.completion_cursor(), before["turn_completion"]["id"])
        for _ in range(3):
            self.thread()
            self.store.threads.list()
            self.assertEqual(self.sessions.snooze_notifications(), [event])
        stores = [CoordinationStore(self.store.database_path, clock=lambda: self.now) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda store: store.threads.claim_snooze_notification(event["id"]), stores))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(self.sessions.snooze_notifications(), [])
        self.assertEqual(self.sessions.snooze_notifications(include_claimed=True), [event])
        self.assertTrue(self.thread()["snooze_due"], "notification delivery does not acknowledge the reminder")

    def test_overdue_reminder_is_discovered_after_restart_without_opening_the_thread(self):
        self.update(snoozed_until=1200)
        self.now = 1200 + 86400
        restarted = CoordinationStore(self.store.database_path, clock=lambda: self.now)
        event, = restarted.threads.snooze_notifications()
        self.assertEqual(event["thread_id"], "thread")
        self.assertTrue(restarted.threads.claim_snooze_notification(event["id"]))
        again = CoordinationStore(self.store.database_path, clock=lambda: self.now)
        self.assertEqual(again.threads.snooze_notifications(), [])
        self.assertEqual(again.threads.completion_cursor(), 0)

    def test_upgrade_recovers_due_reminders_already_returned_by_an_old_runtime(self):
        self.update(snoozed_until=1200)
        self.now = 1300
        with self.store._connection() as db:
            db.execute("DROP TABLE snooze_notifications")
            db.execute("UPDATE work_threads SET attention='now' WHERE thread_id='thread'")
        upgraded = CoordinationStore(self.store.database_path, clock=lambda: self.now)
        event, = upgraded.threads.snooze_notifications()
        self.assertEqual(upgraded.threads.snooze_notifications(), [event])

    def test_cancelled_or_replaced_snoozes_cannot_claim_old_reminders(self):
        for index, action in enumerate(("resume", "resnooze", "now", "later", "archived", "message")):
            with self.subTest(action=action):
                self.now = 1000 + index * 1000
                deadline = self.now + 200
                self.update(attention="now")
                self.update(snoozed_until=deadline)
                self.now = deadline
                event, = self.sessions.snooze_notifications()
                if action == "message":
                    self.store.threads.user_message("thread")
                else:
                    body = {"resume_snooze": deadline} if action == "resume" else (
                        {"snoozed_until": deadline + 200} if action == "resnooze" else {"attention": action})
                    self.update(**body)
                self.assertEqual(self.sessions.snooze_notifications(include_claimed=True), [])
                self.assertFalse(self.sessions.claim_snooze_notification(event["id"]))
                self.assertFalse(self.store.threads.claim_snooze_notification(event["id"]))

    def test_early_resume_has_no_expiry_alert_and_resnooze_has_a_new_identity(self):
        self.update(snoozed_until=1200)
        self.update(resume_snooze=1200)
        self.now = 1200
        self.assertEqual(self.sessions.snooze_notifications(), [])
        self.update(snoozed_until=1400)
        self.now = 1400
        first, = self.sessions.snooze_notifications()
        self.assertTrue(self.sessions.claim_snooze_notification(first["id"]))
        self.update(snoozed_until=1600)
        self.now = 1600
        second, = self.sessions.snooze_notifications()
        self.assertNotEqual(second["id"], first["id"])

    def test_snooze_reminders_respect_workspace_and_validate_claim_ids(self):
        other = self.root / "other"
        other.mkdir()
        self.store.register(session_id="other", client="claude", cwd=str(other))
        self.store.threads.ensure("other")
        self.store.threads.update("other", snoozed_until=1200)
        self.update(snoozed_until=1200)
        self.now = 1200
        scoped = BrowserSessions(self.store, str(other), rpc_factory=FakeCodex)
        self.addCleanup(scoped.close)
        event, = scoped.snooze_notifications()
        self.assertEqual(event["thread_id"], "other")
        parent = next(item for item in self.sessions.snooze_notifications() if item["thread_id"] == "thread")
        self.assertFalse(scoped.claim_snooze_notification(parent["id"]))
        for value in (None, True, -1, 0, "1"):
            with self.subTest(value=value), self.assertRaises(CoordinationError):
                scoped.claim_snooze_notification(value)

    def test_pending_approval_is_preserved_while_its_notifications_are_suppressed(self):
        thread = self.sessions.create({})["session"]["thread_id"]
        self.sessions._event({"id": 42, "method": "item/commandExecution/requestApproval", "params": {"threadId": thread}})
        key = next(iter(self.sessions.requests))
        self.sessions.update_work_thread(thread, {"snoozed_until": 1200})
        self.assertEqual(self.sessions.approval_notifications(include_claimed=True), [])
        self.assertFalse(self.sessions.claim_approval_notification(key))
        self.assertEqual(len(self.sessions.pending_requests(thread)), 1)
        self.now = 1200
        self.assertEqual(len(self.sessions.approval_notifications()), 1)
        resumed = self.sessions.update_work_thread(thread, {"resume_snooze": 1200})
        self.assertTrue(resumed["needs_attention"])
        self.assertFalse(resumed["can_handle_response"])


class SnoozePushTests(unittest.TestCase):
    def test_snooze_suppresses_new_phone_notifications_and_queued_retries(self):
        fixture = push_fixtures.WebPushTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.push.subscribe(fixture.device, push_fixtures.subscription())
        fixture.complete()
        fixture.transport.response = (503, "60")
        fixture.push.tick()
        self.assertEqual(len(fixture.transport.sent), 1)
        fixture.sessions.update_work_thread("terminal", {"snoozed_until": 1400})
        fixture.now += 61
        fixture.transport.response = (201, None)
        fixture.push.tick()
        self.assertEqual(len(fixture.transport.sent), 1)
        fixture.store.threads.start_turn("terminal", turn_id="background")
        fixture.store.threads.finish_turn("terminal")
        fixture.push.tick()
        self.assertEqual(len(fixture.transport.sent), 1)


class SnoozeExpiryPushTests(unittest.TestCase):
    def setUp(self):
        self.f = push_fixtures.WebPushTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.complete()
        self.f.push.subscribe(self.f.device, push_fixtures.subscription())
        self.f.sessions.update_work_thread("terminal", {"snoozed_until": 1200})

    def test_background_expiry_pushes_to_each_phone_independently_of_desktop_once(self):
        f = self.f
        other = f.pair()
        f.push.subscribe(other, push_fixtures.subscription("two"))
        f.push.tick()
        self.assertEqual(f.transport.sent, [])
        f.now = 1200
        event, = f.sessions.snooze_notifications()
        self.assertTrue(f.sessions.claim_snooze_notification(event["id"]))
        f.push.tick()
        self.assertEqual(len(f.transport.sent), 2)
        for item in f.transport.sent:
            self.assertEqual(item["payload"]["title"], "Ribbon Field · Snooze ended")
            self.assertEqual(item["payload"]["body"], "Secret project content")
            self.assertEqual(item["payload"]["url"], "/#terminal")
        f.now += 2 * 86400
        restarted = f.manager()
        restarted.subscribe(f.device, push_fixtures.subscription())
        restarted.tick()
        restarted.tick()
        self.assertEqual(len(f.transport.sent), 2, "delivered snoozes cannot replay after their TTL or restart")
        self.assertEqual(f.sessions.rpc.calls, [], "the terminal conversation never needed to be open")
        f.sessions.update_work_thread("terminal", {"snoozed_until": f.now + 60})
        restarted.tick()
        f.now += 60
        restarted.tick()
        self.assertEqual(len(f.transport.sent), 4)
        self.assertNotEqual(f.transport.sent[0]["payload"]["tag"], f.transport.sent[2]["payload"]["tag"])

    def test_overdue_expiry_pushes_after_restart_without_a_browser_read(self):
        f = self.f
        f.now = 1200 + 2 * 86400
        restarted = f.manager()
        restarted.tick()
        self.assertEqual(len(f.transport.sent), 1)
        self.assertEqual(f.transport.sent[0]["payload"]["title"], "Ribbon Field · Snooze ended")

    def test_failed_unsent_reminder_retries_after_restart_even_after_its_ttl(self):
        f = self.f
        f.now = 1200
        f.transport.response = (503, "60")
        f.push.tick()
        self.assertEqual(len(f.transport.sent), 1)
        f.now += 7200
        f.transport.response = (201, None)
        restarted = f.manager()
        restarted.tick()
        self.assertEqual(len(f.transport.sent), 2)
        self.assertEqual(f.transport.sent[0]["payload"], f.transport.sent[1]["payload"])
        restarted.tick()
        self.assertEqual(len(f.transport.sent), 2)

    def test_resume_or_resnooze_discards_a_queued_push_retry(self):
        f = self.f
        f.now = 1200
        f.transport.response = (503, "60")
        f.push.tick()
        event, = f.sessions.snooze_notifications(include_claimed=True)
        f.sessions.update_work_thread("terminal", {"resume_snooze": 1200})
        f.transport.response = (201, None)
        # Check delivery eligibility itself, without relying on tick's cleanup.
        f.push._send(f.device, "snooze:" + str(event["id"]))
        self.assertEqual(len(f.transport.sent), 1)
        f.sessions.update_work_thread("terminal", {"snoozed_until": 1400})
        f.now = 1400
        f.transport.response = (503, "60")
        f.push.tick()
        f.sessions.update_work_thread("terminal", {"snoozed_until": 1600})
        f.now = 1461
        f.transport.response = (201, None)
        f.push.tick()
        self.assertEqual(len(f.transport.sent), 2)
        f.now = 1600
        f.push.tick()
        self.assertEqual(len(f.transport.sent), 3)


if __name__ == "__main__":
    unittest.main()
