#!/usr/bin/env python3
"""Check a standalone app after relocation, with a fresh HOME and minimal PATH."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import selectors
import shutil
import subprocess
import tempfile
import urllib.request


def verify(app: Path, *, native_smoke: bool = False, report: Path | None = None) -> dict:
    result = {"source": str(app), "relocated": False, "backend": False, "cli": False}
    with tempfile.TemporaryDirectory(prefix="Ribbon Field relocation ") as directory:
        root = Path(directory)
        relocated = root / "Applications/Ribbon Field.app"
        shutil.copytree(app, relocated, symlinks=True)
        resources = relocated / "Contents/Resources"
        config = json.loads((resources / "backend.json").read_text())
        if config != {"python": "runtime/bin/python3", "database": None, "path": None}:
            raise ValueError("Not a portable app configuration")
        home = root / "new-user"
        home.mkdir()
        environment = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                       "TMPDIR": str(root), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                       "PYTHONPATH": str(resources / "backend")}
        python = resources / config["python"]
        probe = subprocess.run([str(python), "-c", """
import json, sys, ssl, sqlite3, platform
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'push-libs'))
import aiohttp, cffi, pywebpush, http_ece
from cryptography.hazmat.primitives.asymmetric import ec
ec.generate_private_key(ec.SECP256R1())
ssl.create_default_context()
sqlite3.connect(':memory:').close()
print(json.dumps({'architecture': platform.machine(), 'python': sys.version.split()[0]}))
""", str(resources)], cwd=home, env=environment, capture_output=True, text=True, check=True)
        result.update(json.loads(probe.stdout))
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(relocated)], check=True)
        result["relocated"] = True
        with subprocess.Popen([str(python), "-u", str(resources / "backend.py")], cwd=home,
                              env=environment, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    if not selector.select(timeout=15):
                        raise RuntimeError("Relocated backend did not announce its URL")
                line = process.stdout.readline()
                if not line:
                    raise RuntimeError("Relocated backend exited: " + process.stderr.read().decode())
                url = json.loads(line)["url"]
                with urllib.request.urlopen(url, timeout=5) as response:
                    if b"Ribbon Field" not in response.read():
                        raise RuntimeError("Backend did not serve the app")
                with urllib.request.urlopen(url + "api/browser/threads", timeout=5) as response:
                    if json.load(response)["data"] != []:
                        raise RuntimeError("Fresh user unexpectedly has existing threads")
                if not (home / ".local/state/agent-coord/state.sqlite3").is_file():
                    raise RuntimeError("Backend did not create the recipient's database")
                result["backend"] = True
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise RuntimeError("Backend did not stop when parent closed")
            if process.returncode:
                raise RuntimeError(process.stderr.read().decode())
        cli = subprocess.run([str(resources / "backend/agent-coord"), "thread", "list"],
                             cwd=home, env=environment, capture_output=True, text=True, check=True)
        if json.loads(cli.stdout) != []:
            raise RuntimeError("Bundled CLI did not use the fresh user's database")
        result["cli"] = True
        if native_smoke:
            native_report = root / "native-smoke.json"
            # The native smoke suite starts no model turns and owns a temporary
            # database/preferences domain. Use only system tools on PATH.
            try:
                subprocess.run([str(python), str(Path(__file__).with_name("smoke_test.py")),
                                "--app", str(relocated), "--report", str(native_report)],
                               cwd=home, env=environment, check=True, capture_output=True, text=True)
            finally:
                if native_report.exists():
                    result["native_smoke"] = json.loads(native_report.read_text())
                if report:
                    report.write_text(json.dumps(result, indent=2) + "\n")
                    for screenshot in native_report.parent.glob(native_report.name + "*.png"):
                        shutil.copyfile(screenshot, Path(str(report) + screenshot.name[len(native_report.name):]))
    if report:
        report.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--app", type=Path)
    source.add_argument("--dmg", type=Path, help="Mount a disk image read-only and verify a copy of its app.")
    parser.add_argument("--native-smoke", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
    if args.dmg:
        image = args.dmg.resolve()
        subprocess.run(["hdiutil", "verify", str(image)], check=True, capture_output=True)
        with tempfile.TemporaryDirectory(prefix="ribbon-dmg-verify-") as scratch:
            mount = Path(scratch) / "volume"
            subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mount),
                            str(image)], check=True, capture_output=True)
            try:
                if not (mount / "Applications").is_symlink() or (mount / "Applications").readlink() != Path("/Applications"):
                    raise ValueError("Disk image has no drag-to-Applications shortcut")
                if not (mount / "Read Me.txt").is_file():
                    raise ValueError("Disk image has no installation instructions")
                result = verify(mount / "Ribbon Field.app", native_smoke=args.native_smoke, report=args.report)
                result["disk_image"] = str(image)
            finally:
                subprocess.run(["hdiutil", "detach", str(mount)], check=True, capture_output=True)
    else:
        result = verify(args.app.resolve(), native_smoke=args.native_smoke, report=args.report)
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Distribution validation failed: {exc}\n{exc.stdout or ''}\n{exc.stderr or ''}")
