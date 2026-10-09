"""Selected composer references: data for the receiving agent, never routing."""
from __future__ import annotations
import json
from .store import CoordinationError


def thread_mentions(message, body, sessions):
    mentions = body.get("mentions", [])
    if not isinstance(mentions, list) or len(mentions) > 50:
        raise CoordinationError("Choose at most 50 thread references.")
    if not mentions:
        return []
    visible = {t["thread_id"] for archived in (False, True)
               for t in sessions.list_work_threads(archived=archived)}
    encoded = message.encode("utf-16-le")
    result, previous = [], 0
    for ref in mentions:
        if not isinstance(ref, dict) or set(ref) != {"start", "end", "text", "session_id"}:
            raise CoordinationError("Invalid selected thread reference.")
        start, end = ref["start"], ref["end"]
        if type(start) is not int or type(end) is not int or not previous <= start < end <= len(encoded) // 2:
            raise CoordinationError("Thread reference positions no longer match the message.")
        try:
            text = encoded[start * 2:end * 2].decode("utf-16-le")
        except UnicodeDecodeError as exc:
            raise CoordinationError("Invalid thread reference position.") from exc
        if text != ref["text"] or not text.startswith("@") or len(text) < 2:
            raise CoordinationError("Thread reference text changed. Select the thread again.")
        if not isinstance(ref["session_id"], str) or ref["session_id"] not in visible:
            raise CoordinationError("A referenced thread is no longer visible. Remove its reference or select another thread.")
        result.append(dict(ref))
        previous = end
    return result


def provider_message(message, mentions):
    if not mentions:
        return message
    return message + "\n\n[Selected thread references; positions are UTF-16 offsets in the user text above]\n" + json.dumps(mentions, ensure_ascii=False)
