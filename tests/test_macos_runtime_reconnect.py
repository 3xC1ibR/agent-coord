from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "native runtime test requires macOS")
class NativeRuntimeReconnectTests(unittest.TestCase):
    def test_same_owner_recovers_after_urlsession_health_poll_failure_and_manual_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class HealthHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path != "/_runtime/health":
                        self.send_error(404)
                        return
                    if (root / "unavailable").exists():
                        raw = json.dumps({"status": "unavailable"}).encode()
                        self.send_response(503)
                    else:
                        raw = json.dumps({
                            "status": "serving",
                            "url": f"http://127.0.0.1:{self.server.server_port}/",
                            "pid": 123,
                            "instance": "same-owner",
                            "payload": str(root),
                        }).encode()
                        self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

                def log_message(self, *_args):
                    pass

            health_server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
            health_server.daemon_threads = True
            server_thread = threading.Thread(target=health_server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            server_thread.start()
            self.addCleanup(health_server.server_close)
            self.addCleanup(server_thread.join, 2)
            self.addCleanup(health_server.shutdown)
            edge_url = f"http://127.0.0.1:{health_server.server_port}/"

            source = root / "RuntimeHost.swift"
            source.write_text((ROOT / "desktop/macos/RuntimeHost.swift").read_text().replace(
                "private func poll(", "func poll(").replace("private var polling", "var polling"))
            (root / "backend.json").write_text(json.dumps({
                "python": sys.executable, "runtime_protocol": 1,
                "runtime_schema_min": 1, "runtime_schema_max": 2,
            }))
            (root / "backend.py").write_text(f'''import json, pathlib, sys
root = pathlib.Path(__file__).parent
if (root / "unavailable").exists():
    print("The runtime owner is unavailable. Refusing a second owner.", file=sys.stderr)
    raise SystemExit(1)
print(json.dumps({{"status": "serving", "url": {edge_url!r},
    "pid": 123, "instance": "same-owner", "payload": str(root)}}))
''')
            main = root / "main.swift"
            main.write_text(r'''import Foundation
let root = URL(fileURLWithPath: CommandLine.arguments[1])
let config = try JSONDecoder().decode(BackendConfiguration.self,
    from: Data(contentsOf: root.appendingPathComponent("backend.json")))
let backend = Backend(directory: root)
var ready = 0, failures = 0
backend.ready = { _ in ready += 1 }
backend.exited = { _ in failures += 1 }
func wait(_ condition: () -> Bool) {
    let deadline = Date().addingTimeInterval(10)
    while !condition() && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.01))
    }
    precondition(condition(), "Runtime callback did not arrive")
}
try backend.start(resources: root, configuration: config, database: root.appendingPathComponent("state.sqlite3").path)
wait { ready == 1 }
let unavailable = root.appendingPathComponent("unavailable")
func disconnect() throws {
    try Data().write(to: unavailable)
    for _ in 0..<15 {
        backend.poll()
        wait { !backend.polling }
    }
    precondition(backend.process == nil)
    try FileManager.default.removeItem(at: unavailable)
}
try disconnect()
precondition(failures == 1 && ready == 1)
// The URLSession /_runtime/health poll must restore the interface for the same owner.
backend.poll()
wait { !backend.polling }
precondition(ready == 2 && backend.process?.processIdentifier == 123,
    "Successful same-owner polling left the interface on its error screen")
backend.poll()
wait { !backend.polling }
precondition(ready == 2, "Healthy polls must not reload the interface")
try disconnect()
precondition(failures == 2)
// The user's Try Again action must also restore the same owner exactly once.
try backend.start(resources: root, configuration: config, database: root.appendingPathComponent("state.sqlite3").path)
wait { ready == 3 }
backend.detach()
precondition(backend.process?.processIdentifier == 123)
''')
            binary = root / "runtime-reconnect-test"
            build = subprocess.run([
                "xcrun", "swiftc", "-swift-version", "5", "-module-cache-path", str(root / "modules"),
                str(source), str(main), "-o", str(binary),
            ], capture_output=True, text=True, timeout=120)
            self.assertEqual(build.returncode, 0, build.stderr)
            result = subprocess.run([str(binary), str(root)], capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
