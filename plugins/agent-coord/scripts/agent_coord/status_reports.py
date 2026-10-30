"""Global generated status reports and durable, reversible user dismissal.

Producers atomically publish immutable directories containing manifest.json,
report.json and report.html. The inbox is independent of workspaces, providers,
and the coordination database. Only user review changes dismissal records.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .store import CoordinationError

IDENTIFIER = re.compile(r"[a-f0-9]{64}\Z")
REPORT_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
              "base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox")


class StatusReports:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else Path(os.environ.get(
            "AGENT_STATUS_INBOX", Path.home() / ".local/state/agent-status/inbox")).expanduser()

    def _directory(self, identifier):
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise CoordinationError("Invalid status report identifier.")
        directory = self.root / "reports" / identifier
        if directory.is_symlink() or not directory.is_dir():
            raise CoordinationError("This status report is unavailable.")
        return directory

    def _read(self, path, maximum):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as source:
                metadata = os.fstat(source.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
                    raise CoordinationError("The status record is not a supported file.")
                content = source.read(maximum + 1)
                if len(content) > maximum:
                    raise CoordinationError("The status record is too large.")
                return content.decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise CoordinationError("The status report cannot be read.") from error

    def _manifest(self, identifier):
        directory = self._directory(identifier)
        try:
            manifest = json.loads(self._read(directory / "manifest.json", 256 * 1024))
            if manifest.get("version") != 1 or manifest.get("id") != identifier:
                raise ValueError("Unsupported report record")
            for field in ("title", "subject", "summary", "generated_at", "received_at"):
                if not isinstance(manifest[field], str) or not manifest[field].strip():
                    raise ValueError("Missing report field")
            for field in ("generated_at", "received_at"):
                if datetime.fromisoformat(manifest[field].replace("Z", "+00:00")).utcoffset() is None:
                    raise ValueError("Timezone required")
            origin = manifest["origin"]
            if not isinstance(origin, dict) or any(value is not None and not isinstance(value, str) for value in origin.values()):
                raise ValueError("Invalid report origin")
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise CoordinationError("The status report record is invalid.") from error
        state = self.root / "dismissed" / (identifier + ".json")
        dismissed_at = None
        if state.exists() or state.is_symlink():
            try:
                dismissed_at = json.loads(self._read(state, 4096))["dismissed_at"]
                if not isinstance(dismissed_at, str):
                    raise ValueError("Invalid dismissal")
            except (ValueError, KeyError, TypeError) as error:
                raise CoordinationError("The report's dismissal record is invalid.") from error
        return {**manifest, "dismissed_at": dismissed_at}

    def list(self):
        reports, unreadable = [], 0
        directory = self.root / "reports"
        if directory.exists():
            for entry in directory.iterdir():
                if not IDENTIFIER.fullmatch(entry.name):
                    continue
                try:
                    reports.append(self._manifest(entry.name))
                except CoordinationError:
                    unreadable += 1
        reports.sort(key=lambda report: (datetime.fromisoformat(report["received_at"].replace("Z", "+00:00")), report["id"]), reverse=True)
        return {"reports": reports, "unreadable": unreadable}

    def content(self, identifier):
        self._manifest(identifier)
        return self._read(self._directory(identifier) / "report.html", 8 * 1024 * 1024)

    def dismiss(self, identifier, dismissed):
        if type(dismissed) is not bool:
            raise CoordinationError("Choose dismissed: true or false.")
        self._manifest(identifier)
        directory = self.root / "dismissed"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / (identifier + ".json")
        if dismissed:
            descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=directory)
            try:
                with os.fdopen(descriptor, "w") as target:
                    json.dump({"dismissed_at": datetime.now(timezone.utc).isoformat()}, target)
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        else:
            destination.unlink(missing_ok=True)
        return self._manifest(identifier)
