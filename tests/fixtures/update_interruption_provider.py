#!/usr/bin/env python3
"""Independent stdio provider for backend-update interruption tests.

Speaks the wire protocols the real Kimi (ACP), Antigravity (ACP) and Muse
(MSP) connections use. It imports nothing from the runtime. Requests and
handled signals are appended to ``provider.jsonl`` under ``RIBBON_UPDATE_FIXTURE_ROOT``.

Prompt text controls the turn:
  HOLD  stays running until cancelled or ``release-<native id>`` exists;
  ASK   additionally requests a permission approval and waits for it;
  other prompts (including continuations) answer immediately.
While a turn holds, ``ask-<native id>`` requests an approval (ACP modes) and
``fail-<native id>`` ends it with a provider failure. ``fail-continuations``
makes automatic continuation prompts fail, as a provider quota error would.
Native session identities persist in ``<mode>-sessions.json`` so a resumed
process proves it reopened the same native conversation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid

ROOT = Path(os.environ["RIBBON_UPDATE_FIXTURE_ROOT"])
MODE = sys.argv[1]
LOCK = threading.RLock()
PROMPTS = {}  # native session -> {"id": request id, "turn": turn id, "cancelled": Event}
APPROVALS = {}  # approval request id -> Event


def record(kind, **values):
    entry = {"kind": kind, "mode": MODE, "pid": os.getpid(), "time": time.time(), **values}
    data = (json.dumps(entry, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(ROOT / "provider.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)


def on_signal(number, _frame):
    record("signal", number=number)
    os._exit(128 + number)


for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    # An ACP process can exit on SIGTERM without emitting a final event.
    handler = signal.SIG_DFL if MODE == "kimi" and number == signal.SIGTERM else on_signal
    signal.signal(number, handler)


def emit(message):
    with LOCK:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        sys.stdout.flush()


def sessions_path():
    return ROOT / f"{MODE}-sessions.json"


def known_sessions():
    path = sessions_path()
    return set(json.loads(path.read_text())) if path.exists() else set()


def remember(native):
    with LOCK:
        values = known_sessions() | {native}
        sessions_path().write_text(json.dumps(sorted(values)))


def prompt_text(parts):
    """Controls come from the user's input, not the coordination preamble."""
    texts = [part.get("text", "") for part in parts if isinstance(part, dict) and part.get("type") == "text"]
    return texts[-1] if texts else ""


def wait_for_end(native, cancelled, ask=None):
    while not cancelled.is_set():
        if (ROOT / f"release-{native}").exists():
            return "released"
        if (ROOT / f"fail-{native}").exists():
            return "failed"
        if ask is not None and (ROOT / f"ask-{native}").exists():
            ask()
            ask = None
        time.sleep(.02)
    return "cancelled"


def outcome_for(text):
    if ("[Automatic continuation after a Ribbon Field backend update]" in text
            and (ROOT / "fail-continuations").exists()):
        return "failed"
    return "answered"


# ---------------------------------------------------------------- ACP modes --

CONFIG = {
    "kimi": [{"id": "model", "category": "model", "type": "select", "currentValue": "k3",
              "options": [{"value": "k3", "name": "K3"}]},
             {"id": "thinking", "category": "thought_level", "type": "select", "currentValue": "high",
              "options": [{"value": "high", "name": "High"}]},
             {"id": "mode", "category": "mode", "type": "select", "currentValue": "default",
              "options": [{"value": "default"}, {"value": "yolo"}]}],
    "antigravity": [{"id": "model", "category": "model", "type": "select", "currentValue": "gemini-test",
                     "options": [{"value": "gemini-test", "name": "Gemini Test"}]},
                    {"id": "mode", "category": "mode", "type": "select", "currentValue": "default",
                     "options": [{"value": "default"}, {"value": "yolo"}]}],
}
AGENT = {"kimi": "Kimi Code CLI", "antigravity": "antigravity-acp"}


