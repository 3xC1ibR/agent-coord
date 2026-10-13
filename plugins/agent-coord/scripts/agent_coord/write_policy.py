"""Provider-independent decisions for explicit, pre-execution file writes.

This is a cooperative coordination gate, not a shell or filesystem sandbox.
Adapters supply normalized paths from a vetoable native hook.
"""
from __future__ import annotations

from .store import path_is_in_scope


def check_write(store, session_id, paths, *, has_outside=False):
    """Return a denial reason or None. Preserve the sole-writer fast path."""
    session = store.get_session(session_id)
    if not paths and has_outside:
        return None
    if not session["write_scope"]:
        blockers = store.scope_blocking_peers(session_id)
        if not blockers:
            if session["scope_required"]:
                store.clear_scope_requirement(session_id)
            store.touch(session_id, "implementing", turn_active=True)
            return None
        unscoped = [peer for peer in blockers if not peer["write_scope"]]
        if not session["scope_required"]:
            store.request_scope_declarations(session_id, [peer["session_id"] for peer in unscoped])
        store.touch(session_id, "waiting", turn_active=True)
        owners = ", ".join(peer["name"] or peer["session_id"] for peer in blockers)
        next_action = "Wait for the unscoped incumbent(s) to declare, then run " if unscoped else "Run "
        return (
            "Another active session requires write-scope coordination: "
            f"{owners}. {next_action}agent-coord begin-work --session-id {session_id} "
            "--scope '<path-or-glob>' before editing. --bead is optional. "
            "Unscoped blocking peers have been asked to declare their scopes."
        )
    if session["lease_mode"] == "validation":
        return "This declaration is a validation lease; explicit write tools are disabled until a write lease is acquired."
    if session["activity"] not in {"implementing", "validating"}:
        return f"Session activity is {session['activity']}; set it to implementing or validating before editing."
    if not paths:
        return "The write target could not be determined from the tool input."
    outside = [path for path in paths if not any(path_is_in_scope(path, scope) for scope in session["write_scope"])]
    if outside:
        return "Write target is outside the declared scope: " + ", ".join(outside)
    conflicts = store.check_conflicts(session_id)
    if conflicts:
        owners = ", ".join(conflict["name"] or conflict["session_id"] for conflict in conflicts)
        return f"Declared work conflicts with live session(s): {owners}."
    return None
