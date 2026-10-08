from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "macOS Swift compiler required")
class WorkspaceFileLinkTests(unittest.TestCase):
    def test_native_resolution_checks_real_files_and_workspace_boundaries(self):
        source = Path(__file__).resolve().parents[1] / "desktop/macos/WorkspaceFiles.swift"
        with tempfile.TemporaryDirectory(prefix="file-links-") as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            file = workspace / "notes with spaces.md"
            file.write_text("hello\n")
            outside = root / "workspace-other"
            outside.mkdir()
            (outside / "secret.txt").write_text("outside\n")
            (workspace / "inside-link").symlink_to(file)
            (workspace / "outside-link").symlink_to(outside / "secret.txt")
            harness = root / "main.swift"
            harness.write_text('''
import Foundation
let root = CommandLine.arguments[1]
let expected = URL(fileURLWithPath: root + "/notes with spaces.md").resolvingSymlinksInPath()
for path in ["notes with spaces.md", "./notes with spaces.md", root + "/notes with spaces.md", "inside-link"] {
    precondition(try! WorkspaceFileLink.resolve(path: path, workspace: root) == expected)
}
for path in ["missing.md", "..", ".", "../workspace-other/secret.txt", root + "-other/secret.txt", "outside-link", "bad\\0name"] {
    do {
        _ = try WorkspaceFileLink.resolve(path: path, workspace: root)
        fatalError("Accepted invalid path: " + path)
    } catch {}
}
do {
    _ = try WorkspaceFileLink.resolve(path: "notes with spaces.md", workspace: "")
    fatalError("Accepted missing workspace")
} catch {}
print("File link resolution passed")
''')
            binary = root / "check"
            build = subprocess.run(["swiftc", "-module-cache-path", str(root / "cache"),
                                    str(source), str(harness), "-o", str(binary)],
                                   capture_output=True, text=True, timeout=120)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run([str(binary), str(workspace)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
