from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import CoordinationError, CoordinationStore

USAGE_SCHEMA_VERSION = 1
USAGE_DIRECTORY = Path(".agent-coord") / "delegations"
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9_.-]")


class UsageParseError(CoordinationError):
    """A delegated transcript does not contain recognizable token usage."""


def _token_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _json_lines(path: Path):
    try:
        with path.open(encoding="utf-8") as transcript:
            for line in transcript:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    # A live transcript can end with one line that has not been
                    # flushed yet. Earlier complete usage events remain valid.
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError as exc:
        raise UsageParseError(f"Cannot read transcript {path}: {exc}") from exc


def _codex_usage(path: Path) -> dict[str, Any]:
    latest: dict[str, Any] | None = None
    for event in _json_lines(path):
        payload = event.get("payload")
        if event.get("type") != "event_msg" or not isinstance(payload, dict):
            continue
        info = payload.get("info")
        if payload.get("type") != "token_count" or not isinstance(info, dict):
            continue
        total = info.get("total_token_usage")
        if isinstance(total, dict):
            latest = total
    if latest is None:
        raise UsageParseError("Codex transcript contains no cumulative token usage.")

    client_usage = {
        "input_tokens": _token_count(latest.get("input_tokens")),
        "cached_input_tokens": _token_count(latest.get("cached_input_tokens")),
        "cache_write_input_tokens": _token_count(
            latest.get("cache_write_input_tokens")
        ),
        "output_tokens": _token_count(latest.get("output_tokens")),
        "reasoning_output_tokens": _token_count(
            latest.get("reasoning_output_tokens")
        ),
        "total_tokens": _token_count(latest.get("total_tokens")),
    }
    if not client_usage["total_tokens"]:
        client_usage["total_tokens"] = (
            client_usage["input_tokens"] + client_usage["output_tokens"]
        )
    normalized = dict(client_usage)
    return {"normalized": normalized, "client": client_usage}


def _claude_usage(path: Path) -> dict[str, Any]:
    messages: dict[str, dict[str, Any]] = {}
    anonymous = 0
    for event in _json_lines(path):
        message = event.get("message")
        if event.get("type") != "assistant" or not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id:
            event_id = event.get("uuid")
            if isinstance(event_id, str) and event_id:
                message_id = event_id
            else:
                anonymous += 1
                message_id = f"anonymous-{anonymous}"
        messages[message_id] = usage
    if not messages:
        raise UsageParseError("Claude transcript contains no assistant token usage.")

    client_usage = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    for usage in messages.values():
        for key in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        ):
            client_usage[key] += _token_count(usage.get(key))
        output_details = usage.get("output_tokens_details")
        if isinstance(output_details, dict):
            client_usage["reasoning_output_tokens"] += _token_count(
                output_details.get("reasoning_tokens")
            )

    normalized_input = (
        client_usage["input_tokens"]
        + client_usage["cache_creation_input_tokens"]
        + client_usage["cache_read_input_tokens"]
    )
    normalized = {
        "input_tokens": normalized_input,
        "cached_input_tokens": client_usage["cache_read_input_tokens"],
        "cache_write_input_tokens": client_usage["cache_creation_input_tokens"],
        "output_tokens": client_usage["output_tokens"],
        "reasoning_output_tokens": client_usage["reasoning_output_tokens"],
        "total_tokens": normalized_input + client_usage["output_tokens"],
    }
    client_usage["total_tokens"] = normalized["total_tokens"]
    return {"normalized": normalized, "client": client_usage}


def parse_transcript_usage(client: str, transcript_path: str | Path) -> dict[str, Any]:
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        raise UsageParseError(f"Transcript does not exist: {path}")
    if client == "codex":
        return _codex_usage(path)
    if client == "claude":
        return _claude_usage(path)
    raise UsageParseError(f"Unsupported delegated client: {client}")


def _artifact_path(cwd: str, delegation_id: str) -> Path:
    root = Path(cwd).resolve()
    safe_id = _SAFE_FILENAME.sub("_", delegation_id).strip("._")
    if not safe_id:
        safe_id = "delegation"
    directory = root / USAGE_DIRECTORY
    if (root / USAGE_DIRECTORY.parts[0]).is_symlink():
        raise UsageParseError("Refusing to write usage through a .agent-coord symlink.")
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise UsageParseError("Refusing to write usage through a delegations symlink.")
    try:
        directory.resolve().relative_to(root)
    except ValueError as exc:
        raise UsageParseError(
            "Delegation usage directory escapes the repository."
        ) from exc
    return directory / f"{safe_id}.usage.json"


def _write_artifact(path: Path, artifact: dict[str, Any]) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            json.dump(artifact, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def capture_delegation_usage(
    store: CoordinationStore,
    *,
    delegation: dict[str, Any],
    session_id: str,
    transcript_path: str,
    capture_event: str,
    model: str | None,
) -> dict[str, Any]:
    parsed = parse_transcript_usage(delegation["client"], transcript_path)
    captured_at = datetime.fromtimestamp(store.clock(), tz=timezone.utc).isoformat()
    usage = {
        "schema_version": USAGE_SCHEMA_VERSION,
        "captured_at": captured_at,
        "capture_event": capture_event,
        "model": model or delegation.get("model"),
        **parsed,
    }
    artifact = {
        "schema_version": USAGE_SCHEMA_VERSION,
        "delegation_id": delegation["delegation_id"],
        "session_id": session_id,
        "client": delegation["client"],
        "cwd": delegation["cwd"],
        "status": delegation["status"],
        "usage": usage,
    }

    artifact_path: str | None = None
    artifact_error: str | None = None
    try:
        target = _artifact_path(delegation["cwd"], delegation["delegation_id"])
        _write_artifact(target, artifact)
        artifact_path = str(target)
    except (OSError, UsageParseError) as exc:
        artifact_error = f"Token usage was captured, but its artifact failed: {exc}"

    return store.record_delegation_token_usage(
        delegation["delegation_id"],
        child_session_id=session_id,
        usage=usage,
        capture_event=capture_event,
        artifact_path=artifact_path,
        error=artifact_error,
    )
