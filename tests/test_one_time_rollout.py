"""Isolated rollout-controller checks; never address the user's app or database."""
import copy
from contextlib import closing
import importlib.util
import itertools
import json
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "scripts/one_time_rollout.py"
SPEC = importlib.util.spec_from_file_location("one_time_rollout", SOURCE)
rollout = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rollout)


def thread(**changes):
    return {"thread_id": "agent", "attention": "now", "turn_key": "turn-1",
            "turn_started_at": 1, "closing": None, "completion": None, "wake": None, **changes}


class RecoverySelectionTests(unittest.TestCase):
    def test_shutdown_keeps_children_orphaned_before_final_process_snapshot(self):
        operations = rollout.MacOperations({"installed": "/fake/Installed.app"})
        native = {"pid": 10, "uid": 501, "birth": "native"}
        backend = {"pid": 20, "uid": 501, "birth": "backend"}
        child = {"pid": 30, "uid": 501, "birth": "tool", "ppid": 20}
        original = {"native": native, "backend": backend, "url": "http://old/", "providers": [child]}
        refreshed = {**original, "providers": []}
        with patch.object(operations, "legacy", return_value=refreshed), \
                patch.object(operations, "run"), \
                patch.object(operations, "processes", side_effect=[{30: {**child, "ppid": 1}}, {}]) as processes, \
                patch.object(rollout.time, "sleep"):
            operations.stop_legacy(original)
        self.assertEqual(processes.call_count, 2)

    def test_orphaned_or_execed_child_is_not_mistaken_for_exited_process(self):
        before = {"pid": 123, "uid": 501, "birth": "time-one", "ppid": 100, "command": "shell"}
        orphaned = {**before, "ppid": 1, "command": "running-tool"}
        self.assertTrue(rollout.same_process(before, orphaned))
        self.assertFalse(rollout.same_process(before, {**orphaned, "birth": "time-two"}))
        self.assertFalse(rollout.same_process(before, None))

    def test_completed_closed_newer_and_uncertain_turns_are_not_replayed(self):
        before = thread()
        cases = [
            (None, "missing"),
            (thread(attention="archived"), "closed_or_closing"),
            (thread(closing={"status": "queued"}), "closed_or_closing"),
            (thread(turn_key="turn-2"), "newer_turn"),
            (thread(completion={"status": "completed"}), "completed"),
            (thread(wake={"dispatching": 1}), "uncertain_wake"),
            (thread(wake={"pause_kind": "stop"}), "unconfirmed_stop"),
        ]
        for after, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(rollout.restore_decision(before, after), expected)

    def test_controller_stop_can_resume_same_turn_but_does_not_override_close(self):
        before = thread()
        self.assertEqual(rollout.restore_decision(before, thread(wake={"pause_kind": "stop"}),
                         interrupted_by_controller=True), "continue")
        self.assertEqual(rollout.restore_decision(before, thread(attention="archived"),
                         interrupted_by_controller=True), "closed_or_closing")

    def test_environment_removes_python_contamination(self):
        with patch.dict(rollout.os.environ, {"PYTHONHOME": "/wrong", "PYTHONPATH": "/wrong", "VIRTUAL_ENV": "/wrong"}):
            environment = rollout.clean_environment("/candidate/Resources")
        self.assertNotIn("PYTHONHOME", environment)
        self.assertNotIn("VIRTUAL_ENV", environment)
        self.assertEqual(environment["PYTHONPATH"], "/candidate/Resources/backend")
        self.assertEqual(environment["PYTHONNOUSERSITE"], "1")

    def test_async_stop_completion_matches_but_prior_stop_or_new_turn_does_not(self):
        record = thread()
        attempt = {"stop": "accepted", "stop_started_at": 100, "observed_after_stop": thread()}
        after = thread(wake={"pause_kind": "stop"}, completion={"status": "interrupted", "completed_at": 101})
        self.assertTrue(rollout.accepted_stop_matches(record, after, attempt))
        self.assertFalse(rollout.accepted_stop_matches(record, thread(turn_key="turn-2"), attempt))
        after["completion"]["completed_at"] = 99
        self.assertFalse(rollout.accepted_stop_matches(record, after, attempt))


