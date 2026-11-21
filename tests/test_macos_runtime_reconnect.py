from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "native runtime test requires macOS")
class NativeRuntimeReconnectTests(unittest.TestCase):
    def test_same_owner_recovers_after_poll_failure_and_manual_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "RuntimeHost.swift"
            source.write_text((ROOT / "desktop/macos/RuntimeHost.swift").read_text().replace(
                "private func poll(", "func poll(").replace("private var polling", "var polling"))
            (root / "backend.json").write_text(json.dumps({
                "python": sys.executable, "runtime_protocol": 1,
                "runtime_schema_min": 1, "runtime_schema_max": 2,
            }))
            (root / "backend.py").write_text('''import json, pathlib, sys
root = pathlib.Path(__file__).parent
if (root / "unavailable").exists():
    print("The runtime owner is unavailable. Refusing a second owner.", file=sys.stderr)
    raise SystemExit(1)
print(json.dumps({"status": "serving", "url": "http://127.0.0.1:12345/",
    "pid": 123, "instance": "same-owner", "payload": str(root)}))
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
backend.detach()
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
// The existing poll timer must restore the interface for the same owner.
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
