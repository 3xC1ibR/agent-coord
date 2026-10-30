from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord import cli, desktop_runtime as runtime, runtime_requests as requests
from agent_coord.context import caller_directory, caller_environment, caller_session, request_ownership
from agent_coord.store import ConflictError, CoordinationError, CoordinationStore


class RuntimeRequestsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        for name in ("one", "two"):
            self.store.register(session_id=name, client="codex", cwd=str(self.root))

    def request(self, kind, payload, *, session="one", environment=None, directory=None):
        return {"kind": kind, "payload": payload, "context": {
            "environment": environment or {}, "session": session,
            "directory": str(directory or self.root)}}

    def execute(self, kind, payload, **context):
        return requests.execute_request(self.store, self.request(kind, payload, **context))

    def owner_transport(self):
        stack = contextlib.ExitStack()
        descriptor = runtime.lock_file(runtime.runtime_directory(self.store.database_path) / "owner.lock")
        stack.callback(os.close, descriptor)
        stack.enter_context(patch("agent_coord.desktop_runtime.discover", return_value={"client_requests": 1}))
        control = stack.enter_context(patch("agent_coord.desktop_runtime.control",
            side_effect=lambda record, action, **values: self.execute_owned(values["request"])))
        self.addCleanup(stack.close)
        return control

    def execute_owned(self, request):
        token = request_ownership.set(frozenset({(os.getpid(), str(self.store.database_path))}))
        try:
            return requests.execute_request(self.store, request)
        finally:
            request_ownership.reset(token)

    def test_cli_mutation_executes_with_shared_store_and_request_identity(self):
        with patch("agent_coord.cli.CoordinationStore", side_effect=AssertionError("must reuse owner store")):
            response = self.execute("cli", {"argv": ["checkpoint", "--json", '{"phase":"validation","summary":"Checked."}']})
        self.assertNotIn("failure", response)
        self.assertEqual(self.store.threads.get("one")["checkpoint"]["summary"], "Checked.")
        self.assertIsNone(self.store.threads.get("two")["checkpoint"])

    def test_cli_stdin_and_relative_directory_do_not_use_runtime_process_context(self):
        target = self.root / "project"
        target.mkdir()
        response = self.execute("cli", {"argv": ["register", "--client", "codex", "--cwd", "project"]})
        self.assertEqual(response["result"]["cwd"], str(target))
        with patch("sys.stdin", io.StringIO("runtime stdin must remain untouched")):
            response = self.execute("cli", {"argv": ["checkpoint", "--json", "-"],
                "stdin": '{"phase":"validation","summary":"Caller input."}'})
            self.assertEqual(sys.stdin.read(), "runtime stdin must remain untouched")
        self.assertNotIn("failure", response)
        self.assertEqual(self.store.threads.get("one")["checkpoint"]["summary"], "Caller input.")

    def test_read_only_status_completes_while_external_writer_holds_lock(self):
        writer = sqlite3.connect(self.store.database_path)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        response = self.execute("cli", {"argv": ["status"]})
        elapsed = time.monotonic() - started
        writer.rollback()
        self.assertNotIn("failure", response)
        self.assertEqual(response["result"]["session_id"], "one")
        self.assertLess(elapsed, 1)

    def test_query_classification_enforces_database_read_only(self):
        def write(arguments, *, store):
            with store._connection() as db:
                db.execute("UPDATE sessions SET name='not permitted'")
        with patch("agent_coord.cli.run", side_effect=write):
            response = self.execute("cli", {"argv": ["activity", "settings"]})
        self.assertEqual(response["failure"]["type"], "OperationalError")
        self.assertIn("readonly", response["failure"]["message"])
        self.assertIsNone(self.store.get_session("one")["name"])

    def test_concurrent_contexts_do_not_mutate_environment_or_leak_identity(self):
        barrier = threading.Barrier(2)
        name = "test.context-isolation"
        def observe(store):
            barrier.wait(timeout=5)
            return [caller_session(), caller_environment().get("AGENT_COORD_CLIENT"), caller_directory()]
        requests.register_operation(name, observe)
        self.addCleanup(requests._OPERATIONS.pop, name)
        original = dict(os.environ)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda identity: self.execute("operation", {"name": name}, session=identity,
                environment={"AGENT_COORD_CLIENT": identity}, directory=self.root / identity), ("one", "two")))
        self.assertEqual(dict(os.environ), original)
        for identity, result in zip(("one", "two"), results):
            self.assertEqual(result["result"], [identity, identity, str(self.root / identity)])

    def test_hook_client_context_preserves_claude_environment_export(self):
        destination = self.root / "claude-environment"
        with patch.dict(os.environ, {"AGENT_COORD_CLIENT": "codex"}), \
                patch("agent_coord.hook.enable_from_environment", return_value=None):
            response = self.execute("hook", {"input": {"hook_event_name": "SessionStart",
                "session_id": "claude-one", "cwd": str(self.root)}}, session=None,
                environment={"AGENT_COORD_CLIENT": "claude", "CLAUDE_ENV_FILE": str(destination)})
        self.assertNotIn("failure", response)
        self.assertEqual(self.store.get_session("claude-one")["client"], "claude")
        self.assertIn("AGENT_COORD_SESSION_ID=claude-one", destination.read_text())

    def test_mcp_uses_bound_caller_and_does_not_construct_another_store(self):
        with patch("agent_coord.checkpoint_mcp.CoordinationStore", side_effect=AssertionError("new store")):
            response = self.execute("mcp", {"name": "update_checkpoint", "arguments": {
                "phase": "validation", "summary": "MCP owner request."}}, session="two")
        self.assertNotIn("failure", response)
        self.assertEqual(self.store.threads.get("two")["checkpoint"]["summary"], "MCP owner request.")
        response = self.execute("mcp", {"name": "update_checkpoint", "arguments": {"session_id": "two", "summary": "wrong caller"}})
        self.assertIn("Caller identity mismatch", response["failure"]["message"])

    def test_transport_failure_never_replays_or_falls_back_to_local_mutation(self):
        control = self.owner_transport()
        control.side_effect = TimeoutError("response lost")
        with self.assertRaisesRegex(CoordinationError, "outcome is unavailable"):
            requests.forward_request("cli", {"argv": ["status"]}, self.store.database_path, timeout=.25)
        self.assertEqual(control.call_count, 1)
        self.assertEqual(control.call_args.kwargs["timeout"], .25)

    def test_cli_uses_authenticated_http_without_opening_a_client_store(self):
        store = self.store
        execute_owned = self.execute_owned
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.headers.get("Authorization") != "Bearer test-token":
                    self.send_error(403)
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                result = {"instance": "test-owner", "database": str(store.database_path),
                          **execute_owned(body)}
                data = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join, 3)
        self.addCleanup(server.shutdown)
        record = {"client_requests": 1, "instance": "test-owner", "database": str(store.database_path),
                  "control_token": "test-token", "control_url": f"http://127.0.0.1:{server.server_port}/"}
        descriptor = runtime.lock_file(runtime.runtime_directory(store.database_path) / "owner.lock")
        self.addCleanup(os.close, descriptor)
        output = io.StringIO()
        with patch("agent_coord.desktop_runtime.discover", return_value=record), \
                patch("agent_coord.cli.CoordinationStore", side_effect=AssertionError("client store")), \
                patch.dict(os.environ, {"AGENT_COORD_SESSION_ID": "one"}), contextlib.redirect_stdout(output):
            result = cli.main(["--db", str(store.database_path), "checkpoint", "--json",
                               '{"phase":"validation","summary":"HTTP owner path."}'])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "ok")
        with store.reading():
            self.assertEqual(store.threads.get("one")["checkpoint"]["summary"], "HTTP owner path.")

    def test_cli_database_cannot_disagree_with_authenticated_owner(self):
        response = self.execute("cli", {"argv": ["--db", str(self.root / "another.sqlite3"), "status"]})
        self.assertIn("does not match", response["failure"]["message"])

    def test_metadata_batch_is_read_only_and_preserves_requested_order(self):
        writer = sqlite3.connect(self.store.database_path)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        response = self.execute("cli", {"argv": ["thread", "metadata", "--thread-id", "two", "--thread-id", "one"]})
        writer.rollback()
        self.assertNotIn("failure", response)
        self.assertEqual([row["thread_id"] for row in response["result"]["data"]], ["two", "one"])

    def test_thread_show_and_close_status_do_not_ensure_or_migrate(self):
        with patch.object(self.store.threads, "ensure", side_effect=AssertionError("read cannot ensure")):
            for command in ("show", "close-status"):
                response = self.execute("cli", {"argv": ["thread", command, "--session-id", "one"]})
                self.assertNotIn("failure", response)

    def test_no_owner_does_not_consume_stdin(self):
        args = cli._parser().parse_args(["--db", str(self.store.database_path), "checkpoint", "--json", "-"])
        with patch("sys.stdin", io.StringIO("input")), patch("agent_coord.desktop_runtime.owner_available", return_value=True):
            self.assertEqual(requests.forward_request("cli", lambda: requests._cli_payload(args,
                ["checkpoint", "--json", "-"]), self.store.database_path), (False, None))
            self.assertEqual(sys.stdin.read(), "input")

    def test_local_operation_reserves_ownership_until_complete(self):
        directory = runtime.runtime_directory(self.store.database_path)
        def local():
            self.assertTrue(requests.owns_database(self.store.database_path))
            # This is the same election a competing runtime performs, after
            # standalone selection and before the command's first write.
            with self.assertRaises(BlockingIOError):
                runtime.lock_file(directory / "owner.lock")
            self.store.touch("one")
            return "completed"
        self.assertEqual(requests.run_local_or_forward("operation", {}, local,
            self.store.database_path), "completed")
        self.assertTrue(runtime.owner_available(directory))
        self.assertFalse(requests.owns_database(self.store.database_path))

    def test_runtime_winning_election_routes_without_running_local_work(self):
        control = self.owner_transport()
        local = unittest.mock.Mock(side_effect=AssertionError("must route"))
        result = requests.run_local_or_forward("cli", {"argv": ["status", "--session-id", "one"]}, local, self.store.database_path)
        self.assertEqual(result["session_id"], "one")
        self.assertEqual(control.call_count, 1)
        local.assert_not_called()

    def test_local_ownership_is_reentrant_and_isolates_other_threads(self):
        reached = threading.Event()
        def contender():
            self.assertFalse(requests.owns_database(self.store.database_path))
            reached.set()
            with requests.owner_operation(self.store.database_path) as owned:
                self.assertTrue(owned)
                return True
        with ThreadPoolExecutor(max_workers=1) as pool:
            with requests.owner_operation(self.store.database_path) as first:
                self.assertTrue(first)
                with requests.owner_operation(self.store.database_path) as nested:
                    self.assertTrue(nested)
                future = pool.submit(contender)
                self.assertTrue(reached.wait(2))
                self.assertFalse(future.done())
            self.assertTrue(future.result(timeout=2))

    def test_independent_process_cannot_win_election_during_local_operation(self):
        path = runtime.runtime_directory(self.store.database_path) / "owner.lock"
        code = """import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(7)
finally:
    os.close(fd)
"""
        def election():
            return subprocess.run([sys.executable, "-c", code, str(path)],
                                  capture_output=True, timeout=5).returncode
        with requests.owner_operation(self.store.database_path):
            self.assertEqual(election(), 7)
        self.assertEqual(election(), 0)

    def test_zero_timeout_does_not_wait_for_another_local_operation(self):
        def contender():
            started = time.monotonic()
            with self.assertRaises(sqlite3.OperationalError) as raised:
                with requests.owner_operation(self.store.database_path, timeout=0):
                    self.fail("another request owns this operation")
            self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
            return time.monotonic() - started
        with ThreadPoolExecutor(max_workers=1) as pool:
            with requests.owner_operation(self.store.database_path):
                self.assertLess(pool.submit(contender).result(timeout=2), .5)

    def test_library_write_waits_for_other_process_standalone_reservation(self):
        path = runtime.runtime_directory(self.store.database_path) / "owner.lock"
        code = """import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print('ready', flush=True)
sys.stdin.readline()
os.close(fd)
"""
        with subprocess.Popen([sys.executable, "-c", code, str(path)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True) as process:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            observed = threading.Event()
            def discover(*args, **kwargs):
                observed.set()
                return None
            def change():
                with self.store._connection() as db:
                    db.execute("UPDATE sessions SET name='after reservation' WHERE session_id='one'")
            with patch("agent_coord.desktop_runtime.discover", side_effect=discover), \
                    ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(change)
                self.assertTrue(observed.wait(2))
                self.assertFalse(future.done())
                with self.store.reading():
                    self.assertIsNone(self.store.get_session("one")["name"])
                process.stdin.write("release\n")
                process.stdin.flush()
                future.result(timeout=2)
            self.assertEqual(process.wait(timeout=2), 0)
        self.assertEqual(self.store.get_session("one")["name"], "after reservation")

    def test_library_connection_remains_readonly_under_verified_runtime(self):
        self.owner_transport()
        with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
            with self.store._connection() as db:
                db.execute("UPDATE sessions SET name='forbidden'")

    def test_library_zero_timeout_never_probes_contended_owner(self):
        descriptor = runtime.lock_file(runtime.runtime_directory(self.store.database_path) / "owner.lock")
        try:
            with self.store.connection_timeout(0), patch("agent_coord.desktop_runtime.discover") as discover:
                with self.assertRaises(sqlite3.OperationalError) as raised:
                    with self.store._connection():
                        self.fail("owner is still held")
            self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
            discover.assert_not_called()
        finally:
            os.close(descriptor)

    def test_owner_exit_before_submission_rechecks_election(self):
        descriptor = runtime.lock_file(runtime.runtime_directory(self.store.database_path) / "owner.lock")
        def release(*args, **kwargs):
            os.close(descriptor)
            return False, None
        local = unittest.mock.Mock(return_value="local")
        with patch.object(requests, "forward_request", side_effect=release) as forward:
            self.assertEqual(requests.run_local_or_forward("operation", {}, local,
                self.store.database_path), "local")
        self.assertEqual(forward.call_count, 1)
        self.assertEqual(local.call_count, 1)

    def test_unavailable_owner_and_unknown_response_never_run_local_work(self):
        control = self.owner_transport()
        local = unittest.mock.Mock(side_effect=AssertionError("local replay"))
        with patch("agent_coord.desktop_runtime.discover", return_value=None):
            with self.assertRaisesRegex(CoordinationError, "owner is unavailable"):
                requests.run_local_or_forward("operation", {}, local, self.store.database_path, timeout=.01)
        control.assert_not_called()
        control.side_effect = TimeoutError("response lost")
        with self.assertRaisesRegex(CoordinationError, "outcome is unavailable"):
            requests.run_local_or_forward("operation", {}, local, self.store.database_path)
        self.assertEqual(control.call_count, 1)
        local.assert_not_called()

    def test_starting_owner_retries_handshake_without_submitting_twice(self):
        control = self.owner_transport()
        local = unittest.mock.Mock(side_effect=AssertionError("must route"))
        with patch("agent_coord.desktop_runtime.discover", side_effect=[None, {"client_requests": 1}]) as discover:
            result = requests.run_local_or_forward("cli", {"argv": ["status", "--session-id", "one"]},
                local, self.store.database_path)
        self.assertEqual(result["session_id"], "one")
        self.assertEqual(discover.call_count, 2)
        self.assertEqual(control.call_count, 1)
        local.assert_not_called()

    def test_local_pure_query_does_not_reserve_owner_lock(self):
        directory = runtime.runtime_directory(self.store.database_path)
        result = requests.run_local_or_forward("operation", {}, lambda: runtime.owner_available(directory),
            self.store.database_path, read_only=True)
        self.assertTrue(result)

    def test_standalone_cli_mutation_holds_owner_lock_across_run(self):
        argv = ["--db", str(self.store.database_path), "checkpoint", "--json", "{}"]
        arguments = cli._parser().parse_args(argv)
        def run(arguments):
            self.assertTrue(requests.owns_database(self.store.database_path))
            self.assertFalse(runtime.owner_available(runtime.runtime_directory(self.store.database_path)))
            return "local"
        with patch("agent_coord.cli.run", side_effect=run):
            self.assertEqual(requests.forward_cli(arguments, argv), (True, "local"))

    def test_standalone_hook_and_mcp_hold_owner_lock_across_handler(self):
        from agent_coord import hook, checkpoint_mcp
        def handler(*args, **kwargs):
            self.assertTrue(requests.owns_database(self.store.database_path))
            return {"status": "ok"}
        with patch.dict(os.environ, {"AGENT_COORD_DB": str(self.store.database_path),
                    "AGENT_COORD_SESSION_ID": "one", "AGENT_COORD_CLIENT": "codex"}), \
                patch("agent_coord.hook.handle", side_effect=handler), \
                patch("agent_coord.checkpoint_mcp.update_checkpoint", side_effect=handler), \
                patch("sys.stdin", io.StringIO('{"hook_event_name":"SessionStart"}')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hook.main(), 0)
            self.assertEqual(checkpoint_mcp.call_tool("update_checkpoint", {}, remote=True), {"status": "ok"})

    def test_standalone_inbox_wait_releases_owner_between_polls(self):
        argv = ["--db", str(self.store.database_path), "inbox", "--session-id", "one", "--wait", "--timeout", "1"]
        arguments = cli._parser().parse_args(argv)
        def sleep(_):
            self.assertTrue(runtime.owner_available(runtime.runtime_directory(self.store.database_path)))
            self.store.send_message(sender_session_id="two", recipient_session_id="one", body="arrived")
        with patch("agent_coord.runtime_requests.time.sleep", side_effect=sleep), \
                patch.dict(os.environ, {"AGENT_COORD_SESSION_ID": "one"}):
            forwarded, messages = requests.forward_cli(arguments, argv)
        self.assertTrue(forwarded)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["body"], "arrived")

    def test_app_control_wait_releases_ownership_between_finite_requests(self):
        argv = ["--db", str(self.store.database_path), "thread", "request-status", "--request-id", "request-one", "--wait", "1"]
        arguments = cli._parser().parse_args(argv)
        def run(arguments):
            self.assertEqual(arguments.wait, 0)
            self.assertTrue(requests.owns_database(self.store.database_path))
            return {"request_id": "request-one", "status": "queued"}
        def sleep(_):
            self.assertTrue(runtime.owner_available(runtime.runtime_directory(self.store.database_path)))
        def poll(store, request_id):
            self.assertTrue(requests.owns_database(store.database_path))
            return {"request_id": request_id, "status": "completed"}
        with patch("agent_coord.cli.run", side_effect=run), \
                patch("agent_coord.runtime_requests.time.sleep", side_effect=sleep), \
                patch.dict(requests._OPERATIONS, {"app.wait-poll": poll}):
            self.assertEqual(requests.forward_cli(arguments, argv)[1]["status"], "completed")

    def test_supervisor_routes_claim_progress_finish_and_runs_child_once(self):
        from agent_coord.jobs import ManagedJobs, supervise
        from agent_coord.runtime_metadata import install_operations
        install_operations()
        jobs = ManagedJobs(self.store)
        marker = self.root / "job-executions"
        job = jobs.create("one", [sys.executable, "-c",
            f"from pathlib import Path; import time; Path({str(marker)!r}).open('a').write('once\\n'); time.sleep(.1)"])
        with self.store._connection() as db:
            db.execute("UPDATE jobs SET state='starting', supervisor_token='owner-token' WHERE id=?", (job["id"],))
        control = self.owner_transport()
        result = supervise(self.store, job["id"], "owner-token", poll_seconds=.01, heartbeat_seconds=.01, grace_seconds=.1)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(supervise(self.store, job["id"], "owner-token")["state"], "completed")
        self.assertEqual(marker.read_text(), "once\n")
        names = [call.kwargs["request"]["payload"]["name"] for call in control.call_args_list]
        self.assertIn("jobs.claim", names)
        self.assertIn("jobs.save_progress", names)
        self.assertIn("jobs.finish", names)

    def test_permanent_job_publication_error_does_not_retry(self):
        from agent_coord.jobs import _finish
        with patch("agent_coord.jobs._touch_supervisor"), \
                patch("agent_coord.runtime_metadata.job_operation", side_effect=CoordinationError("invalid operation")) as operation:
            with self.assertRaisesRegex(CoordinationError, "invalid operation"):
                _finish(self.store, "one-job", "token", state="completed", exit_code=0, error=None, discarded=0)
        self.assertEqual(operation.call_count, 1)

    def test_metadata_reads_bypass_owner_election(self):
        from agent_coord.runtime_metadata import metadata_store
        with patch.object(requests, "owner_operation", side_effect=AssertionError("read must not elect")):
            self.assertEqual(metadata_store(self.store).get_session("one")["session_id"], "one")

    def test_error_envelope_preserves_conflicts_and_sqlite_codes(self):
        conflicts = [{"session_id": "two", "overlap": ["file.py"]}]
        with self.assertRaises(ConflictError) as raised:
            requests.raise_failure(requests.failure(ConflictError(conflicts)))
        self.assertEqual(raised.exception.conflicts, conflicts)
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = 517
        with self.assertRaises(sqlite3.OperationalError) as raised:
            requests.raise_failure(requests.failure(error))
        self.assertEqual(raised.exception.sqlite_errorcode, 517)

    def test_inbox_wait_cursor_observes_message_delivered_by_another_hook(self):
        started = self.execute("operation", {"name": "inbox.wait-start", "kwargs": {"session_id": "one"}})["result"]
        message = self.store.send_message(sender_session_id="two", recipient_session_id="one", body="new message")
        self.store.inbox("one", mark_delivered=True)
        response = self.execute("operation", {"name": "inbox.wait-poll", "kwargs": {
            "session_id": "one", "started_at": started["started_at"], "after": started["after"], "mark_delivered": True}})
        self.assertEqual([item["id"] for item in response["result"]], [message["id"]])

    def test_peeking_inbox_wait_keeps_target_distinct_from_actor(self):
        started = self.execute("operation", {"name": "inbox.wait-start", "kwargs": {
            "session_id": "two", "mark_delivered": False}})["result"]
        message = self.store.send_message(sender_session_id="one", recipient_session_id="two", body="unread")
        response = self.execute("operation", {"name": "inbox.wait-poll", "kwargs": {
            "session_id": "two", "started_at": started["started_at"], "after": started["after"], "mark_delivered": False}})
        self.assertEqual(response["result"][0]["id"], message["id"])
        self.assertIsNone(response["result"][0]["delivered_at"])

    def test_ask_wait_creates_once_then_polls_finite_requests(self):
        self.owner_transport()
        argv = ["--db", str(self.store.database_path), "ask", "--from-session", "one", "--blocking", "--wait", ".01", "Continue?"]
        arguments = cli._parser().parse_args(argv)
        with patch.dict(os.environ, {"AGENT_COORD_SESSION_ID": "one"}):
            forwarded, result = requests.forward_cli(arguments, argv)
        self.assertTrue(forwarded)
        self.assertTrue(result["timed_out"])
        self.assertEqual(len(self.store.asks.list(session_id="one")), 1)

    def test_unknown_operations_and_long_lived_cli_commands_are_rejected(self):
        response = self.execute("operation", {"name": "store._connection"})
        self.assertIn("Unknown runtime operation", response["failure"]["message"])
        response = self.execute("cli", {"argv": ["wake-zellij", "watch", "--session-id", "one"]})
        self.assertIn("retain its local process", response["failure"]["message"])


if __name__ == "__main__":
    unittest.main()
