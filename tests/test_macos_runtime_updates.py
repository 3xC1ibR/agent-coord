from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "native runtime test requires macOS")
class NativeRuntimeUpdateTests(unittest.TestCase):
    def test_update_completion_requires_committed_serving_handshake(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "RuntimeHost.swift"
            source.write_text((ROOT / "desktop/macos/RuntimeHost.swift").read_text().replace(
                "private func accept(", "func accept("))
            main = root / "main.swift"
            main.write_text(r'''import Foundation
let root = URL(fileURLWithPath: CommandLine.arguments[1])
let previous = root.appendingPathComponent("previous"), target = root.appendingPathComponent("target")
let config: [String: Any] = ["python": "/usr/bin/python3", "runtime_protocol": 1,
    "runtime_schema_min": 1, "runtime_schema_max": 2]
for payload in [previous, target] {
    try FileManager.default.createDirectory(at: payload, withIntermediateDirectories: true)
    try JSONSerialization.data(withJSONObject: config).write(to: payload.appendingPathComponent("backend.json"))
}
let backend = Backend(directory: root)
var ready = 0, notices: [(String, Bool)] = []
backend.ready = { _ in ready += 1 }
backend.updateStatus = { notices.append(($0, $1)) }
func status(_ instance: String, _ payload: URL) -> [String: Any] {
    return ["status": "serving", "url": "http://127.0.0.1:12345/", "pid": Int32(123),
            "instance": instance, "payload": payload.path, "runtime_updates": 1]
}
func update(_ id: String, _ phase: String) -> [String: Any] {
    return ["id": id, "phase": phase, "target": target.path, "previous": previous.path]
}
var predecessor = status("old", previous)
predecessor["interrupted_provider_continuation"] = 1
try backend.accept(predecessor)
precondition(backend.continuesInterruptedProviders)
var accepted = status("old", previous)
accepted["update"] = update("first", "accepted")
try backend.accept(accepted)
precondition(backend.updateInProgress && ready == 1)
try backend.accept(["status": "preparing", "payload": target.path, "update": update("first", "committed")])
precondition(backend.updateInProgress && ready == 1)
precondition(!notices.contains { $0.0.hasPrefix("Backend updated.") })
var committed = status("new", target)
committed["update"] = update("first", "committed")
var invalid = committed
invalid.removeValue(forKey: "url")
do { try backend.accept(invalid); fatalError("Invalid handshake accepted") } catch {}
precondition(backend.updateInProgress)
precondition(!notices.contains { $0.0.hasPrefix("Backend updated.") })
try backend.accept(committed)
precondition(!backend.updateInProgress && ready == 2)
precondition(notices.filter { $0.0.hasPrefix("Backend updated.") }.count == 1)
try backend.accept(committed)
precondition(ready == 2)
accepted["update"] = update("second", "accepted")
try backend.accept(accepted)
var wrong = status("wrong", previous)
wrong["update"] = update("second", "committed")
do { try backend.accept(wrong); fatalError("Wrong payload accepted") } catch {}
precondition(backend.updateInProgress)
var rollback = status("restored", previous)
rollback["update"] = update("second", "rolled-back").merging(["error": "Candidate failed"]) { _, new in new }
try backend.accept(rollback)
precondition(!backend.updateInProgress && notices.last?.1 == true)

let stable = Backend(directory: root)
var addresses: [URL] = []
stable.ready = { addresses.append($0) }
let edge = "http://127.0.0.1:22345/"
var edgeStatus = status("before", previous)
edgeStatus["edge_url"] = edge
edgeStatus["edge_observer"] = String(repeating: "a", count: 43)
edgeStatus["unbrokered_provider_wait"] = 1
edgeStatus["interrupted_provider_continuation"] = 1
try stable.accept(edgeStatus)
precondition(!stable.continuesInterruptedProviders)
for generation in 1...3 {
    let key = "upgrade-\(generation)"
    var active = status("before-\(generation)", previous)
    active["edge_url"] = edge
    active["update"] = update(key, "accepted")
    try stable.accept(active)
    var successor = status("after-\(generation)", target)
    successor["edge_url"] = edge
    successor["url"] = "http://127.0.0.1:\(32345 + generation)/"
    successor["pid"] = Int32(222 + generation)
    successor["update"] = update(key, "committed")
    try stable.accept(successor)
    precondition(!stable.updateInProgress)
    precondition(stable.process?.processIdentifier == Int32(222 + generation))
    precondition(addresses.map(\.absoluteString) == [edge])
}
var changedEdge = status("after", target)
changedEdge["edge_url"] = "http://127.0.0.1:42345/"
do { try stable.accept(changedEdge); fatalError("A generation changed the native origin") } catch {}
precondition(addresses.count == 1)
''')
            binary = root / "runtime-update-test"
            build = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-module-cache-path", str(root / "modules"),
                                    str(source), str(main), "-o", str(binary)], capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            result = subprocess.run([str(binary), str(root)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
