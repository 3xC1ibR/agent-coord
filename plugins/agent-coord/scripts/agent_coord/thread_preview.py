"""Read-only, bounded conversation previews; never resume or mark a thread seen."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

MESSAGE_LIMIT = 1600
TRANSCRIPT_TAIL_BYTES = 256 * 1024


def _message(role, text, timestamp=None):
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    return {"role": role, "text": text[:MESSAGE_LIMIT],
            "truncated": len(text) > MESSAGE_LIMIT, "timestamp": timestamp}


def latest_history_message(history):
    for turn in reversed(history.get("turns", [])):
        for item in reversed(turn.get("items", [])):
            kind = item.get("type")
            if kind == "agentMessage":
                message = _message("assistant", item.get("text"))
            elif kind == "userMessage":
                text = "\n".join(part.get("text", "[Image]" if part.get("type") in {"image", "localImage"} else "")
                                 for part in item.get("content", []))
                message = _message("user", text)
            else:
                continue
            if message:
                return message
    return None


def latest_terminal_message(thread):
    # Match only this session's transcript, never arbitrary paths supplied by a request.
    try:
        session_id = str(uuid.UUID(thread["thread_id"]))
    except (ValueError, TypeError):
        return None
    if thread["client"] == "codex":
        root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        paths = list((root / "sessions").glob(f"**/*-{session_id}.jsonl"))
        paths += list((root / "archived_sessions").glob(f"**/*-{session_id}.jsonl"))
    elif thread["client"] == "claude":
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
        paths = list((root / "projects").glob(f"*/{session_id}.jsonl"))
    else:
        return None
    if len(paths) != 1:
        return None
    try:
        with paths[0].open("rb") as stream:
            stream.seek(0, 2)
            start = max(0, stream.tell() - TRANSCRIPT_TAIL_BYTES)
            stream.seek(start)
            if start:
                stream.readline()  # Discard a possibly partial leading record.
            lines = stream.read(TRANSCRIPT_TAIL_BYTES).splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        if thread["client"] == "codex":
            payload = record.get("payload", {})
            if record.get("type") != "response_item" or not isinstance(payload, dict) or payload.get("type") != "message":
                continue
        else:
            payload = record.get("message", {})
            if record.get("type") not in {"user", "assistant"} or not isinstance(payload, dict):
                continue
        role = payload.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = payload.get("content", [])
        text = content if isinstance(content, str) else "\n".join(
            part["text"] for part in content if isinstance(part, dict)
            and part.get("type") in {"text", "input_text", "output_text"} and isinstance(part.get("text"), str))
        message = _message(role, text, record.get("timestamp"))
        if message:
            return message
    return None


def thread_preview(sessions, thread_id):
    thread = sessions.work_thread(thread_id, history=False)  # Enforces workspace visibility.
    if thread["browser_session"]:
        with sessions.lock:
            message = latest_history_message(sessions._history(thread_id))
    else:
        message = latest_terminal_message(thread)
    return {"thread": thread, "latest_message": message}
