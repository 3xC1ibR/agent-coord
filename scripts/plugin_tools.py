#!/usr/bin/env python3
"""Repository-owned checks and cache refresh helpers for the Agent Coord plugin.

These validate this project's layout, not every possible client manifest schema.
Client installation and Claude's strict validator remain part of the workflow.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/agent-coord"
VERSION = re.compile(r"(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)(?:\+codex\.[0-9]+)?")


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
    for skill in ("agent-coordination", "manage-threads"):
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
    for source in sorted(plugin.rglob("*")):
        if not source.is_file() or "__pycache__" in source.parts or source.suffix == ".pyc":
            continue
        target = installed / source.relative_to(plugin)
        if not target.is_file() or source.read_bytes() != target.read_bytes():
            raise ValueError(f"Installed cache does not match source: {target}")
        count += 1
    if not count:
        raise ValueError(f"No source files to verify: {plugin}")
    return count


def install_codex(marketplace: str, *, codex_home: Path | None = None) -> dict:
    """Keep old hook paths usable for sessions that loaded them before refresh."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", marketplace):
        raise ValueError("Invalid marketplace name.")
    home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    cache = home / "plugins/cache" / marketplace / "agent-coord"
    with tempfile.TemporaryDirectory(prefix="agent-coord-plugin-backup-") as temporary:
        backup = Path(temporary) / "versions"
        if cache.exists():
            shutil.copytree(cache, backup, symlinks=True)
        try:
            result = subprocess.run(["codex", "plugin", "add", f"agent-coord@{marketplace}", "--json"],
                                    check=True, capture_output=True, text=True)
        finally:
            if backup.exists():
                cache.mkdir(parents=True, exist_ok=True)
                for previous in backup.iterdir():
                    target = cache / previous.name
                    if os.path.lexists(target):
                        continue
                    if previous.is_symlink():
                        target.symlink_to(os.readlink(previous))
                    elif previous.is_dir():
                        shutil.copytree(previous, target, symlinks=True)
                    else:
                        shutil.copy2(previous, target)
        return json.loads(result.stdout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", type=Path, default=PLUGIN)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    commands.add_parser("cachebuster")
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