class FakeOperations:
    def __init__(self, root, plan):
        self.root, self.plan = root, plan
        self.events = []
        self.fail_at = None
        self.posts = []
        self.status = {"status": "serving", "database": plan["database"], "instance": "candidate-instance",
                       "url": "http://127.0.0.1:1234/", "paused": True, "busy": [], "pending": None,
                       "schema": 1, "protocol": 1}
        self.payload = root / ".state.sqlite3.runtime/payloads/candidate"
        self.status["payload"] = str(self.payload)

    def event(self, value):
        self.events.append(value)
        if value == self.fail_at:
            raise RuntimeError("injected " + value)

    def copy_bundle(self, source, target):
        self.event("copy")
        rollout.shutil.copytree(source, target, symlinks=True)

    def backup_database(self, target):
        self.event("backup")
        Path(target).write_text("isolated backup")

    def inventory(self, url):
        self.event("inventory")
        return [{"thread_id": "agent", "status": "running" if "old" in url else "saved"}]

    def api(self, url, path, body=None):
        self.posts.append((path, body))
        self.event("interrupt" if path.endswith("interrupt") else "continue")
        return {"turn": {"id": "resumed-turn"}}

    def stop_legacy(self, identity):
        self.event("stop_legacy")

    def install(self, source, directory):
        self.event("install")
        installed = Path(self.plan["installed"])
        rollout.shutil.rmtree(installed)
        rollout.shutil.copytree(source, installed)
        rollout.shutil.copytree(installed / "Contents/Resources", self.payload)

    def runtime(self, action):
        self.event(action)
        if action == "resume":
            self.status["paused"] = False
        return copy.deepcopy(self.status)

    def open_app(self):
        self.event("open")

    def confirm_continuation(self, receipt):
        self.event("confirm")
        return not receipt.get("dispatched")


class ControllerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / "rollout"
        self.directory.mkdir()
        installed, candidate = self.root / "Installed.app", self.root / "Candidate.app"
        for bundle, version in ((installed, "0.1.0"), (candidate, "0.1.3")):
            resources = bundle / "Contents/Resources"
            (resources / "backend").mkdir(parents=True)
            (resources / "backend/module.py").write_text("# " + version)
            (resources / "backend.json").write_text(json.dumps({"runtime_version": version}))
        self.plan = {"directory": str(self.directory), "installed": str(installed), "candidate": str(candidate),
                     "candidate_sha256": rollout.tree_hash(candidate), "installed_sha256": rollout.tree_hash(installed),
                     "database": str(self.root / "state.sqlite3"), "id": "test"}
        self.ops = FakeOperations(self.root, self.plan)
        self.controller = rollout.Controller(self.plan, self.ops)
        self.controller.preflight = lambda: {"url": "http://old/"}
        self.snapshot = patch.object(rollout, "database_snapshot", return_value={"agent": thread()})
        self.snapshot.start()
        self.addCleanup(self.snapshot.stop)

    def test_full_cutover_orders_stop_install_paused_check_then_resume(self):
        self.controller.execute()
        events = self.ops.events
        for earlier, later in (("stop_legacy", "install"), ("install", "bootstrap"),
                               ("bootstrap", "open"), ("status", "resume"), ("resume", "continue")):
            self.assertLess(events.index(earlier), events.index(later))
        result = rollout.read_json(self.directory / "state.json")
        self.assertEqual(result["status"], "installed")
        self.assertEqual(result["attempts"]["agent"]["resume"], "accepted")
        body = self.ops.posts[-1][1]
        self.assertTrue(body["askDispatcher"])
        self.assertEqual(body["requestId"], "test:agent")
        self.assertIn("Do not blindly repeat", body["message"])
        self.assertFalse(any("/queue" in path for path, _ in self.ops.posts))

    def test_turn_admitted_during_initial_snapshot_is_reconciled(self):
        admitted = None
        def snapshot(database):
            nonlocal admitted
            if admitted is None:
                admitted = thread(turn_started_at=rollout.time.time())
                return {}
            return {"agent": admitted}
        with patch.object(rollout, "database_snapshot", side_effect=snapshot), \
                patch.object(rollout.time, "time", side_effect=itertools.count(100)):
            self.controller.execute()
        result = rollout.read_json(self.directory / "state.json")
        self.assertEqual(result["roster"]["agent"], admitted)
        self.assertEqual(result["attempts"]["agent"]["resume"], "accepted")

    def test_success_and_failure_both_prevent_second_execution(self):
        self.ops.fail_at = "bootstrap"
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.controller.execute()
        count = len(self.ops.events)
        with self.assertRaisesRegex(RuntimeError, "already attempted"):
            self.controller.execute()
        self.assertEqual(len(self.ops.events), count)
        self.assertEqual(rollout.read_json(self.directory / "state.json")["status"], "failed")

    def test_uncertain_continuation_is_recorded_once_and_not_retried(self):
        self.ops.fail_at = "continue"
        self.controller.execute()
        result = rollout.read_json(self.directory / "state.json")
        self.assertEqual(result["status"], "installed_with_followup")
        self.assertEqual(result["attempts"]["agent"]["resume"], "uncertain")
        self.assertEqual(self.ops.events.count("continue"), 1)

    def test_dispatch_receipt_alone_is_incomplete_restoration(self):
        original = self.ops.api
        def dispatched(url, path, body=None):
            result = original(url, path, body)
            return {"dispatched": True, "receipt": {"id": 123}} if path.endswith("messages") else result
        self.ops.api = dispatched
        self.controller.execute()
        result = rollout.read_json(self.directory / "state.json")
        self.assertEqual(result["attempts"]["agent"]["resume"], "pending_review")
        self.assertEqual(result["status"], "installed_with_followup")

    def test_unconfirmed_stop_is_reported_for_followup(self):
        stopped = thread(wake={"pause_kind": "stop"}, completion={"status": "interrupted", "id": 2, "completed_at": 0})
        with patch.object(rollout, "database_snapshot", side_effect=[{"agent": thread()}, {"agent": thread()},
                          {"agent": thread()}, {"agent": stopped}, {"agent": stopped}]):
            self.controller.execute()
        result = rollout.read_json(self.directory / "state.json")
        self.assertEqual(result["attempts"]["agent"]["resume"], "skipped:unconfirmed_stop")
        self.assertEqual(result["status"], "installed_with_followup")

    def test_failed_pause_verification_never_resumes_admission(self):
        self.ops.status["paused"] = False
        with self.assertRaisesRegex(RuntimeError, "not paused"):
            self.controller.execute()
        self.assertNotIn("resume", self.ops.events)
        self.assertNotIn("continue", self.ops.events)

    def test_staged_different_payload_is_rejected(self):
        self.ops.status["pending"] = "/different/payload"
        with self.assertRaisesRegex(RuntimeError, "pending upgrade"):
            self.controller.execute()
        self.assertNotIn("resume", self.ops.events)

    def test_mutated_retained_payload_is_rejected(self):
        original = self.ops.runtime
        def mutated(action):
            if action == "bootstrap":
                (self.ops.payload / "backend/module.py").write_text("# wrong")
            return original(action)
        self.ops.runtime = mutated
        with self.assertRaisesRegex(RuntimeError, "content differs"):
            self.controller.execute()
        self.assertNotIn("resume", self.ops.events)

    def test_failed_shutdown_never_installs(self):
        self.ops.fail_at = "stop_legacy"
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.controller.execute()
        self.assertNotIn("install", self.ops.events)


class ArtifactTests(unittest.TestCase):
    def test_hash_covers_symlinks_and_executable_bits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item = root / "file"
            item.write_text("payload")
            initial = rollout.tree_hash(root)
            item.chmod(0o700)
            self.assertNotEqual(initial, rollout.tree_hash(root))
            (root / "link").symlink_to("file")
            linked = rollout.tree_hash(root)
            (root / "link").unlink()
            (root / "link").symlink_to("elsewhere")
            self.assertNotEqual(linked, rollout.tree_hash(root))

    def test_backup_keeps_live_terminal_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("CREATE TABLE evidence(value TEXT)")
                connection.execute("INSERT INTO evidence VALUES ('before')")
            operations = rollout.MacOperations({"database": str(database)})
            operations.backup_database(Path(temporary) / "backup.sqlite3")
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("INSERT INTO evidence VALUES ('terminal after backup')")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 2)
            with closing(sqlite3.connect(Path(temporary) / "backup.sqlite3")) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
