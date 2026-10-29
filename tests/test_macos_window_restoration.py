from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "macOS Swift compiler required")
class WindowRestorationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="ribbon-window-state-")
        cls.addClassCleanup(cls.temporary.cleanup)
        root = Path(cls.temporary.name)
        main = root / "main.swift"
        main.write_text(r'''
import AppKit
let root = URL(fileURLWithPath: CommandLine.arguments[1])
let mode = CommandLine.arguments[2]
let store = WindowRestorationStore(url: root.appendingPathComponent("windows/state.json"), database: root.appendingPathComponent("state.sqlite3").path)
let one = RestoredWindow(id: "00000000-0000-4000-8000-000000000001", folderSlot: 3, kind: .workspace,
    route: "/?directory=%2Fwork#a", frame: CGRect(x: 50, y: 80, width: 900, height: 700), screen: nil,
    displayID: nil, minimized: false, workspaceJSON: "{\"draft\":\"" + String(repeating: "x", count: 131072) + "\"}")
let two = RestoredWindow(id: "00000000-0000-4000-8000-000000000002", folderSlot: 7, kind: .conversation,
    route: "/#b", frame: CGRect(x: -1800, y: 20, width: 800, height: 600), screen: nil,
    displayID: nil, minimized: true, workspaceJSON: "{\"selected\":\"b\"}")
if mode == "roundtrip" {
    try store.save(windows: [one, two], focusedWindow: two.id, nextFolderSlot: 8)
    let loaded = store.load()!
    precondition(loaded.windows.count == 2 && loaded.windows[0].id == one.id)
    precondition(loaded.windows[1].kind == .conversation && loaded.windows[1].minimized)
    precondition(loaded.focusedWindow == two.id && loaded.nextFolderSlot == 8)
    precondition(loaded.windows[0].workspaceJSON == one.workspaceJSON)
    precondition((loaded.windows[0].workspaceState?["draft"] as? String)?.count == 131072)
    let permissions = try FileManager.default.attributesOfItem(atPath: store.url.path)[.posixPermissions] as! NSNumber
    precondition(permissions.intValue & 0o777 == 0o600)
    let wrongDB = WindowRestorationStore(url: store.url, database: root.appendingPathComponent("other.sqlite3").path)
    precondition(wrongDB.load() == nil)
} else if mode == "closed" {
    try store.save(windows: [one, two], focusedWindow: two.id, nextFolderSlot: 8)
    try store.save(windows: [two], focusedWindow: two.id, nextFolderSlot: 8)
    let loaded = store.load()!
    precondition(loaded.windows.count == 1 && loaded.windows[0].folderSlot == 7 && loaded.nextFolderSlot == 8)
    try store.save(windows: [], focusedWindow: nil, nextFolderSlot: 8)
    precondition(store.load()!.windows.isEmpty && store.load()!.nextFolderSlot == 8)
} else if mode == "geometry" {
    let screen = CGRect(x: 0, y: 30, width: 1280, height: 770)
    for frame in [CGRect(x: -5000, y: 5000, width: 900, height: 700), CGRect(x: 20, y: 20, width: 6000, height: 2000)] {
        let restored = WindowRestorationStore.usableFrame(frame, screens: [screen], minimum: CGSize(width: 480, height: 420))
        precondition(screen.contains(restored) && restored.width >= 480 && restored.height >= 420)
    }
    precondition(WindowRestorationStore.usableFrame(one.frame, screens: [screen], minimum: CGSize(width: 480, height: 420)) == one.frame)
} else if mode == "invalid" {
    for route in ["https://example.com/", "//example.com/", "/not-an-app-page", "/\\evil"] {
        precondition(!WindowRestorationStore.validRoute(route))
    }
    for route in ["/", "/settings", "/monitor", "/?directory=%2Ftmp#a"] { precondition(WindowRestorationStore.validRoute(route)) }
    var bad = one; bad.route = "//example.com/"
    try store.save(windows: [bad, two, two], focusedWindow: one.id, nextFolderSlot: 1)
    let loaded = store.load()!
    precondition(loaded.windows.count == 1 && loaded.windows[0].id == two.id && loaded.nextFolderSlot == 8)
    try store.save(windows: [one], focusedWindow: one.id, nextFolderSlot: Int.max)
    precondition(store.load()!.nextFolderSlot == 4)
    try Data("not json".utf8).write(to: store.url)
    precondition(store.load() == nil)
} else if mode == "template" {
    let source = "const saved = __AGENT_COORD_WINDOW_STATE__; const id = '__AGENT_COORD_WINDOW_ID__';"
    let json = "{\"text\":\"__AGENT_COORD_WINDOW_ID__\"}"
    let script = WindowRestorationStore.script(source, values: ["__AGENT_COORD_WINDOW_STATE__": json, "__AGENT_COORD_WINDOW_ID__": "native-id"])
    precondition(script == "const saved = " + json + "; const id = 'native-id';")
}
print("passed " + mode)
''')
        cls.binary = root / "check"
        source = Path(__file__).resolve().parents[1] / "desktop/macos/WindowRestoration.swift"
        result = subprocess.run(["swiftc", "-module-cache-path", str(root / "cache"), str(source), str(main), "-o", str(cls.binary)],
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)

    def check(self, mode):
        with tempfile.TemporaryDirectory(prefix="ribbon-window-check-") as directory:
            result = subprocess.run([str(self.binary), directory, mode], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_all_windows_identity_and_large_private_drafts_round_trip(self):
        self.check("roundtrip")

    def test_explicit_close_is_removed_and_slots_are_not_reused(self):
        self.check("closed")

    def test_changed_display_geometry_is_usable(self):
        self.check("geometry")

    def test_invalid_and_duplicate_records_do_not_create_windows(self):
        self.check("invalid")

    def test_template_markers_inside_drafts_are_not_replaced(self):
        self.check("template")
