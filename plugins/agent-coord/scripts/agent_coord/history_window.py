"""The recent view of a conversation history, Turn abridgement, and prose extraction.

Opening a conversation transfers its recent window of pre-abridged turns;
complete tool outputs are read on demand from ``turns.full.jsonl`` by byte offset.
"""
from __future__ import annotations

from .timeline_clock import seconds
from .zellij_wake import WAKE_PROMPT

RECENT_TURNS = 40
PREVIEW_CHARACTERS = 512
PREVIEW_ENTRIES = 10
# The conversation itself stays complete; only tool calls and their output shorten.
COMPLETE_ITEM_TYPES = frozenset({"userMessage", "agentMessage", "reasoning", "plan"})


def _shorten(value):
    if isinstance(value, str):
        return (value[:PREVIEW_CHARACTERS], True) if len(value) > PREVIEW_CHARACTERS else (value, False)
    if isinstance(value, list):
        parts = [_shorten(entry) for entry in value[:PREVIEW_ENTRIES]]
        return [part for part, _ in parts], len(value) > PREVIEW_ENTRIES or any(cut for _, cut in parts)
    if isinstance(value, dict):
        parts = {key: _shorten(entry) for key, entry in value.items()}
        return {key: part for key, (part, _) in parts.items()}, any(cut for _, cut in parts.values())
    return value, False


def _abridged_item(item: dict, *, blob_ref: dict | None = None) -> dict:
    if item.get("type") in COMPLETE_ITEM_TYPES:
        return item
    shortened, cut = _shorten(item)
    if cut:
        shortened["abridged"] = True
        if blob_ref is not None:
            shortened["blobRef"] = dict(blob_ref)
    return shortened


def abridge_turn(turn: dict, *, full_offset: int | None = None, full_length: int | None = None) -> dict:
    """Return an abridged copy of one finalized turn with optional full-turn byte coordinates."""
    blob_ref = (
        {"turnId": turn.get("id"), "offset": full_offset, "length": full_length}
        if full_offset is not None and full_length is not None
        else None
    )
    return {
        **turn,
        "items": [_abridged_item(item, blob_ref=blob_ref) for item in turn.get("items") or []],
    }


def recent_view(
    thread: dict,
    *,
    live_turn: str | None = None,
    before_seq: int | None = None,
    limit: int = RECENT_TURNS,
) -> dict:
    """Return a new thread with a bounded window of turns; the argument is not changed.

    A running turn stays complete: its streamed deltas extend the full fields.
    """
    turns = thread.get("turns") or []
    end = len(turns) if before_seq is None else max(0, min(len(turns), before_seq))
    start = max(0, end - limit)
    kept = turns[start:end]
    view = {key: value for key, value in thread.items() if key != "turns"}
    view["turns"] = [
        turn
        if turn.get("status") == "inProgress" or turn.get("id") == live_turn
        else {**turn, "items": [_abridged_item(item) for item in turn.get("items") or []]}
        for turn in kept
    ]
    view["earlierTurns"] = start
    if start > 0:
        view["beforeSeq"] = start
    view["view"] = "recent"
    return view


def turn_messages(turn: dict, turn_index: int = 0):
    """Yield ``(turn_id, item_id, role, timestamp, text, folded)`` prose items in one turn."""
    if not isinstance(turn, dict):
        return
    for item_index, item in enumerate(turn.get("items") or []):
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "userMessage":
            text = "\n".join(
                part["text"]
                for part in (item.get("content") or [])
                if isinstance(part, dict)
                and part.get("type") in {"text", "input_text"}
                and isinstance(part.get("text"), str)
            )
            role = "user"
            if text.strip() == WAKE_PROMPT or text.lstrip().startswith(
                (
                    "[Automatic Agent Coord inbox wake",
                    "[Automatic Agent Coord message delivery",
                    "Check and handle your unread agent-coord messages.",
                    "# AGENTS.md",
                    "<environment_context>",
                    "<INSTRUCTIONS>",
                    "<agent-coord",
                    "[agent-coord",
                )
            ):
                continue
        elif kind == "agentMessage" and isinstance(item.get("text"), str):
            text, role = item["text"], "assistant"
        else:
            continue
        if text.strip():
            yield (
                str(turn.get("id") or f"search-turn-{turn_index}"),
                str(item.get("id") or f"search-item-{item_index}"),
                role,
                seconds(item.get("timelineAt")) or seconds(turn.get("startedAt")),
                text,
                text.casefold(),
            )


def messages(history):
    """Yield ``(turn_id, item_id, role, timestamp, text, folded)`` prose items across a history."""
    if not isinstance(history, dict):
        return
    for turn_index, turn in enumerate(history.get("turns") or []):
        yield from turn_messages(turn, turn_index)


def routing_history(history, records=None) -> dict:
    """Return the bounded prose view used by dispatch routing."""
    if not isinstance(history, dict):
        return {"history_status": "unavailable", "messages": [], "history_truncated": False}
    turns = history.get("turns")
    if turns is not None and not isinstance(turns, list):
        return {"history_status": "unavailable", "messages": [], "history_truncated": False}
    if isinstance(turns, list) and any(
        not isinstance(turn, dict) or (turn.get("items") is not None and not isinstance(turn.get("items"), list))
        for turn in turns
    ):
        return {"history_status": "unavailable", "messages": [], "history_truncated": False}
    records = list(messages(history)) if records is None else records
    return {
        "history_status": "partial" if history.get("historyUnavailable") else "saved",
        "messages": [
            {
                "turn_id": record[0],
                "item_id": record[1],
                "role": record[2],
                "timestamp": record[3],
                "text": record[4][:6000],
                "truncated": len(record[4]) > 6000,
            }
            for record in records[-200:]
        ],
        "history_truncated": len(records) > 200,
    }
