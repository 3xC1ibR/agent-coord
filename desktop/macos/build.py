#!/usr/bin/env python3
"""Build Ribbon Field.app, optionally with a portable runtime and release DMG."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SCRIPTS = ROOT / "plugins/agent-coord/scripts"
BUNDLE_ID = "com.agentcoord.desktop"
APP_NAME = "Ribbon Field"
BUNDLE_NAME = APP_NAME + ".app"
LEGACY_BUNDLE_NAME = "Agent Coord.app"
# Retain the executable and bundle identity used by existing local tooling.
EXECUTABLE_NAME = "Agent Coord"


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


def check_not_running(app: Path) -> None:
    executable = app / "Contents/MacOS" / EXECUTABLE_NAME
    if executable.exists() and subprocess.run(
        ["/usr/sbin/lsof", "-t", str(executable)], capture_output=True,
    ).stdout.strip():
        raise ValueError(f"Quit the app before replacing it: {app}")


def legacy_installation(destination: Path) -> Path | None:
    legacy = destination.with_name(LEGACY_BUNDLE_NAME)
    if destination.name == BUNDLE_NAME and (legacy.exists() or legacy.is_symlink()):
        return legacy
    return None


def replace_app(source: Path, destination: Path, *, legacy: Path | None = None) -> None:
    check_destination(destination)
    check_not_running(destination)
    if legacy is not None:
        check_destination(legacy)
        check_not_running(legacy)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".agent-coord-install-", dir=destination.parent) as scratch:
        staged = Path(scratch) / BUNDLE_NAME
        shutil.copytree(source, staged, symlinks=True)
        old = Path(scratch) / "previous.app"
        old_legacy = Path(scratch) / "legacy.app"
        try:
            if destination.exists():
                destination.rename(old)
            if legacy is not None:
                legacy.rename(old_legacy)
            staged.rename(destination)
        except OSError:
            if old_legacy.exists():
                old_legacy.rename(legacy)
            if old.exists():
                old.rename(destination)
            raise


def register_app(destination: Path) -> None:
    # Refresh LaunchServices even when only URL handlers changed and the local
    # development bundle version stayed the same.
    subprocess.run(["/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister",
                    "-f", str(destination)], check=True)


def copy_backend(resources: Path, *, standalone: bool = False) -> dict[str, str]:
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
    cli = resources / "backend/agent-coord"
    if standalone:
        # Resolve from the installed bundle, including paths containing spaces.
        cli.write_text('#!/bin/sh\n'
                       'backend_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1\n'
                       'unset PYTHONHOME PYTHONPATH\n'
                       'exec "$backend_dir/../runtime/bin/python3" -s -B "$backend_dir/agent-coord.py" "$@"\n')
        shutil.copyfile(SCRIPTS / "agent-coord", cli.with_suffix(".py"))
        snapshot["agent-coord.py"] = hashlib.sha256(cli.with_suffix(".py").read_bytes()).hexdigest()
    else:
        shutil.copyfile(SCRIPTS / "agent-coord", cli)
    cli.chmod(0o755)
    snapshot["agent-coord"] = hashlib.sha256(cli.read_bytes()).hexdigest()
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build/macos" / BUNDLE_NAME)
    parser.add_argument("--install", action="store_true", help="Also install into ~/Applications.")
    parser.add_argument("--install-dir", type=Path, default=Path.home() / "Applications")
    parser.add_argument("--python", default=sys.executable, help="Python 3.10+ used by the app.")
    parser.add_argument("--database", type=Path, help="Coordination database; defaults to the CLI's database.")
    parser.add_argument("--standalone", action="store_true", help="Bundle Python and resolve user paths on launch.")
    parser.add_argument("--arch", choices=("arm64", "x86_64"), default=platform.machine())
    parser.add_argument("--dmg", type=Path, help="Create a drag-to-Applications disk image (requires --standalone).")
    parser.add_argument("--sign-identity", default="-", help="Developer ID Application identity; defaults to ad hoc.")
    parser.add_argument("--notary-profile", help="Notarytool keychain profile; sign, notarize, and staple the app and DMG.")
    parser.add_argument("--version", default="0.1.0")
    parser.add_argument("--build-number", default="1")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Building the native app requires macOS and Xcode Command Line Tools.")
    if args.dmg and not args.standalone:
        parser.error("--dmg requires --standalone; local bundles contain machine-specific paths.")
    if args.standalone and args.database:
        parser.error("--standalone cannot embed a builder's --database path.")
    if args.arch != platform.machine() and not args.standalone:
        parser.error("Cross-architecture builds require --standalone.")
    if args.notary_profile and (not args.standalone or not args.dmg or
                              not args.sign_identity.startswith("Developer ID Application:")):
        parser.error("--notary-profile requires --standalone, --dmg, and a Developer ID Application identity.")
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version) or not re.fullmatch(r"[1-9]\d*", args.build_number):
        parser.error("Use a numeric X.Y.Z --version and a positive integer --build-number.")
    if args.dmg and (args.dmg.exists() or args.dmg.is_symlink()):
        parser.error(f"Refusing to overwrite an existing release: {args.dmg}")
    python = shutil.which(args.python)
    if not python:
        parser.error(f"Python executable not found: {args.python}")
    subprocess.run([python, "-c", "import sys; assert sys.version_info >= (3, 10), 'Python 3.10+ is required'"], check=True)
    subprocess.run(["xcrun", "--find", "swiftc"], check=True, stdout=subprocess.DEVNULL)
    if not args.standalone and not shutil.which("codex"):
        parser.error("Install Codex and make it available on PATH before building the app.")
    output = args.output.expanduser().absolute()
    destination = args.install_dir.expanduser().absolute() / BUNDLE_NAME
    legacy = legacy_installation(destination) if args.install else None
    for path in {output, *([destination] if args.install else []), *([legacy] if legacy else [])}:
        check_destination(path)
        check_not_running(path)

    sys.path.insert(0, str(SCRIPTS))
    from agent_coord.store import default_database_path

    database = (args.database or default_database_path()).expanduser().absolute()
    with tempfile.TemporaryDirectory(prefix="agent-coord-build-") as scratch:
        scratch = Path(scratch)
        app = scratch / BUNDLE_NAME
        contents = app / "Contents"
        resources = contents / "Resources"
        binaries = contents / "MacOS"
        resources.mkdir(parents=True)
        binaries.mkdir()
        snapshot = copy_backend(resources, standalone=args.standalone)
        if args.standalone:
            from distribution import bundle_runtime
            runtime = bundle_runtime(resources, args.arch, ROOT / "build/macos/runtime-cache")
        # Keep optional crypto/network packages out of the dependency-free CLI.
        # Use the same interpreter as the backend for compatible binary wheels.
        uv = shutil.which("uv")
        if args.standalone and not uv:
            parser.error("Standalone builds require uv to install architecture-specific, hash-locked wheels.")
        installer = ([uv, "pip", "install", "--python", python] if uv else
                     [python, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile"])
        if args.standalone:
            installer += ["--python-version", runtime["version"], "--python-platform", runtime["triple"]]
            for package in ("aiohttp", "cffi", "cryptography", "frozenlist", "multidict", "propcache", "yarl"):
                installer += ["--only-binary", package]
        subprocess.run(installer + ["--require-hashes", "--target", str(resources / "push-libs"),
                                    "-r", str(HERE / "push-requirements.txt")], check=True,
                       env=dict(os.environ, MACOSX_DEPLOYMENT_TARGET="12.0"))
        shutil.copyfile(HERE / "push-requirements.txt", resources / "push-requirements.txt")
        for name in ("backend.py", "bridge.js", "command-palette.js"):
            shutil.copyfile(HERE / name, resources / name)
        config = {
            "python": str(Path(python).absolute()), "database": str(database),
            "path": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        }
        if args.standalone:
            config = {"python": "runtime/bin/python3", "database": None, "path": None}
        (resources / "backend.json").write_text(json.dumps(config, indent=2) + "\n")
        (resources / "backend-snapshot.json").write_text(json.dumps(snapshot, indent=2) + "\n")
        shutil.copyfile(ROOT / "LICENSE", resources / "LICENSE")
        info = {
                "CFBundleIdentifier": BUNDLE_ID, "CFBundleName": APP_NAME,
                "CFBundleDisplayName": APP_NAME, "CFBundleExecutable": EXECUTABLE_NAME,
                "CFBundlePackageType": "APPL", "CFBundleShortVersionString": args.version,
                "CFBundleVersion": args.build_number, "CFBundleIconFile": "AppIcon",
                "LSMinimumSystemVersion": "12.0", "NSHighResolutionCapable": True,
                "NSPrincipalClass": "NSApplication",
                "CFBundleURLTypes": [{"CFBundleURLName": BUNDLE_ID, "CFBundleURLSchemes": ["agentcoord"],
                                      "CFBundleTypeRole": "Viewer"}],
                "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
            }
        (contents / "Info.plist").write_text(json.dumps(info))
        subprocess.run(["/usr/bin/plutil", "-convert", "xml1", str(contents / "Info.plist")], check=True)
        environment = dict(os.environ, MACOSX_DEPLOYMENT_TARGET="12.0")
        subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-O",
                        "-target", f"{args.arch}-apple-macosx12.0", str(HERE / "App.swift"),
                        str(HERE / "WorkspaceFiles.swift"), str(HERE / "FileBrowser.swift"),
                        "-o", str(binaries / EXECUTABLE_NAME)], check=True, env=environment)
        subprocess.run(["xcrun", "swift", str(HERE / "Icon.swift"), str(scratch / "AppIcon.iconset")], check=True)
        subprocess.run(["iconutil", "-c", "icns", str(scratch / "AppIcon.iconset"),
                        "-o", str(resources / "AppIcon.icns")], check=True)
        from distribution import sign_app, notarize, staple
        sign_app(app, args.sign_identity)
        if args.notary_profile:
            archive = scratch / "Ribbon-Field.zip"
            subprocess.run(["ditto", "-c", "-k", "--keepParent", str(app), str(archive)], check=True)
            notarize(archive, args.notary_profile)
            staple(app)
            subprocess.run(["spctl", "--assess", "--type", "execute", "--verbose=2", str(app)], check=True)
        replace_app(app, output, legacy=legacy if args.install and output == destination else None)
    if args.install and destination != output:
        replace_app(output, destination, legacy=legacy)
    if args.install:
        register_app(destination)
    if args.dmg:
        from distribution import create_dmg
        create_dmg(output, args.dmg.expanduser().absolute(), identity=args.sign_identity,
                   profile=args.notary_profile, version=args.version, architecture=args.arch)
    print(json.dumps({"built": str(output), "installed": str(destination) if args.install else None,
                      "standalone": args.standalone, "architecture": args.arch,
                      "dmg": str(args.dmg) if args.dmg else None,
                      "notarized": bool(args.notary_profile)}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