def acp_update(native, text):
    emit({"method": "session/update", "params": {"sessionId": native, "update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}})


def acp_prompt(request):
    params = request["params"]
    native = params["sessionId"]
    text = prompt_text(params["prompt"])
    turn = str(uuid.uuid4())
    cancelled = threading.Event()
    with LOCK:
        PROMPTS[native] = {"id": request["id"], "turn": turn, "cancelled": cancelled}
    held = "HOLD" in text or "ASK" in text
    record("prompt", session=native, turn=turn, held=held,
           continuation="[Automatic continuation after a Ribbon Field backend update]" in text,
           text=text[-200:])

    def ask():
        approval = "approval-" + turn
        APPROVALS[approval] = threading.Event()
        record("approval-requested", session=native, turn=turn, id=approval)
        emit({"id": approval, "method": "session/request_permission", "params": {
            "sessionId": native, "toolCall": {"toolCallId": "tool-" + turn, "title": "Run fixture command",
                                              "rawInput": {"command": "touch external-effect"}},
            "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "reject", "name": "Reject", "kind": "reject_once"}]}})
        return APPROVALS[approval]

    def run():
        acp_update(native, "working ")
        if "ASK" in text:
            event = ask()
            while not event.is_set() and not cancelled.is_set():
                time.sleep(.02)
        outcome = wait_for_end(native, cancelled, ask) if held else outcome_for(text)
        with LOCK:
            if PROMPTS.get(native, {}).get("turn") != turn:
                return
            PROMPTS.pop(native, None)
        if outcome == "cancelled":
            record("prompt-ended", session=native, turn=turn, stop_reason="cancelled")
            emit({"id": request["id"], "result": {"stopReason": "cancelled"}})
            return
        if outcome == "failed":
            record("prompt-ended", session=native, turn=turn, stop_reason="refusal")
            emit({"id": request["id"], "result": {"stopReason": "refusal"}})
            return
        acp_update(native, "done")
        record("prompt-ended", session=native, turn=turn, stop_reason="end_turn")
        emit({"id": request["id"], "result": {"stopReason": "end_turn"}})

    threading.Thread(target=run, daemon=True).start()


def acp(request):
    method, params = request.get("method"), request.get("params") or {}
    if "method" not in request:
        approval = APPROVALS.get(request.get("id"))
        record("client-response", id=request.get("id"), result=request.get("result"))
        if approval is not None:
            approval.set()
        return
    record("request", method=method, session=params.get("sessionId"))
    if method == "session/cancel":
        with LOCK:
            prompt = PROMPTS.get(params.get("sessionId"))
        if prompt:
            prompt["cancelled"].set()
        return
    if method == "initialize":
        result = {"protocolVersion": 1, "agentInfo": {"name": AGENT[MODE], "version": "fixture"},
                  "agentCapabilities": {"loadSession": True, "promptCapabilities": {"image": False},
                                        "sessionCapabilities": {"resume": {}}}}
    elif method == "authenticate":
        result = {}
    elif method == "session/new":
        native = "native-" + uuid.uuid4().hex
        remember(native)
        result = {"sessionId": native, "configOptions": CONFIG[MODE]}
    elif method in {"session/resume", "session/load"}:
        if params.get("sessionId") not in known_sessions():
            emit({"id": request["id"], "error": {"code": -32602, "message": "Unknown session"}})
            return
        result = {"sessionId": params["sessionId"], "configOptions": CONFIG[MODE]}
    elif method == "session/set_config_option":
        result = {"configOptions": CONFIG[MODE]}
    elif method == "session/prompt":
        acp_prompt(request)
        return
    else:
        result = {}
    emit({"id": request["id"], "result": result})


# ---------------------------------------------------------------- MSP mode --

def msp_session(native, profile):
    return {"session": {"sessionId": native, "modelId": "muse-large-2", "activeTurnId": None,
                        "permissionState": {"profileId": profile}, "turns": []}, "viewCursor": ""}


def msp_turn(native, turn, text):
    cancelled = threading.Event()
    with LOCK:
        PROMPTS[native] = {"turn": turn, "cancelled": cancelled}
    held = "HOLD" in text
    record("prompt", session=native, turn=turn, held=held,
           continuation="[Automatic continuation after a Ribbon Field backend update]" in text,
           text=text[-200:])

    def run():
        emit({"method": "turn/started", "params": {"sessionId": native, "turnId": turn, "commandId": turn}})
        item = "item-" + turn
        emit({"method": "item/started", "params": {"sessionId": native, "item": {
            "itemId": item, "kind": "agentMessage", "turnId": turn, "status": "inProgress", "text": "working "}}})
        outcome = wait_for_end(native, cancelled) if held else outcome_for(text)
        with LOCK:
            if PROMPTS.get(native, {}).get("turn") != turn:
                return
            PROMPTS.pop(native, None)
        if outcome in {"cancelled", "failed"}:
            terminal = "cancelled" if outcome == "cancelled" else "failed"
            record("prompt-ended", session=native, turn=turn, stop_reason=terminal)
            emit({"method": "turn/completed", "params": {"sessionId": native, "turnId": turn, "terminal": terminal,
                                                          **({"error": {"message": "Fixture quota exhausted"}}
                                                             if terminal == "failed" else {})}})
            return
        emit({"method": "item/completed", "params": {"sessionId": native, "item": {
            "itemId": item, "kind": "agentMessage", "turnId": turn, "status": "completed", "text": "working done"}}})
        record("prompt-ended", session=native, turn=turn, stop_reason="completed")
        emit({"method": "turn/completed", "params": {"sessionId": native, "turnId": turn, "terminal": "completed"}})

    threading.Thread(target=run, daemon=True).start()


def msp(request):
    method, params = request.get("method"), request.get("params") or {}
    if "method" not in request or method == "initialized":
        return
    record("request", method=method, session=params.get("sessionId"))
    if method == "initialize":
        result = {"schema": {"version": 1, "fingerprint": "sha256:4e3200c3c9dc0cd7919d11022630d43f9bc1c5cdda375381e90b0183b5665069"}, "sessionDurability": "durable"}
    elif method == "session/start":
        remember(params["sessionId"])
        result = msp_session(params["sessionId"], params["permissionProfile"])
    elif method == "session/resume":
        if params.get("sessionId") not in known_sessions():
            emit({"id": request["id"], "error": {"code": -32602, "message": "Unknown session"}})
            return
        result = msp_session(params["sessionId"], params["permissionProfile"])
    elif method == "turn/start":
        result = {"commandId": params["commandId"], "status": "accepted", "turnId": params["commandId"],
                  "startedNewTurn": True, "disposition": "started"}
        emit({"id": request["id"], "result": result})
        msp_turn(params["sessionId"], params["commandId"], prompt_text(params["input"]))
        return
    elif method == "turn/interrupt":
        with LOCK:
            prompt = PROMPTS.get(params.get("sessionId"))
        if prompt and prompt["turn"] == params.get("turnId"):
            prompt["cancelled"].set()
        result = {"status": "accepted"}
    else:
        result = {"status": "accepted"}
    emit({"id": request["id"], "result": result})


def main():
    record("start", argv=sys.argv[1:])
    handler = msp if MODE == "muse" else acp
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        handler(request)
    record("eof")


if __name__ == "__main__":
    main()
