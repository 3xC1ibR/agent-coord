from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MACOS = ROOT / "desktop/macos"
SPEC = importlib.util.spec_from_file_location("macos_build", MACOS / "build.py")
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


class DesktopBackendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="desktop test ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def start(self, *, environment=None, db=None):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "plugins/agent-coord/scripts"),
                   PYTHONWARNINGS="error::ResourceWarning")
        env.update(environment or {})
        process = subprocess.Popen(
            [sys.executable, "-u", str(MACOS / "backend.py"), "--db", str(db or self.root / "state.sqlite3")],
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        def cleanup():
            if not process.stdin.closed:
                process.stdin.close()
            try:
                process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            process.stdout.close()
            process.stderr.close()

        self.addCleanup(cleanup)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=10), "Backend failed to announce its URL")
        line = process.stdout.readline()
        if not line:
            process.wait(timeout=3)
            self.fail(process.stderr.read().decode())
        return process, json.loads(line)["url"]

    def read_json(self, url):
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.load(response)

    def test_start_serves_existing_ui_with_a_private_loopback_port(self):
        process, url = self.start()
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        with urllib.request.urlopen(url, timeout=5) as response:
            self.assertIn(b"Agent Coord", response.read())
        self.assertTrue(self.read_json(url + "api/browser/config")["token"])
        self.assertEqual(self.read_json(url + "api/browser/threads")["data"], [])
        self.assertIsNone(process.poll())

    def test_parent_pipe_eof_stops_the_server_and_open_event_stream(self):
        process, url = self.start()
        with urllib.request.urlopen(url + "api/browser/events", timeout=5) as response:
            self.assertEqual(response.readline(), b"retry: 1500\n")
            process.stdin.close()
            self.assertEqual(process.wait(timeout=6), 0)
        self.assertNotIn(b"ResourceWarning", process.stderr.read())
        with self.assertRaises(OSError):
            urllib.request.urlopen(url, timeout=1)

    def test_sigterm_uses_the_same_graceful_shutdown(self):
        process, _ = self.start()
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=6), 0)
        self.assertEqual(process.stderr.read(), b"")

    def test_stopping_one_backend_does_not_stop_another(self):
        first, first_url = self.start()
        second, second_url = self.start()
        self.assertNotEqual(first_url, second_url)
        first.stdin.close()
        self.assertEqual(first.wait(timeout=6), 0)
        self.assertTrue(self.read_json(second_url + "api/browser/config")["token"])
        self.assertIsNone(second.poll())

    def test_parent_exit_also_stops_its_codex_subprocess(self):
        tools = self.root / "bin"
        tools.mkdir()
        fake = tools / "codex"
        fake.write_text("#!/usr/bin/env python3\n" +
            "import json, os, pathlib, sys\n" +
            "pathlib.Path(os.environ['DESKTOP_TEST_PID']).write_text(str(os.getpid()))\n" +
            "for line in sys.stdin:\n" +
            "    request = json.loads(line)\n" +
            "    if 'id' in request:\n" +
            "        result = {'data': []} if request.get('method') == 'model/list' else {}\n" +
            "        print(json.dumps({'id': request['id'], 'result': result}), flush=True)\n")
        fake.chmod(0o755)
        pidfile = self.root / "codex.pid"
        process, url = self.start(environment={
            "PATH": str(tools) + os.pathsep + os.environ["PATH"], "DESKTOP_TEST_PID": str(pidfile),
        })
        self.assertEqual(self.read_json(url + "api/browser/models")["data"], [])
        pid = int(pidfile.read_text())
        os.kill(pid, 0)
        process.stdin.close()
        self.assertEqual(process.wait(timeout=6), 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertNotIn(b"ResourceWarning", process.stderr.read())

    def test_invalid_database_reports_a_startup_error(self):
        with subprocess.Popen(
            [sys.executable, str(MACOS / "backend.py"), "--db", str(self.root)],
            env=dict(os.environ, PYTHONPATH=str(ROOT / "plugins/agent-coord/scripts")),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as process:
            stdout, stderr = process.communicate(timeout=6)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(stdout, b"")
            self.assertTrue(stderr)


class DesktopPackagingTests(unittest.TestCase):
    def test_snapshot_contains_all_current_backend_and_web_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = build.copy_backend(root)
            self.assertIn("agent_coord/ui.py", snapshot)
            self.assertIn("agent_coord/web/index.html", snapshot)
            self.assertIn("agent_coord/web/notifications.js", snapshot)
            self.assertFalse(any("__pycache__" in path for path in snapshot))
            for relative in snapshot:
                self.assertEqual((root / "backend" / relative).read_bytes(), (build.SCRIPTS / relative).read_bytes())

    def test_install_does_not_replace_an_unrelated_directory_or_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unrelated = root / "Agent Coord.app"
            unrelated.mkdir()
            keep = unrelated / "keep.txt"
            keep.write_text("user data")
            with self.assertRaises(ValueError):
                build.replace_app(root, unrelated)
            self.assertEqual(keep.read_text(), "user data")
            link = root / "linked.app"
            link.symlink_to(unrelated)
            with self.assertRaises(ValueError):
                build.check_destination(link)

    @unittest.skipUnless(sys.platform == "darwin", "macOS bundle tools")
    def test_install_replaces_only_an_identified_agent_coord_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "source.app", root / "Agent Coord.app"
            for app in (source, destination):
                (app / "Contents").mkdir(parents=True)
                (app / "Contents/Info.plist").write_text(json.dumps({"CFBundleIdentifier": build.BUNDLE_ID}))
            (source / "new.txt").write_text("new")
            (destination / "old.txt").write_text("old")
            build.replace_app(source, destination)
            self.assertEqual((destination / "new.txt").read_text(), "new")
            self.assertFalse((destination / "old.txt").exists())


if __name__ == "__main__":
    unittest.main()
