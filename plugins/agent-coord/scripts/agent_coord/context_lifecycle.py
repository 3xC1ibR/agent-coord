"""Assignment-bound context advice for orchestration-created app agents."""
from __future__ import annotations

import json

from .organization import app_agent_creators
from .store import CoordinationError

RECOMMENDATIONS = {"reuse", "fresh", "either"}
MAX_ITEMS = 8
MAX_ITEM_CHARS = 400
MAX_REASON_CHARS = 500
MAX_REPORT_CHARS = 6000
HANDOVER_FIELDS = ("confirmed_facts", "constraints", "tried_and_failed", "open_questions")

_LIST_SCHEMA = {"type": "array", "maxItems": MAX_ITEMS,
                "items": {"type": "string", "minLength": 1, "maxLength": MAX_ITEM_CHARS}}
REPORT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["assignment_message_id", "recommendation", "reason", "reuse_for", "fresh_for", "handover"],
    "properties": {
        "assignment_message_id": {"type": "integer", "minimum": 1},
        "recommendation": {"type": "string", "enum": sorted(RECOMMENDATIONS)},
        "reason": {"type": "string", "minLength": 1, "maxLength": MAX_REASON_CHARS},
        "reuse_for": _LIST_SCHEMA,
        "fresh_for": _LIST_SCHEMA,
        "handover": {"type": "object", "additionalProperties": False,
                     "required": list(HANDOVER_FIELDS),
                     "properties": {field: _LIST_SCHEMA for field in HANDOVER_FIELDS}},
    },
}
CHECKPOINT_SCHEMA = {
    "description": ("Context advice for orchestration-created app agents, bound to the assignment message. "
                    "At most 6000 serialized characters. Omission preserves it within this turn; null clears it."),
    "anyOf": [{"type": "null"}, REPORT_SCHEMA],
}

REPORTING_INSTRUCTIONS = (
    "This app agent was created for orchestration. When returning an assignment result, including blocked or "
    "failed work, save context_lifecycle in your normal checkpoint before returning control or sending a "
    "requested result reply. Include assignment_message_id from the delivered request, recommendation "
    "(reuse, fresh, or either), reason (observed evidence), reuse_for and fresh_for (types of follow-up work), "
    "and handover with confirmed_facts, constraints, tried_and_failed, and open_questions arrays. Capture "
    "concrete findings and failed approaches needed by a replacement. Each array accepts up to 8 strings of "
    "400 characters; reason accepts 500 characters, and the complete serialized report accepts 6000 characters. "
    "Use existing checkpoint links for artifacts. Do not invent token counts or noise ratios. Omission "
    "preserves the report only within this turn; null clears it. Automatic completion events and requested "
    "replies carry a matching report snapshot. Do not send a separate assessment message or reply where none "
    "was requested. This advice does not close the conversation or prevent direct user follow-ups and peer coordination."
)


def is_orchestrated(db, session_id):
    creator = app_agent_creators(db).get(session_id)
    return bool(creator and creator["mode"] == "orchestration")


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise CoordinationError(f"{label} must be non-empty text of at most {limit} characters.")
    return value.strip()


def _items(value, label):
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise CoordinationError(f"{label} must be a list of at most {MAX_ITEMS} strings.")
    return [_text(item, label + " item", MAX_ITEM_CHARS) for item in value]


def validate_report(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(REPORT_SCHEMA["required"]):
        raise CoordinationError("context_lifecycle requires assignment_message_id, recommendation, reason, "
                                "reuse_for, fresh_for, and handover.")
    message_id = value["assignment_message_id"]
    if type(message_id) is not int or message_id <= 0:
        raise CoordinationError("context_lifecycle assignment_message_id must be a positive integer.")
    recommendation = value["recommendation"]
    if not isinstance(recommendation, str) or recommendation not in RECOMMENDATIONS:
        raise CoordinationError("context_lifecycle recommendation must be reuse, fresh, or either.")
    handover = value["handover"]
    if not isinstance(handover, dict) or set(handover) != set(HANDOVER_FIELDS):
        raise CoordinationError("context_lifecycle handover requires confirmed_facts, constraints, "
                                "tried_and_failed, and open_questions.")
    result = {
        "assignment_message_id": message_id,
        "recommendation": recommendation,
        "reason": _text(value["reason"], "context_lifecycle reason", MAX_REASON_CHARS),
        "reuse_for": _items(value["reuse_for"], "context_lifecycle reuse_for"),
        "fresh_for": _items(value["fresh_for"], "context_lifecycle fresh_for"),
        "handover": {field: _items(handover[field], "context_lifecycle " + field) for field in HANDOVER_FIELDS},
    }
    if len(encode(result)) > MAX_REPORT_CHARS:
        raise CoordinationError(f"context_lifecycle must fit within {MAX_REPORT_CHARS} serialized characters.")
    return result


def encode(report):
    return json.dumps(report, sort_keys=True, separators=(",", ":")) if report is not None else None


def validate_assignment(db, session_id, report):
    if not is_orchestrated(db, session_id):
        raise CoordinationError("context_lifecycle is only supported for orchestration-created app agents.")
    assignment = db.execute("""SELECT m.*, t.closed_at FROM messages m
        JOIN message_threads t ON t.thread_id = m.thread_id WHERE m.id = ?""",
        (report["assignment_message_id"],)).fetchone()
    if (not assignment or assignment["recipient_session_id"] != session_id
            or assignment["classification"] != "action_required" or assignment["in_reply_to"] is not None
            or assignment["suppressed_at"] is not None or assignment["closed_at"] is not None):
        raise CoordinationError("context_lifecycle must identify an actionable assignment sent to this agent.")


def checkpoint_record(row):
    """Expose the structured report without leaking its storage fields."""
    result = dict(row)
    raw = result.pop("context_lifecycle_json", None)
    result.pop("context_lifecycle_turn_key", None)
    if raw is not None:
        result["context_lifecycle"] = json.loads(raw)
    return result


def snapshot_for_assignment(db, child_session_id, assignment_message_id, turn_id, *, turn_key=None, checkpoint_id=None):
    """Read one exact turn, even if another turn has since started. Never fall back to latest."""
    if not is_orchestrated(db, child_session_id) or (turn_id is None and turn_key is None):
        return None
    row = db.execute("""SELECT * FROM thread_checkpoints WHERE thread_id = ? AND turn_id IS ?
        AND (? IS NULL OR context_lifecycle_turn_key = ?) AND (? IS NULL OR id = ?)
        ORDER BY id DESC LIMIT 1""",
        (child_session_id, turn_id, turn_key, turn_key, checkpoint_id, checkpoint_id)).fetchone()
    if not row or row["context_lifecycle_json"] is None:
        return None
    report = json.loads(row["context_lifecycle_json"])
    if report["assignment_message_id"] != assignment_message_id:
        return None
    return {"checkpoint_id": row["id"], **report}


def reply_snapshot(db, session_id, message_id):
    current = db.execute("SELECT turn_id, turn_key FROM work_threads WHERE thread_id = ?", (session_id,)).fetchone()
    if not current:
        return None
    return encode(snapshot_for_assignment(db, session_id, message_id, current["turn_id"],
                                         turn_key=current["turn_key"]))
