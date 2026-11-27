#!/usr/bin/env python3
"""Repository-owned checks and cache refresh helpers for the Agent Coord plugin.

These validate this project's layout, not every possible client manifest schema.
Client installation and Claude's strict validator remain part of the workflow.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from stat import S_ISREG
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/agent-coord"
VERSION = re.compile(r"(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)(?:\+codex\.[0-9]+)?")
SNAPSHOT_EXCLUDED_PREFIX = ("scripts", "agent_coord", "web", "vendor", "openui")


def _excluded_from_codex_snapshot(relative: Path) -> bool:
    return (
        "__pycache__" in relative.parts
        or relative.suffix == ".pyc"
        or relative.parts[:len(SNAPSHOT_EXCLUDED_PREFIX)] == SNAPSHOT_EXCLUDED_PREFIX
    )


def read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def marketplace_name(path: Path) -> str:
    name = read_object(path).get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise ValueError(f"Invalid marketplace name: {path}")
    return name


def validate(plugin: Path) -> None:
    manifests = [read_object(plugin / client / "plugin.json")
                 for client in (".codex-plugin", ".claude-plugin")]
    for manifest in manifests:
        if manifest.get("name") != "agent-coord":
            raise ValueError("Both manifests must name agent-coord.")
        if not isinstance(manifest.get("version"), str) or not VERSION.fullmatch(manifest["version"]):
            raise ValueError("Invalid plugin version or cachebuster suffix.")
        if not isinstance(manifest.get("description"), str) or not manifest["description"].strip():
            raise ValueError("Plugin descriptions must be nonempty strings.")
    if manifests[0].get("skills") != "./skills/":
        raise ValueError("Codex must use the shared ./skills/ directory.")
    for skill in ("agent-coordination", "manage-threads", "dispatch", "orchestrate"):
        path = plugin / "skills" / skill / "SKILL.md"
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---\n") or f"\nname: {skill}\n" not in content:
            raise ValueError(f"Missing skill frontmatter/name: {path}")
    hooks = [read_object(plugin / path)["hooks"] for path in ("hooks.json", "hooks/hooks.json")]
    if set(hooks[0]) != set(hooks[1]):
        raise ValueError("Client hooks must cover the same events.")
    for event in hooks[0]:
        if [g.get("matcher") for g in hooks[0][event]] != [g.get("matcher") for g in hooks[1][event]]:
            raise ValueError(f"Client hook matchers differ for {event}.")
        for client_hooks in hooks:
            for group in client_hooks[event]:
                for hook in group["hooks"]:
                    if hook.get("type") != "command" or "/scripts/agent-coord-hook" not in hook.get("command", ""):
                        raise ValueError(f"Unexpected hook command for {event}.")
    for path in ("scripts/agent-coord", "scripts/agent-coord-hook", "scripts/agent_coord/cli.py"):
        if not (plugin / path).is_file():
            raise ValueError(f"Missing shared runtime: {path}")
    web_root = plugin / "scripts/agent_coord/web"
    if (web_root / "index.html").is_file():
        import importlib.util
        spec = importlib.util.spec_from_file_location("build_web", ROOT / "scripts/build_web.py")
        if spec is None or spec.loader is None:
            raise ValueError("Missing scripts/build_web.py validator.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.verify(web_root)


def cachebuster(plugin: Path, *, stamp: str | None = None) -> str:
    validate(plugin)
    path = plugin / ".codex-plugin/plugin.json"
    manifest = read_object(path)
    base = VERSION.fullmatch(manifest["version"]).group(1)
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    if not re.fullmatch(r"[0-9]+", stamp):
        raise ValueError("Cachebuster must be numeric.")
    version = base + "+codex." + stamp
    if version == manifest["version"]:
        raise ValueError("Cachebuster must change the installed version.")
    manifest["version"] = version
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        temporary.chmod(path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return version


def verify(plugin: Path, installed: Path) -> int:
    count = 0
    codex_snapshot = (installed / ".agent-coord-development.json").is_file()
    for source in sorted(plugin.rglob("*")):
        if not source.is_file() or "__pycache__" in source.parts or source.suffix == ".pyc":
            continue
        relative = source.relative_to(plugin)
        if codex_snapshot and _excluded_from_codex_snapshot(relative):
            continue
        target = installed / relative
        if not target.is_file() or source.read_bytes() != target.read_bytes():
            raise ValueError(f"Installed cache does not match source: {target}")
        count += 1
    if not count:
        raise ValueError(f"No source files to verify: {plugin}")
    return count


def install_codex(marketplace: str, *, codex_home: Path | None = None) -> dict:
    """Install in isolation, then publish without pruning any live version.

    Backing up and restoring after `plugin add` leaves pinned hooks and web
    assets absent while Codex prunes the cache. Its installer only ever sees
    this disposable CODEX_HOME; failures and interrupts cannot prune live files.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", marketplace):
        raise ValueError("Invalid marketplace name.")
    home = (codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    cache = home / "plugins/cache" / marketplace / "agent-coord"
    # Serialize helpers without locking Codex's unrelated configuration writes.
    with (home / ".agent-coord-plugin-install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = (home / "config.toml").resolve()
        original = config.read_bytes() if config.exists() else None
        mode = config.stat().st_mode & 0o777 if original is not None else 0o600
        with tempfile.TemporaryDirectory(prefix=".agent-coord-install-", dir=home) as temporary:
            staging = Path(temporary)
            staged_config = staging / "config.toml"
            if original is not None:
                staged_config.write_bytes(original)
                staged_config.chmod(0o600)
            result = subprocess.run(["codex", "plugin", "add", f"agent-coord@{marketplace}", "--json"],
                                    check=True, capture_output=True, text=True,
                                    env={**os.environ, "CODEX_HOME": str(staging)})
            receipt = json.loads(result.stdout)
            version = receipt["version"]
            if not isinstance(version, str) or not VERSION.fullmatch(version):
                raise ValueError("Installer returned an invalid plugin version.")
            installed = staging / "plugins/cache" / marketplace / "agent-coord" / version
            if Path(receipt["installedPath"]).resolve() != installed.resolve() or not installed.is_dir():
                raise ValueError("Installer returned an unexpected cache path.")
            updated = staged_config.read_bytes()
            current = config.read_bytes() if config.exists() else None
            if current != original:
                raise ValueError("Codex configuration changed during installation; retry the refresh.")
            cache.mkdir(parents=True, exist_ok=True)
            target = cache / version
            if os.path.lexists(target):
                # Same-version updates must not mutate files pinned by an old
                # session. A new cachebuster is required for changed payloads.
                verify(installed, target)
            else:
                os.replace(installed, target)
            if updated != original:
                staged_config.chmod(mode)
                os.replace(staged_config, config)
            receipt["installedPath"] = str(target)
            return receipt


def _exchange_paths(left: Path, right: Path) -> None:
    """Exchange a legacy link and its replacement without a lookup gap."""
    libc = ctypes.CDLL(None, use_errno=True)
    names = (os.fsencode(left), os.fsencode(right))
    if sys.platform == "darwin":
        result = libc.renamex_np(*names, 2)  # RENAME_SWAP
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        result = libc.renameat2(-100, names[0], -100, names[1], 2)  # RENAME_EXCHANGE
    else:
        raise ValueError("Atomic exchange is required to repair a legacy Codex cache without a path gap.")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(left))


def _snapshot_digest(plugin: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(plugin.rglob("*")):
        relative = path.relative_to(plugin)
        if _excluded_from_codex_snapshot(relative) or path.name == ".agent-coord-development.json":
            continue
        if path.is_symlink():
            raise ValueError(f"Unsupported link in Codex snapshot: {path}")
        if path.is_file():
            digest.update(json.dumps([str(relative), path.stat().st_mode & 0o777]).encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _copy_codex_snapshot(source: Path, target: Path) -> None:
    def ignore(directory: str, entries: list[str]) -> set[str]:
        ignored = {name for name in entries if name == "__pycache__" or name.endswith(".pyc")}
        relative_dir = Path(directory).relative_to(source)
        if relative_dir.parts == SNAPSHOT_EXCLUDED_PREFIX[:-1] and SNAPSHOT_EXCLUDED_PREFIX[-1] in entries:
            ignored.add(SNAPSHOT_EXCLUDED_PREFIX[-1])
        return ignored

    shutil.copytree(source, target, ignore=ignore)
    _snapshot_digest(target)


def _repair_codex_links(cache: Path, current: Path, retained: Path, plugin: Path) -> int:
    """Materialize only links made by the former development installer.

    Keep retained trees in place: a running process may have resolved its old
    version to that path. Atomic exchange also keeps every pinned cache path valid.
    """
    repaired = 0
    for version in sorted(cache.iterdir()):
        if not VERSION.fullmatch(version.name) or not version.is_dir():
            continue
        legacy_root = version.is_symlink() and version.readlink() == retained / version.name
        if version.is_symlink() and not legacy_root:
            continue
        legacy_components = any((version / name).is_symlink() and
                                (version / name).readlink() == current / name
                                for name in ("scripts", "skills"))
        if not legacy_root and not legacy_components:
            continue
        with tempfile.TemporaryDirectory(dir=cache) as temporary:
            replacement = Path(temporary, "plugin")
            _copy_codex_snapshot(version, replacement)
            if legacy_components:
                (replacement / ".agent-coord-development.json").write_text(json.dumps({
                    "source": str(plugin), "digest": _snapshot_digest(replacement)}) + "\n")
            if legacy_root:
                _exchange_paths(replacement, version)
            else:
                # Preserve the cache directory inode for pinned working directories.
                for name in ("scripts", "skills"):
                    entry = version / name
                    if entry.is_symlink() and entry.readlink() == current / name:
                        _exchange_paths(replacement / name, entry)
                os.replace(replacement / ".agent-coord-development.json",
                           version / ".agent-coord-development.json")
        repaired += 1
    return repaired


def _prune_cached_versions(cache: Path, retained: Path) -> dict:
    """Share immutable file data without introducing unsupported cache links."""
    shared_bytes = 0
    identical = {}
    # Include retained originals, but never move or delete their pinned paths.
    versions = [p for root in (retained, cache) if root.is_dir() for p in sorted(root.iterdir())
                if VERSION.fullmatch(p.name) and p.is_dir() and not p.is_symlink()]
    with tempfile.TemporaryDirectory(dir=cache) as scratch:
        link = Path(scratch, "file")
        for version in versions:
            for directory, directories, files in os.walk(version):
                directories[:] = [name for name in directories if name != "__pycache__"
                                  and not Path(directory, name).is_symlink()]
                for name in files:
                    path = Path(directory, name)
                    status = path.lstat()
                    if not S_ISREG(status.st_mode) or name.endswith(".pyc"):
                        continue
                    key = (status.st_size, status.st_mode, status.st_uid, status.st_gid, status.st_mtime_ns,
                           hashlib.sha256(path.read_bytes()).digest())
                    original = identical.setdefault(key, path)
                    if original == path or original.samefile(path):
                        continue
                    os.link(original, link)
                    os.replace(link, path)
                    shared_bytes += status.st_size
    return {"deduplicatedBytes": shared_bytes}


def _publish_codex_snapshot(plugin: Path, target: Path) -> None:
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
        snapshot = Path(temporary, "plugin")
        _copy_codex_snapshot(plugin, snapshot)
        (snapshot / ".agent-coord-development.json").write_text(json.dumps({
            "source": str(plugin), "digest": _snapshot_digest(snapshot)}) + "\n")
        os.rename(snapshot, target)


def _matches_development_snapshot(plugin: Path, target: Path) -> bool:
    marker = target / ".agent-coord-development.json"
    if target.is_symlink() or not marker.is_file():
        return False
    metadata = read_object(marker)
    return (metadata.get("source") == str(plugin) and
            metadata.get("digest") == _snapshot_digest(plugin) == _snapshot_digest(target))


def _write_development_shim(plugin: Path, current: Path, target: Path) -> None:
    for path in plugin.iterdir():
        entry = target / path.name
        if path.name in {"scripts", "skills"}:
            if not os.path.lexists(entry):
                entry.symlink_to(current / path.name, target_is_directory=True)
            elif not entry.is_symlink() or entry.readlink() != current / path.name:
                raise ValueError(f"Unmanaged component in development installation: {entry}")
            continue
        # Claude supports live script/skill links; keep its registration files local.
        for source in path.rglob("*") if path.is_dir() else (path,):
            if not source.is_file():
                continue
            destination = target / source.relative_to(plugin)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_file() and destination.read_bytes() == source.read_bytes():
                continue
            with tempfile.TemporaryDirectory(dir=target) as scratch:
                copy = Path(scratch, "metadata")
                shutil.copy2(source, copy)
                os.replace(copy, destination)


def _publish_development_shim(plugin: Path, current: Path, target: Path) -> None:
    scripts = target / "scripts"
    if target.is_dir() and scripts.is_symlink() and scripts.readlink() == current / "scripts":
        _write_development_shim(plugin, current, target)
        return
    if os.path.lexists(target):
        raise ValueError(f"Development version collides with an existing cache: {target}")
    with tempfile.TemporaryDirectory(dir=target.parent) as name:
        staging = Path(name, "plugin")
        staging.mkdir()
        _write_development_shim(plugin, current, staging)
        os.rename(staging, target)


def dev_link(plugin: Path, *, prune=False, codex_home=None, claude_home=None, share=None) -> dict:
    """Refresh local clients while preserving every previous Codex snapshot."""
    plugin = plugin.resolve()
    validate(plugin)
    codex_home = Path(codex_home or os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
    claude_home = Path(claude_home or os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser().resolve()
    share = Path(share or Path.home() / ".local/share/agent-coord").expanduser().resolve()
    current = share / "current"
    marketplace = marketplace_name(ROOT / ".agents/plugins/marketplace.json")
    codex_cache = codex_home / "plugins/cache" / marketplace / "agent-coord"
    claude_cache = claude_home / "plugins/cache/agent-coord/agent-coord"
    if not codex_cache.is_dir() or not claude_cache.is_dir():
        raise ValueError("Install the Agent Coord marketplace plugin in both clients before running dev-link.")
    share.mkdir(parents=True, exist_ok=True)
    # Share the Codex installation lock with the immutable release installer.
    with (codex_home / ".agent-coord-plugin-install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        registry = claude_home / "plugins/installed_plugins.json"
        before = registry.read_bytes()
        data = json.loads(before)
        if data.get("version") != 2:
            raise ValueError("Unrecognized Claude plugin registry format.")
        rows = [row for row in data["plugins"].get("agent-coord@agent-coord", []) if row["scope"] == "user"]
        if len(rows) != 1 or not VERSION.fullmatch(rows[0]["version"]):
            raise ValueError("Expected a user installation of agent-coord@agent-coord.")
        row = rows[0]
        previous = claude_cache / row["version"]
        if Path(row["installPath"]).resolve() != previous.resolve() or not previous.is_dir():
            raise ValueError("Unexpected Claude plugin installation path.")
        installed = [p for p in codex_cache.iterdir() if VERSION.fullmatch(p.name) and p.is_dir()]
        if not installed:
            raise ValueError("Codex has no installed Agent Coord version.")
        if current.exists() and not current.is_symlink():
            raise ValueError(f"Unmanaged development pointer: {current}")
        with tempfile.TemporaryDirectory(dir=share) as temporary:
            pointer = Path(temporary, "current")
            pointer.symlink_to(plugin, target_is_directory=True)
            os.replace(pointer, current)
        base = VERSION.fullmatch(read_object(plugin / ".codex-plugin/plugin.json")["version"]).group(1)
        repaired = _repair_codex_links(codex_cache, current, share / "retained" / marketplace, plugin)
        # Source edits require a new Codex version; pinned sessions keep their bytes.
        candidates = [path for path in installed if VERSION.fullmatch(path.name).group(1) == base]
        latest = max(candidates, key=lambda path: int(path.name.partition("+codex.")[2] or 0), default=None)
        if latest and _matches_development_snapshot(plugin, latest):
            version = latest.name
        else:
            version = base + "+codex." + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        codex = codex_cache / version
        claude = claude_cache / version
        if not codex.exists():
            _publish_codex_snapshot(plugin, codex)
        _publish_development_shim(plugin, current, claude)
        if row["version"] != version:
            row.update(version=version, installPath=str(claude),
                       lastUpdated=datetime.now(timezone.utc).isoformat())
            with tempfile.TemporaryDirectory(dir=registry.parent) as name:
                updated = Path(name, "installed_plugins.json")
                updated.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                updated.chmod(registry.stat().st_mode & 0o777)
                if registry.read_bytes() != before:
                    raise ValueError("Claude plugin configuration changed; retry dev-link.")
                os.replace(updated, registry)
        result = {"current": str(current), "version": version, "codex": str(codex), "claude": str(claude),
                  "repairedCodexDirectories": repaired}
        if prune:
            result.update(_prune_cached_versions(codex_cache, share / "retained" / marketplace))
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", type=Path, default=PLUGIN)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    commands.add_parser("cachebuster")
    development = commands.add_parser("dev-link", help="Use one checkout for local Codex and Claude development")
    development.add_argument("--prune", action="store_true", help="Compact old Codex versions, preserving pinned paths")
    install = commands.add_parser("install-codex")
    install.add_argument("marketplace")
    marketplace = commands.add_parser("marketplace-name")
    marketplace.add_argument("--marketplace-path", type=Path, default=ROOT / ".agents/plugins/marketplace.json")
    check = commands.add_parser("verify")
    check.add_argument("installed", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "marketplace-name":
            print(marketplace_name(args.marketplace_path))
        elif args.command == "install-codex":
            print(json.dumps(install_codex(args.marketplace), indent=2))
        elif args.command == "dev-link":
            print(json.dumps(dev_link(args.plugin, prune=args.prune), indent=2))
        elif args.command == "cachebuster":
            print(cachebuster(args.plugin))
        elif args.command == "verify":
            print(f"Verified {verify(args.plugin, args.installed)} source files in {args.installed}")
        else:
            validate(args.plugin)
            print("Agent Coord plugin layout is valid.")
    except subprocess.CalledProcessError as exc:
        parser.exit(1, f"Codex installation failed: {exc.stderr or exc}\n")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"Plugin check failed: {exc}\n")


if __name__ == "__main__":
    main()
