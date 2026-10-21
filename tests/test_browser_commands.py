"""Chat configuration commands must never become model prompts."""
import tempfile
import unittest
from pathlib import Path

from test_codex_app_server import FakeCodex
from agent_coord.codex_app_server import BrowserSessions, BrowserBusyError
from agent_coord.store import CoordinationError, CoordinationStore


class BrowserCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "coord.sqlite3")
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)
        self.thread = self.sessions.create({})["session"]["thread_id"]

    def command(self, text):
        return self.sessions.send(self.thread, {"message": text})

    def test_cd_handles_relative_and_quoted_paths_and_changes_next_turn(self):
        folder = self.root / "Project folder"
        folder.mkdir()
        result = self.command('/cd "Project folder"')
        self.assertEqual(result["session"]["cwd"], str(folder))
        self.assertEqual(self.store.get_session(self.thread)["cwd"], str(folder))
        self.assertEqual(self.sessions.work_thread(self.thread)["cwd"], str(folder))
        self.assertIn(str(folder), self.command("/cd")["command"]["message"])
        self.assertFalse(any(method == "turn/start" for method, _ in self.sessions.rpc.calls))
        self.sessions.send(self.thread, {"message": "Work here"})
        params = next(params for method, params in reversed(self.sessions.rpc.calls) if method == "turn/start")
        self.assertEqual(params["cwd"], str(folder))
        self.assertIn(str(folder), params["sandboxPolicy"]["writableRoots"])

    def test_cd_preserves_history_associations_and_settings_across_resume(self):
        folder = self.root / "other"
        folder.mkdir()
        self.sessions.rpc.fast_turn = True
        self.sessions.send(self.thread, {"message": "Remember this"})
        project = self.store.threads.organization.create_project("Example")
        self.store.threads.update(self.thread, project_id=project["id"], title="Keep title")
        self.command("/model other-model")
        self.command("/cd other")
        self.assertEqual(len(self.sessions.read(self.thread)["thread"]["turns"]), 1)
        self.assertEqual(self.sessions.work_thread(self.thread)["project_id"], project["id"])
        self.assertEqual(self.sessions.work_thread(self.thread)["title"], "Keep title")
        self.sessions.loaded.clear()
        self.sessions.read(self.thread)
        options = next(params for method, params in reversed(self.sessions.rpc.calls) if method == "thread/resume")
        self.assertEqual(options["cwd"], str(folder))
        self.assertEqual(options["model"], "other-model")
        self.command("/cd ..")
        self.assertEqual(self.sessions._record(self.thread)["cwd"], str(self.root))

    def test_cd_rejects_missing_outside_file_and_busy_paths(self):
        file = self.root / "file"
        file.touch()
        for value in ("missing", "..", "file", '"unterminated'):
            with self.subTest(value=value), self.assertRaises(CoordinationError):
                self.command("/cd " + value)
            self.assertEqual(self.sessions._record(self.thread)["cwd"], str(self.root))
        self.sessions.send(self.thread, {"message": "Work"})
        with self.assertRaises(BrowserBusyError):
            self.command("/cd .")

    def test_permissions_and_help_are_local_commands(self):
        self.assertIn("/yolo", self.command("/help")["command"]["message"])
        self.assertIn("/cd", self.command("/help")["command"]["message"])
        self.assertIn("/permissions", self.command("/help")["command"]["message"])
        self.assertIn("/fork", self.command("/help")["command"]["message"])
        self.assertIn("/close", self.command("/help")["command"]["message"])
        self.assertIn("workspace", self.command("/permissions")["command"]["message"].lower())
        self.assertEqual(self.command("/permissions yolo")["session"]["yolo"], 1)
        self.assertEqual(self.command("/permissions default")["session"]["yolo"], 0)
        with self.assertRaises(CoordinationError):
            self.command("/permissions unknown")
        self.assertFalse(any(method == "turn/start" for method, _ in self.sessions.rpc.calls))
        self.sessions.send(self.thread, {"message": "Work"})
        with self.assertRaises(BrowserBusyError):
            self.command("/permissions yolo")

    def test_yolo_enables_permissions_without_starting_a_turn(self):
        for command in ("/yolo", "/YOLO", "/yolo"):
            result = self.command(command)
            self.assertEqual(result["session"]["yolo"], 1)
            self.assertEqual(result["command"]["name"], "/yolo")
        self.assertFalse(any(method == "turn/start" for method, _ in self.sessions.rpc.calls))
        self.assertEqual(self.command("/permissions default")["session"]["yolo"], 0)
        for command in ("/yolo off", "/yolo extra"):
            with self.assertRaisesRegex(CoordinationError, "without arguments"):
                self.command(command)
            self.assertEqual(self.sessions._record(self.thread)["yolo"], 0)
        self.command("/yolo")
        self.sessions.send(self.thread, {"message": "Work"})
        params = next(params for method, params in reversed(self.sessions.rpc.calls) if method == "turn/start")
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertEqual(params["sandboxPolicy"]["type"], "dangerFullAccess")
        with self.assertRaises(BrowserBusyError):
            self.command("/yolo")

    def test_settings_commands_reconcile_once_and_fetch_the_catalog_once(self):
        for command in ("/model available-model high", "/effort medium", "/permissions default", "/yolo"):
            with self.subTest(command=command):
                self.sessions.rpc.calls.clear()
                result = self.command(command)
                methods = [method for method, _ in self.sessions.rpc.calls]
                self.assertEqual(methods.count("thread/read"), 1)
                self.assertEqual(methods.count("model/list"), 0 if command.startswith(("/permissions", "/yolo")) else 1)
                self.assertNotIn("turn/start", methods)
                self.assertIn("command", result)

    def test_settings_validation_still_checks_effort_and_direct_updates_reconcile(self):
        before = self.sessions._record(self.thread)
        with self.assertRaisesRegex(CoordinationError, "not supported"):
            self.command("/model available-model unsupported")
        after = self.sessions._record(self.thread)
        self.assertEqual((after["model"], after["effort"]), (before["model"], before["effort"]))
        self.sessions.rpc.calls.clear()
        self.sessions.update(self.thread, {"effort": "high"})
        methods = [method for method, _ in self.sessions.rpc.calls]
        self.assertEqual(methods.count("thread/read"), 1)
        self.assertEqual(methods.count("model/list"), 1)
        self.sessions.send(self.thread, {"message": "Work"})
        with self.assertRaises(BrowserBusyError):
            self.command("/effort medium")

    def test_configuration_with_images_is_rejected_instead_of_sent(self):
        image = {"name": "test.png", "url": "data:image/png;base64,iVBORw0KGgo="}
        for command in ("/cd .", "/permissions yolo", "/yolo", "/model", "/effort", "/help"):
            with self.subTest(command=command), self.assertRaisesRegex(CoordinationError, "Remove attached images"):
                self.sessions.send(self.thread, {"message": command, "images": [image]})
        self.assertFalse(any(method == "turn/start" for method, _ in self.sessions.rpc.calls))


if __name__ == "__main__":
    unittest.main()
