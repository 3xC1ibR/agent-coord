"""Pin the desktop gateway origin, owner and activation across coordinator replacement."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent-coord/scripts"))

from agent_coord import desktop_runtime as runtime, runtime_gateway_lifecycle as gateway


class GatewayLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = str((Path(temporary.name) / "state.sqlite3").resolve())
        self.directory = gateway.gateway_directory(self.database)
        self.directory.mkdir(mode=0o700)
        self.target = self.directory.parent / "payloads" / ("a" * 64)
        self.source = {"database": self.database, "instance": "retiring", "payload": "old"}
        self.owner = runtime.lock_file(self.directory / "owner.lock")
        self.addCleanup(os.close, self.owner)
        self.settled = False
        self.prepared = []
        self.released = []
        self.reject_prepare = False
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

            def respond(self, values=None):
                if self.headers.get("Authorization") != "Bearer test-credential":
                    self.send_error(403)
                    return
                result = {**fixture.identity, "settled": fixture.settled}
                if self.path == "/_runtime/prepare":
                    fixture.prepared.append(values)
                    if fixture.reject_prepare:
                        self.send_error(409, "A forwarded write is still completing")
                        return
                    result.update(values, prepared=True)
                elif self.path == "/_runtime/release-preparation":
                    fixture.released.append(values)
                    result.update(values, released=True)
                else:
                    self.assert_identity_route()
                data = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def assert_identity_route(self):
                if self.path != "/_runtime/identity":
                    raise ValueError("Unexpected gateway route")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.identity = {"database": self.database, "payload": str(self.target), "instance": "gateway",
                         "url": f"http://127.0.0.1:{self.server.server_port}/"}
        self.discovery = {**self.identity, "control_token": "test-credential"}
        runtime.atomic_json(self.directory / "gateway.json", self.discovery)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_only_public_origin_leaves_the_helper_and_stays_bound_until_settled(self):
        first = gateway.status(self.database, {"status": "serving"}, candidate=self.target)
        self.assertNotIn("credential", json.dumps(first))
        self.assertFalse(first["interaction_settled"])
        self.assertEqual(first["interaction_url"], self.identity["url"])
        held = gateway.status(self.database, {"status": "updating"})
        self.assertEqual(held["interaction_url"], first["interaction_url"])
        self.settled = True
        self.assertTrue(gateway.status(self.database, {"status": "serving"})["interaction_settled"])
        for changed in ({"url": "http://localhost:123/"}, {"control_token": "new-credential"}):
            with self.subTest(changed=changed):
                runtime.atomic_json(self.directory / "gateway.json", {**self.discovery, **changed})
                with self.assertRaisesRegex(RuntimeError, "origin|credential"):
                    gateway.status(self.database, {})
        (self.directory / "gateway.json").unlink()
        with self.assertRaisesRegex(RuntimeError, "missing"):
            gateway.status(self.database, {})

    def test_existing_installer_authorization_is_preserved_before_attach(self):
        self.target.mkdir(parents=True)
        runtime.atomic_json(self.target / "backend.json", {"runtime_version": "0.1.27+78"})
        prior = {"id": "signed-authorization", "source_instance": "retiring", "runtime_version": "0.1.27+78"}
        authorization = self.directory.parent / "update-authorization.json"
        runtime.atomic_json(authorization, prior)
        with patch.object(runtime, "control") as control:
            with self.assertRaisesRegex(RuntimeError, "prior installer authorization"):
                gateway.attach(self.database, self.source, self.target, True)
            control.assert_not_called()
        self.assertEqual(json.loads(authorization.read_text()), prior)

    def test_two_activation_callers_share_one_fence_and_one_backend_operation(self):
        issued = []
        def control(record, action, **values):
            if action == "status":
                return {"pending": str(self.target), "pending_confirmation": True}
            self.assertEqual(action, "activate-update")
            self.assertEqual(self.prepared, [{"request_id": "one", "target": str(self.target),
                                             "source_instance": "retiring"}])
            issued.append(values["request_id"])
            runtime.atomic_json(self.directory.parent / "update.json",
                                {"id": "one", "target": str(self.target), "phase": "accepted"})
            raise ConnectionResetError("Lost activation acknowledgement")
        def activate():
            return gateway.activate(self.source, request_id="one", payload=str(self.target))["update"]["id"]
        with patch.object(runtime, "control", side_effect=control), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(activate) for _ in range(2)]
            self.assertEqual([future.result(timeout=3) for future in futures], ["one", "one"])
        self.assertEqual(issued, ["one"])
        self.assertEqual(gateway.status(self.database, {})["interaction_request_id"], "one")

    def test_unconfirmed_prepare_blocks_backend_activation_and_preserves_original_identity(self):
        request = gateway._request
        def incomplete(record, route, *args, **kwargs):
            if route == "prepare":
                raise TimeoutError("Unknown prepare outcome")
            return request(record, route, *args, **kwargs)
        with patch.object(gateway, "_request", side_effect=incomplete), patch.object(runtime, "control",
                return_value={"pending": str(self.target), "pending_confirmation": True}) as control:
            with self.assertRaisesRegex(TimeoutError, "Unknown prepare outcome"):
                gateway.activate(self.source, request_id="original", payload=str(self.target))
            with self.assertRaisesRegex(RuntimeError, "original identity"):
                gateway.activate(self.source, request_id="different", payload=str(self.target))
            self.assertTrue(all(call.args[1] == "status" for call in control.call_args_list))
        self.assertEqual(gateway.status(self.database, {})["interaction_request_id"], "original")

    def test_detached_serve_recovers_authorized_request_after_helper_exits_with_fence_ready(self):
        from agent_coord import runtime_interaction_gateway

        class HelperExited(BaseException):
            pass

        recorded = threading.Event()
        attempts = []
        target = str(self.target)
        def control(record, action, **values):
            if action == "status":
                return {"pending": target, "pending_confirmation": True, "instance": "retiring"}
            self.assertEqual(action, "activate-update")
            attempts.append(values["request_id"])
            if len(attempts) == 1:
                # The helper exits after the authenticated ready fence, before
                # the coordinator receives or accepts the network operation.
                raise HelperExited()
            update = {"id": values["request_id"], "target": values["payload"], "source_instance": "retiring", "phase": "accepted"}
            runtime.atomic_json(self.directory.parent / "update.json", update)
            recorded.set()
            return {"update": update}

        def service(database, directory, ready_fd):
            os.close(ready_fd)
            self.assertTrue(recorded.wait(5), "Detached gateway failed to reconcile the original authorization")

        with patch.object(runtime, "control", side_effect=control), patch.object(runtime, "discover", return_value=self.source):
            with self.assertRaises(HelperExited):
                gateway.activate(self.source, request_id="original", payload=target)
            intent = self.directory / "activation-request.json"
            self.assertEqual(runtime.read_json(intent).get("phase", "authorized"), "authorized")
            self.assertIsNone(gateway.runtime_update.read(self.directory.parent))
            # The normal detached server lifecycle starts recovery; no second
            # UI/CLI activate command or status call is involved.
            with patch.object(runtime_interaction_gateway, "serve", side_effect=service):
                read, write = os.pipe()
                try:
                    gateway.serve(self.database, self.directory, os.dup(self.owner), write)
                finally:
                    os.close(read)
            update = gateway.activate(self.source, request_id="original", payload=target)["update"]
        self.assertEqual(update["id"], "original")
        self.assertEqual(attempts, ["original", "original"])
        self.assertEqual([r["request_id"] for r in self.prepared], ["original", "original"])
        self.assertEqual(runtime.read_json(intent)["phase"], "recorded")

    def test_rejected_preparation_releases_only_its_fence_and_never_activates(self):
        self.reject_prepare = True
        with patch.object(runtime, "control", return_value={"pending": str(self.target), "pending_confirmation": True}) as control:
            with self.assertRaisesRegex(gateway.GatewayControlRejected, "409"):
                gateway.activate(self.source, request_id="declined", payload=str(self.target))
            self.assertEqual(self.released, self.prepared)
            self.assertEqual(self.released[0], {"request_id": "declined", "source_instance": "retiring", "target": str(self.target)})
            self.assertEqual(runtime.read_json(self.directory / "activation-request.json")["phase"], "released")
            self.assertTrue(all(call.args[1] == "status" for call in control.call_args_list))
        self.assertIsNone(gateway.runtime_update.read(self.directory.parent))

    def test_unrelated_runtime_update_supersedes_recovery_without_new_control(self):
        values = {"request_id": "interrupted", "target": str(self.target), "source_instance": "retiring"}
        runtime.atomic_json(self.directory / "activation-request.json", {**values, "phase": "authorized", "previous_update_id": None})
        runtime.atomic_json(self.directory.parent / "update.json", {"id":"different", "target":"other", "phase":"failed"})
        with patch.object(runtime, "control") as control:
            with self.assertRaisesRegex(RuntimeError, "superseded"):
                gateway._submit_authorization(self.source, self.directory.parent, values, self.discovery, 2, recovering=True)
            control.assert_not_called()
        self.assertFalse(self.prepared)
        self.assertFalse(self.released)

    def test_live_owner_with_unavailable_or_changed_discovery_cannot_start_a_replacement(self):
        gateway.status(self.database, {})
        self.server.shutdown()
        self.server.server_close()
        with patch.object(gateway, "_start") as start:
            with self.assertRaises(OSError):
                gateway.status(self.database, {})
            start.assert_not_called()

    def test_fresh_gateway_requires_the_packaged_manifest_and_retained_content_identity(self):
        resources = self.directory.parent / "source"
        package = resources / "backend/agent_coord"
        package.mkdir(parents=True)
        manifest = {}
        for name in ("desktop_runtime", "runtime_gateway_lifecycle", "runtime_interaction_gateway"):
            file = package / (name + ".py")
            file.write_text(name)
            manifest["agent_coord/" + file.name] = hashlib.sha256(file.read_bytes()).hexdigest()
        runtime.atomic_json(resources / "backend.json", runtime.compatibility_metadata("1.0+2"))
        runtime.atomic_json(resources / "backend-snapshot.json", manifest)
        staged = runtime.stage_payload(resources, self.directory.parent)
        self.assertEqual(gateway._validate_helper(self.directory, staged), staged)
        (staged / "backend/agent_coord/runtime_interaction_gateway.py").write_text("stale helper")
        with self.assertRaisesRegex(RuntimeError, "packaged backend manifest"):
            gateway._validate_helper(self.directory, staged)

    def test_absent_legacy_gateway_needs_no_owner_or_extra_control(self):
        other = str(Path(self.database).with_name("unrelated.sqlite3"))
        with patch.object(gateway, "_start") as start, patch.object(runtime, "control", return_value={"ok": True}) as control:
            self.assertEqual(gateway.status(other, {"status": "serving"}), {"status": "serving"})
            result = gateway.activate({**self.source, "database": other}, request_id="plain", payload="staged")
            self.assertEqual(result, {"ok": True})
            control.assert_called_once()
            start.assert_not_called()


class DetachedGatewayTests(unittest.TestCase):
    def test_detached_gateway_recovers_its_origin_then_retires_with_receipts(self):
        import signal
        import time

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            database = str(root / "state.sqlite3")
            resources = root / "resources"
            package = resources / "backend/agent_coord"
            package.mkdir(parents=True)
            for module in (ROOT / "plugins/agent-coord/scripts/agent_coord").glob("*.py"):
                shutil.copyfile(module, package / module.name)
            shutil.copyfile(ROOT / "desktop/macos/backend.py", resources / "backend.py")
            (package / "runtime_interaction_gateway.py").write_text(r'''import json, os, secrets
from pathlib import Path
from http.server import BaseHTTPRequestHandler
from .local_http import LocalHTTPServer
from .desktop_runtime import atomic_json

def serve(database, directory, ready_fd):
    path = directory / "gateway.json"
    old = json.loads(path.read_text()) if path.exists() else {}
    result = dict(database=database, payload=str(Path(__file__).resolve().parents[2]), instance=secrets.token_hex(8))
    token = old.get("control_token") or secrets.token_hex(16)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers["Authorization"] != "Bearer " + token:
                self.send_error(403)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(dict(result, settled=(directory / "settled").exists())).encode())
        def do_POST(self):
            if self.headers["Authorization"] != "Bearer " + token or not (directory / "settled").exists():
                self.send_error(403)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(dict(result, retired=True)).encode())
            (directory / "retired").touch()
        def log_message(self, *_):
            pass

    port = int(old["url"].split(":")[-1].strip("/")) if old else 0
    with LocalHTTPServer(("127.0.0.1", port), Handler) as server:
        result["url"] = "http://127.0.0.1:%d/" % server.server_port
        server.timeout = .02
        atomic_json(path, dict(result, control_token=token))
        (directory / "fixture-pid").write_text(str(os.getpid()))
        os.write(ready_fd, b"ready\n")
        os.close(ready_fd)
        while not (directory / "retired").exists():
            server.handle_request()
''')
            manifest = {"agent_coord/" + file.name: hashlib.sha256(file.read_bytes()).hexdigest()
                        for file in package.glob("*.py")}
            runtime.atomic_json(resources / "backend-snapshot.json", manifest)
            runtime.atomic_json(resources / "backend.json", {"python": sys.executable, **runtime.compatibility_metadata("1+2")})
            directory = gateway.gateway_directory(database)
            staged = runtime.stage_payload(resources, directory.parent)
            try:
                with ThreadPoolExecutor(max_workers=3) as pool:
                    starts = [pool.submit(gateway.status, database, {}, candidate=staged) for _ in range(3)]
                    results = [future.result(timeout=8) for future in starts]
                self.assertEqual(len({result["interaction_url"] for result in results}), 1)
                before = runtime.read_json(directory / "gateway.json")
                with self.assertRaisesRegex(RuntimeError, "unfinished intake"):
                    gateway.retire(database)
                os.kill(int((directory / "fixture-pid").read_text()), signal.SIGTERM)
                limit = time.monotonic() + 4
                while not runtime.owner_available(directory) and time.monotonic() < limit:
                    time.sleep(.02)
                self.assertTrue(runtime.owner_available(directory))
                self.assertEqual(gateway.status(database, {})["interaction_url"], before["url"])
                restored = runtime.read_json(directory / "gateway.json")
                self.assertEqual(restored["control_token"], before["control_token"])
                self.assertNotEqual(restored["instance"], before["instance"])
                (directory / "settled").touch()
                archive = gateway.retire(database)
                self.assertEqual(runtime.read_json(archive / "gateway.json")["instance"], restored["instance"])
                self.assertFalse(directory.exists())
                self.assertEqual(gateway.status(database, {}), {})
            except Exception as error:
                log = directory / "gateway.log"
                if log.exists():
                    error.add_note(log.read_text())
                raise
            finally:
                pid = directory / "fixture-pid"
                if pid.exists() and not runtime.owner_available(directory):
                    os.kill(int(pid.read_text()), signal.SIGTERM)


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "Requires the Swift desktop host")
class NativeGatewayTests(unittest.TestCase):
    def test_held_update_loads_one_gateway_and_switches_only_after_drain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = (ROOT / "desktop/macos/RuntimeHost.swift").read_text().replace(
                "private func accept(", "func accept(").replace("private var database: String?", "var database: String?")
            (root / "RuntimeHost.swift").write_text(source)
            (root / "main.swift").write_text(r'''import Foundation
let root = URL(fileURLWithPath: CommandLine.arguments[1])
let db = root.appendingPathComponent("state.sqlite3")
let payloads = root.appendingPathComponent(".state.sqlite3.runtime/payloads")
let old = payloads.appendingPathComponent(String(repeating: "a", count: 64))
let candidate = payloads.appendingPathComponent(String(repeating: "b", count: 64))
for payload in [old, candidate] {
    try FileManager.default.createDirectory(at: payload, withIntermediateDirectories: true)
    let metadata: [String: Any] = ["python": "/usr/bin/python3", "runtime_protocol": 1,
                                   "runtime_schema_min": 1, "runtime_schema_max": 2]
    try JSONSerialization.data(withJSONObject: metadata).write(to: payload.appendingPathComponent("backend.json"))
}
let backend = Backend(directory: root)
backend.database = db.path
var origins: [String] = [], notices: [String] = []
backend.ready = { origins.append($0.absoluteString) }
backend.updateStatus = { message, _ in notices.append(message) }
let origin = "http://127.0.0.1:30123/", direct = "http://127.0.0.1:30124/"
var state: [String: Any] = ["pid": Int32(101), "instance": "old", "status": "serving", "url": direct,
    "payload": old.path, "pending": candidate.path, "pending_confirmation": true,
    "runtime_updates": 1, "runtime_update_prompts": 1, "interaction_url": origin,
    "interaction_payload": candidate.path, "interaction_settled": false,
    "update": ["id":"prior", "target":old.path, "phase":"committed"]]
try backend.accept(state)
precondition(origins == [origin] && backend.pendingUpdate == candidate.path)
state["interaction_request_id"] = "handover"
state["update"] = ["id":"handover", "target":candidate.path, "phase":"stopping"]
state["status"] = "updating"
try backend.accept(state)
precondition(origins == [origin] && notices.isEmpty)
var missing = state
missing.removeValue(forKey: "interaction_url")
do { try backend.accept(missing); fatalError("Bypassed missing gateway") } catch {}
state["payload"] = candidate.path
state["instance"] = "new"
state["status"] = "serving"
state.removeValue(forKey: "pending")
state["update"] = ["id":"handover", "target":candidate.path, "phase":"committed"]
try backend.accept(state)
precondition(origins == [origin] && notices.isEmpty)
state["interaction_settled"] = true
try backend.accept(state)
try backend.accept(state)
precondition(origins == [origin, direct] && notices.count == 1 && !backend.updateInProgress)
''')
            binary = root / "native-gateway-test"
            subprocess.run(["xcrun", "swiftc", "-swift-version", "5", str(root / "RuntimeHost.swift"),
                            str(root / "main.swift"), "-o", str(binary)], check=True, timeout=90)
            subprocess.run([str(binary), str(root)], check=True, timeout=10)


if __name__ == "__main__":
    unittest.main()
