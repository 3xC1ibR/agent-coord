from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
sys.path.insert(0, str(SCRIPTS))
from agent_coord import desktop_runtime as runtime
from agent_coord import entrypoint


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.database = self.root / "state.sqlite3"
        self.directory = runtime.runtime_directory(self.database)
        self.payload = self.directory / "payloads/owner"
        self.backend = self.payload / "backend"
        shutil.copytree(SCRIPTS / "agent_coord", self.backend / "agent_coord",
                        ignore=shutil.ignore_patterns("__pycache__", "web"))
        # Different source under the owner proves dispatch, not accidental
        # compatibility. The owner intentionally has no MCP executable.
        with (self.backend / "agent_coord/store.py").open("a") as stream:
            stream.write("\n# Retained runtime differs from the client checkout.\n")
        (self.payload / "backend.json").write_text(json.dumps({"python": sys.executable}))
        self.env = dict(os.environ, AGENT_COORD_DB=str(self.database),
                        AGENT_COORD_SESSION_ID="entrypoint-test", AGENT_COORD_CLIENT="claude",
                        PYTHONPATH=str(SCRIPTS))
        self.env.pop("CLAUDE_ENV_FILE", None)
        code = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                "from agent_coord.cli import main; raise SystemExit(main())")
        subprocess.run([sys.executable, "-I", "-c", code, str(self.backend), "register",
                        "--client", "claude", "--cwd", str(self.root)],
                       env=self.env, capture_output=True, text=True, check=True)
        self.owner = runtime.lock_file(self.directory / "owner.lock")
        self.addCleanup(os.close, self.owner)
        # Load the retained backend in an isolated package namespace. The
        # fixture owns the same kernel lock as a runtime and executes complete
        # requests against one shared store, rather than returning status for
        # every action and accidentally allowing client-side database writes.
        package = "retained_owner_" + self.root.name.replace("-", "_")
        spec = importlib.util.spec_from_file_location(package, self.backend / "agent_coord/__init__.py",
            submodule_search_locations=[str(self.backend / "agent_coord")])
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        self.addCleanup(lambda: [sys.modules.pop(name, None) for name in list(sys.modules)
                                if name == package or name.startswith(package + ".")])
        spec.loader.exec_module(module)
        owner_runtime = importlib.import_module(package + ".desktop_runtime")
        owner_runtime._OWNED_DATABASES.add(str(self.database))
        self.owner_store = module.CoordinationStore(self.database)
        self.execute_request = importlib.import_module(package + ".runtime_requests").execute_request
        self.status = {"instance": "test-instance", "database": str(self.database),
                       "protocol": runtime.PROTOCOL_VERSION, "payload": str(self.payload), "client_requests": 1}
        self.actions = []
        self.legacy_owner = False
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if (self.headers.get("Authorization") != "Bearer test-token"
                        or body.get("instance") != "test-instance"):
                    self.send_error(403)
                    return
                fixture.actions.append(body.get("action"))
                result = fixture.status
                if fixture.legacy_owner and body.get("action") == "identity":
                    result = {**fixture.status, "error": "Unknown runtime control action."}
                elif body.get("action") == "client-request":
                    result = {**fixture.status, **fixture.execute_request(fixture.owner_store, body)}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.record = {**self.status, "control_token": "test-token",
                       "control_url": f"http://127.0.0.1:{self.server.server_port}/"}
        self.save_record()

    def close_server(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def save_record(self):
        runtime.atomic_json(self.directory / "discovery.json", self.record)

    def invoke(self, script="agent-coord", *args, input=None, env=None):
        return subprocess.run([sys.executable, str(SCRIPTS / script), *args],
                              input=input, env=env or self.env, cwd=self.root,
                              capture_output=True, text=True, timeout=10)

    def test_cli_uses_live_owner_and_preserves_explicit_database(self):
        self.record["payload"] = "/untrusted/stale/discovery/path"
        self.save_record()
        for options in (["--db", "state.sqlite3"], ["--db=state.sqlite3"]):
            with self.subTest(options=options):
                env = dict(self.env, AGENT_COORD_DB=str(self.root / "wrong.sqlite3"))
                result = self.invoke("agent-coord", *options, "status", env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["session_id"], "entrypoint-test")
                self.assertFalse((self.root / "wrong.sqlite3").exists())

    def test_hook_preserves_stdin_and_exports(self):
        export = self.root / "claude.env"
        result = self.invoke("agent-coord-hook", input=json.dumps({
            "hook_event_name": "SessionStart", "session_id": "entrypoint-test", "cwd": str(self.root),
        }), env=dict(self.env, CLAUDE_ENV_FILE=str(export)))
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertNotIn("systemMessage", response)
        self.assertIn("hookSpecificOutput", response)
        self.assertIn("entrypoint-test", export.read_text())
        self.assertIn(str(self.backend), export.read_text())

    def test_owner_routing_authenticates_retained_code_and_execution_without_status(self):
        result = self.invoke("agent-coord", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.actions, ["identity", "identity", "client-request"])

    def test_legacy_owner_falls_back_only_for_authenticated_unsupported_action(self):
        self.legacy_owner = True
        result = self.invoke("agent-coord", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.actions, ["identity", "status", "identity", "status", "client-request"])
        self.legacy_owner = False
        self.actions.clear()
        self.status["error"] = "Owner identity rejected."
        result = self.invoke("agent-coord", "status")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.actions, ["identity"])

    def test_identity_protocol_and_instance_must_match(self):
        for invalid in ({"protocol": -1}, {"instance": "other-instance"}):
            with self.subTest(invalid=invalid):
                original = dict(self.status)
                self.status.update(invalid)
                result = self.invoke("agent-coord", "status")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.status = original

    def test_mcp_checkpoint_works_without_retained_mcp_executable(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "update_checkpoint", "arguments": {"phase": "validation", "summary": "Owner MCP verified."}}}
        result = self.invoke("agent-coord-mcp", input=json.dumps(request) + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertFalse(response["result"].get("isError"), response)
        saved = self.invoke("agent-coord", "thread", "show", "--session-id", "entrypoint-test")
        self.assertEqual(json.loads(saved.stdout)["checkpoint"]["summary"], "Owner MCP verified.")

    def test_direct_mismatched_library_access_still_fails(self):
        result = subprocess.run([sys.executable, "-c", "from agent_coord import CoordinationStore; CoordinationStore()"],
                                env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different Ribbon Field runtime", result.stderr)

    def test_held_owner_with_bad_discovery_fails_closed(self):
        for bad in ({"control_token": "wrong"}, {"database": str(self.root / "other")},
                    {"control_url": "http://127.0.0.1:1/"}):
            with self.subTest(bad=bad):
                runtime.atomic_json(self.directory / "discovery.json", {**self.record, **bad})
                result = self.invoke("agent-coord", "status")
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("error", json.loads(result.stderr))
        (self.directory / "discovery.json").unlink()
        result = self.invoke("agent-coord", "status")
        self.assertEqual(result.returncode, 2)
        self.assertIn("owner is unavailable", result.stderr)

    def test_external_payload_and_symlink_escape_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        alias = self.directory / "payloads/alias"
        alias.symlink_to(outside)
        for path in (outside, alias):
            with self.subTest(path=path):
                self.status["payload"] = str(path)
                result = self.invoke("agent-coord", "status")
                self.assertEqual(result.returncode, 2)
                self.assertIn("retained payload", result.stderr)

    def test_malformed_owner_metadata_blocks_write_hooks(self):
        hook = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Write",
                           "session_id": "entrypoint-test", "cwd": str(self.root),
                           "tool_input": {"file_path": str(self.root / "example.py")}})
        for invalid in ([], None, "invalid", {}):
            with self.subTest(discovery=invalid):
                runtime.atomic_json(self.directory / "discovery.json", invalid)
                result = self.invoke("agent-coord-hook", input=hook)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("error", json.loads(result.stderr))
        self.save_record()
        for invalid in ([], None, {**self.record, "payload": None}):
            with self.subTest(status=invalid):
                self.status = invalid
                result = self.invoke("agent-coord-hook", input=hook)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("error", json.loads(result.stderr))

    def test_no_owner_runs_local_and_install_does_not_pin_owner(self):
        database = self.root / "local.sqlite3"
        result = self.invoke("agent-coord", "--db", str(database), "register", "--client", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.invoke("agent-coord", "install-cli", "--bin-dir", str(self.root / "bin"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["target"], str(SCRIPTS / "agent-coord"))

    def test_database_option_inside_child_arguments_is_untouched(self):
        args = ["job", "start", "--", "child", "--db", "child.sqlite3"]
        with patch.dict(os.environ, self.env):
            database, remaining = entrypoint.client_database("cli", args)
        self.assertEqual(database, self.database)
        self.assertEqual(remaining, args)
