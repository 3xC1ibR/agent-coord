#!/usr/bin/env python3
"""Build a local Agent Coord.app using Apple's command-line developer tools."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SCRIPTS = ROOT / "plugins/agent-coord/scripts"
BUNDLE_ID = "com.agentcoord.desktop"


def check_destination(destination: Path) -> None:
    if destination.is_symlink():
        raise ValueError(f"Refusing to replace a symbolic link: {destination}")
    if not destination.exists():
        return
    try:
        result = subprocess.run(["/usr/bin/plutil", "-extract", "CFBundleIdentifier", "raw", "-o", "-",
                                 str(destination / "Contents/Info.plist")], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip() == BUNDLE_ID:
            return
    except (OSError, ValueError):
        pass
    raise ValueError(f"Refusing to replace an unrelated file or application: {destination}")


def replace_app(source: Path, destination: Path) -> None:
    check_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".agent-coord-install-", dir=destination.parent) as scratch:
        staged = Path(scratch) / "Agent Coord.app"
        shutil.copytree(source, staged)
        old = Path(scratch) / "previous.app"
        if destination.exists():
            destination.rename(old)
        try:
            staged.rename(destination)
        except OSError:
            if old.exists():
                old.rename(destination)
            raise


def copy_backend(resources: Path) -> dict[str, str]:
    snapshot = {}
    for source in sorted((SCRIPTS / "agent_coord").rglob("*")):
        if not source.is_file() or "__pycache__" in source.parts or source.suffix == ".pyc":
            continue
        relative = source.relative_to(SCRIPTS)
        target = resources / "backend" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        data = source.read_bytes()
        target.write_bytes(data)
        snapshot[str(relative)] = hashlib.sha256(data).hexdigest()
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build/macos/Agent Coord.app")
    parser.add_argument("--install", action="store_true", help="Also install into ~/Applications.")
    parser.add_argument("--install-dir", type=Path, default=Path.home() / "Applications")
    parser.add_argument("--python", default=sys.executable, help="Python 3.10+ used by the app.")
    parser.add_argument("--database", type=Path, help="Coordination database; defaults to the CLI's database.")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Building the native app requires macOS and Xcode Command Line Tools.")
    python = shutil.which(args.python)
    if not python:
        parser.error(f"Python executable not found: {args.python}")
    subprocess.run([python, "-c", "import sys; assert sys.version_info >= (3, 10), 'Python 3.10+ is required'"], check=True)
    subprocess.run(["xcrun", "--find", "swiftc"], check=True, stdout=subprocess.DEVNULL)
    if not shutil.which("codex"):
        parser.error("Install Codex and make it available on PATH before building the app.")
    output = args.output.expanduser().absolute()
    destination = args.install_dir.expanduser().absolute() / "Agent Coord.app"
    for path in {output, *([destination] if args.install else [])}:
        check_destination(path)
        executable = path / "Contents/MacOS/Agent Coord"
        if executable.exists() and subprocess.run(
            ["/usr/sbin/lsof", "-t", str(executable)], capture_output=True,
        ).stdout.strip():
            parser.error(f"Quit the app before replacing it: {path}")

    sys.path.insert(0, str(SCRIPTS))
    from agent_coord.store import default_database_path

    database = (args.database or default_database_path()).expanduser().absolute()
    with tempfile.TemporaryDirectory(prefix="agent-coord-build-") as scratch:
        scratch = Path(scratch)
        app = scratch / "Agent Coord.app"
        contents = app / "Contents"
        resources = contents / "Resources"
        binaries = contents / "MacOS"
        resources.mkdir(parents=True)
        binaries.mkdir()
        snapshot = copy_backend(resources)
        for name in ("backend.py", "bridge.js"):
            shutil.copyfile(HERE / name, resources / name)
        config = {
            "python": str(Path(python).absolute()), "database": str(database),
            "path": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        }
        (resources / "backend.json").write_text(json.dumps(config, indent=2) + "\n")
        (resources / "backend-snapshot.json").write_text(json.dumps(snapshot, indent=2) + "\n")
        shutil.copyfile(ROOT / "LICENSE", resources / "LICENSE")
        info = {
                "CFBundleIdentifier": BUNDLE_ID, "CFBundleName": "Agent Coord",
                "CFBundleDisplayName": "Agent Coord", "CFBundleExecutable": "Agent Coord",
                "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "0.1.0",
                "CFBundleVersion": "1", "CFBundleIconFile": "AppIcon",
                "LSMinimumSystemVersion": "12.0", "NSHighResolutionCapable": True,
                "NSPrincipalClass": "NSApplication",
                "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
            }
        (contents / "Info.plist").write_text(json.dumps(info))
        subprocess.run(["/usr/bin/plutil", "-convert", "xml1", str(contents / "Info.plist")], check=True)
        environment = dict(os.environ, MACOSX_DEPLOYMENT_TARGET="12.0")
        subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-O", str(HERE / "App.swift"),
                        "-o", str(binaries / "Agent Coord")], check=True, env=environment)
        subprocess.run(["xcrun", "swift", str(HERE / "Icon.swift"), str(scratch / "AppIcon.iconset")], check=True)
        subprocess.run(["iconutil", "-c", "icns", str(scratch / "AppIcon.iconset"),
                        "-o", str(resources / "AppIcon.icns")], check=True)
        subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True)
        subprocess.run(["codesign", "--verify", "--strict", str(app)], check=True)
        replace_app(app, output)
    if args.install and destination != output:
        replace_app(output, destination)
    print(json.dumps({"built": str(output), "installed": str(destination) if args.install else None}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
