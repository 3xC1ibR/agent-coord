"""Content-free observations of Claude's native transcripts and tool processes."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import uuid


class TranscriptProgress:
    """Record file identity and length without opening potentially large transcripts."""

    def __init__(self, native_id):
        try:
            self.identity = str(uuid.UUID(native_id))
        except (TypeError, ValueError, AttributeError):
            self.identity = None
        self.path = None
        self.files = {}

    def sample(self):
        if self.identity is None:
            return None
        if self.path is None:
            root = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
            matches = [path for path in root.glob("*/" + self.identity + ".jsonl")
                       if not path.parent.is_symlink() and not path.is_symlink()]
            if len(matches) != 1:
                return None
            self.path = matches[0]
        directory = self.path.with_suffix("") / "subagents"
        try:
            paths = [self.path]
            if not directory.is_symlink() and not directory.parent.is_symlink():
                paths.extend(directory.glob("*.jsonl"))
            files = {}
            latest = None
            for path in paths:
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                identity = (info.st_dev, info.st_ino)
                files[path] = (identity, info.st_size)
                old_identity, old_size = self.files.get(path, (None, 0))
                if info.st_size and (identity != old_identity or info.st_size > old_size):
                    latest = info.st_mtime if latest is None else max(latest, info.st_mtime)
            self.files = files
            if self.path not in files:
                self.path = None
            return latest
        except OSError:
            return None


def _cpu_seconds(value):
    days, separator, time_value = value.partition("-")
    if not separator:
        time_value, days = days, "0"
    seconds = 0.0
    for part in time_value.split(":"):
        seconds = seconds * 60 + float(part)
    return int(days) * 86400 + seconds


def _descendants(rows, roots):
    children = {}
    for pid, row in rows.items():
        children.setdefault(row[0], []).append(pid)
    found = set()
    pending = list(roots)
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in found and child not in roots:
                found.add(child)
                pending.append(child)
    return found


def read_process_table(provider_pids):
    """One process-tree sample per watchdog scan. Inspect arguments only for candidate Python helpers."""
    if not provider_pids:
        return {}
    try:
        sample = subprocess.run(["ps", "-axo", "pid=,ppid=,time=,stat=,comm="],
                                capture_output=True, text=True, errors="replace", timeout=2, check=True)
        rows = {}
        python_pids = set()
        for line in sample.stdout.splitlines():
            fields = line.split(None, 4)
            if len(fields) != 5 or fields[3].startswith("Z"):
                continue
            pid, parent, cpu, _, executable = fields
            pid = int(pid)
            rows[pid] = (int(parent), _cpu_seconds(cpu), False)
            if Path(executable).name.lower().startswith("python"):
                python_pids.add(pid)
        candidates = python_pids & _descendants(rows, provider_pids)
        if candidates:
            # The persistent stdio MCP helper sleeps for the lifetime of Claude.
            # A Python interpreter running an actual tool remains a live child.
            arguments = subprocess.run(["ps", "-ww", "-p", ",".join(map(str, sorted(candidates))),
                                        "-o", "pid=,args="], capture_output=True, text=True,
                                       errors="replace", timeout=2)
            for line in arguments.stdout.splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2 and "from agent_coord.checkpoint_mcp import main;" in parts[1]:
                    pid = int(parts[0])
                    if pid in candidates:
                        rows[pid] = (*rows[pid][:2], True)
        return rows
    except (OSError, ValueError, subprocess.SubprocessError):
        # A failed observation cannot establish the absence of running tools.
        return None


def tool_cpu_times(rows, provider_pid):
    if rows is None or provider_pid not in rows:
        return None
    return {pid: rows[pid][1] for pid in _descendants(rows, {provider_pid}) if not rows[pid][2]}
