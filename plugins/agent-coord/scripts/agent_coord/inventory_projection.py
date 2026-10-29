"""Thread-list cards: the inventory fields that lists, tabs and trees use.

The thread list feeds overview cards, tabs, the agent tree and the send
palette. A conversation's links, its context reuse advice and its complete
original request are shown only when that conversation is open, and the
single-thread endpoint still returns them. Sending them for every thread on
every list read made the response about 6 MB, which the backend encodes under
the shared GIL and every window parses again.
"""
from __future__ import annotations

import hashlib

ORIGINAL_REQUEST_PREVIEW = 600


def list_card(thread: dict) -> dict:
    card = dict(thread)
    request = card.get("original_request")
    if isinstance(request, str) and len(request) > ORIGINAL_REQUEST_PREVIEW:
        card["original_request"] = request[:ORIGINAL_REQUEST_PREVIEW]
        card["original_request_truncated"] = True
    card["link_count"] = len(card.pop("links", None) or [])
    checkpoint = card.get("checkpoint")
    if isinstance(checkpoint, dict) and "context_lifecycle" in checkpoint:
        card["checkpoint"] = {key: value for key, value in checkpoint.items() if key != "context_lifecycle"}
    return card


def entity_tag(body: bytes) -> str:
    """Strong validator for an encoded inventory, for If-None-Match."""
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
