"""Expose installed skills inside the private ACP profile's readable paths.

Snapshots contain real files, including skill resources, rather than links out
of Antigravity's workspace. Immutable directories preserve the resources used
by other conversations when an installed skill changes.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .antigravity_setup import provider_directory
from .context import cli_path
from .store import CoordinationError


@dataclass(frozen=True)
class SkillContext:
    coordination_skill: Path
    instructions: str


def _metadata(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    values = {}
    if lines and lines[0].strip() == "---":
        key = None
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if line[:1].isspace() and key:
                values[key] += " " + line.strip()
            elif ":" in line:
                key, value = line.split(":", 1)
                key = key.strip()
                values[key] = "" if value.strip() in {"|", ">", "|-", ">-"} else value.strip().strip("\"'")
            else:
                key = None
    return values.get("name") or path.parent.name, " ".join(values.get("description", "").split())[:1000]


def _snapshot(source, destination):
    """Hash outside database scopes and publish a complete resource tree."""
    files, skills = [], []
    ignored = {".git", ".venv", "__pycache__", "node_modules", ".DS_Store"}

    def collect(directory, relative, ancestors):
        resolved = directory.resolve()
        if resolved in ancestors:
            raise CoordinationError(f"Skill resources contain a directory link cycle: {directory}")
        for path in sorted(directory.iterdir()):
            if path.name in ignored:
                continue
            target = relative / path.name
            if path.is_dir():
                collect(path, target, ancestors | {resolved})
            elif path.is_file():
                files.append((target, path.read_bytes(), path.stat().st_mode & 0o777))

    for directory in sorted(source.iterdir()) if source.is_dir() else []:
        definition = directory / "SKILL.md"
        if directory.is_dir() and definition.is_file():
            name, description = _metadata(definition)
            skills.append((name, description, directory.name))
            collect(directory, Path(directory.name), set())
    digest = hashlib.sha256()
    for relative, content, mode in files:
        header = json.dumps([str(relative), len(content), mode]).encode("utf-8")
        digest.update(len(header).to_bytes(8, "big"))
        digest.update(header)
        digest.update(content)
    target = destination / digest.hexdigest()
    if not target.is_dir():
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=destination) as temporary:
            staging = Path(temporary) / "skills"
            staging.mkdir(mode=0o700)
            for relative, content, mode in files:
                path = staging / relative
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.write_bytes(content)
                path.chmod(mode)
            try:
                staging.rename(target)
            except OSError:
                if not target.is_dir():
                    raise
                # A concurrent connection published this same complete snapshot.
    return target, [(name, description, target / folder / "SKILL.md") for name, description, folder in skills]


def prepare_skills(store, cwd, *, home=None, bundled=None):
    """Catalog workspace, personal, native global, and bundled skills."""
    home = Path.home() if home is None else Path(home)
    bundled = cli_path().parent.parent / "skills" if bundled is None else Path(bundled)
    workspace = Path(cwd)
    destination = provider_directory(store) / "profile/antigravity-acp/skill-snapshots"
    sources = [workspace / ".agents/skills", workspace / ".gemini/skills",
               home / ".agents/skills", home / ".gemini/config/skills", bundled]
    inventory, bundle_snapshot = {}, None
    try:
        for source in sources:
            snapshot, entries = _snapshot(source, destination)
            if source == bundled:
                bundle_snapshot = snapshot
            for name, description, path in entries:
                inventory.setdefault(name, {"name": name, "description": description, "path": str(path)})
    except (OSError, UnicodeError) as exc:
        raise CoordinationError(f"Cannot prepare Antigravity skills: {exc}") from exc
    coordination = bundle_snapshot / "agent-coordination/SKILL.md"
    if not coordination.is_file():
        raise CoordinationError(f"The bundled agent-coordination skill is missing: {bundled}. Reinstall Ribbon Field.")
    instructions = (
        "Available skills are listed below with readable absolute paths. When the user names a skill, "
        "read its SKILL.md before acting and follow its workflow. Use the catalog's exact path; "
        "resolve supporting files relative to that copy. Do not search the home directory for an installed skill. "
        "If a requested skill is absent or cannot be read, explain that instead of silently skipping it.\n"
        + json.dumps(sorted(inventory.values(), key=lambda item: item["name"].casefold()), ensure_ascii=False)
    )
    return SkillContext(coordination, instructions)
