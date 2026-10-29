"""The recent view of a conversation history, for opening a conversation.

Opening a conversation transferred its complete history; tool output made
long conversations several megabytes. The recent view keeps the latest turns
and shortens long tool fields in completed turns. It is computed for one
response from a copy and never stored. The complete history remains the
durable record and is read on request.
"""
from __future__ import annotations

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


def _abridged_item(item: dict) -> dict:
    if item.get("type") in COMPLETE_ITEM_TYPES:
        return item
    shortened, cut = _shorten(item)
    if cut:
        shortened["abridged"] = True
    return shortened


def recent_view(thread: dict, *, live_turn: str | None = None) -> dict:
    """Return a new thread with the latest turns; the argument is not changed.

    A running turn stays complete: its streamed deltas extend the full fields.
    """
    turns = thread.get("turns") or []
    kept = turns[-RECENT_TURNS:]
    view = {key: value for key, value in thread.items() if key != "turns"}
    view["turns"] = [turn if turn.get("status") == "inProgress" or turn.get("id") == live_turn else
                     {**turn, "items": [_abridged_item(item) for item in turn.get("items") or []]}
                     for turn in kept]
    view["earlierTurns"] = len(turns) - len(kept)
    view["view"] = "recent"
    return view
