#!/usr/bin/env python3
"""Independent wire-level workload for live-runtime acceptance.

No runtime imports. Work completes only after an explicit test control file or
the original approval reply; coordinator EOF and provider signals are recorded.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(os.environ["RIBBON_FAKE_ROOT"])
MODE = sys.argv[1]
LOCK = threading.RLock()
THREADS = {}
APPROVALS = {}


def record(kind, **values):
    entry = {"kind": kind, "pid": os.getpid(), "mode": MODE,
             "time": time.monotonic(), **values}
    # One append syscall, including when provider and tool processes overlap.
    data = (json.dumps(entry, separators=(",", ":")) + "\n").encode()
    fd = os.open(ROOT / "provider.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.write(fd, data) != len(data):
            raise RuntimeError("short audit write")
    finally:
        os.close(fd)


def signal_received(number, _frame):
    record("signal", number=number)
    raise SystemExit(128 + number)


for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(number, signal_received)


def emit(message):
    with LOCK:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def wait_release():
    while not (ROOT / "release").exists():
        time.sleep(.02)


def event(method, thread, turn_state, **values):
    emit({"method": method, "params": {"threadId": thread,
          "turnId": turn_state["id"], **values}})


def work(thread, turn, role, *, claude=False, input_id=None):
    record("turn", thread=thread, turn=turn["id"], role=role, input_id=input_id)
    output = {"id": "output-" + turn["id"], "type": "agentMessage", "text": ""}
    if claude:
        emit({"type": "system", "subtype": "init", "session_id": thread})
        emit({"type": "stream_event", "event": {"type": "message_start"}})
        emit({"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
              "content_block": {"type": "text", "text": ""}}})
    else:
        event("turn/started", thread, turn, turn=copy.deepcopy(turn))
        event("item/started", thread, turn, item=copy.deepcopy(output))
    if role == "approval":
        request_id = "approval-" + turn["id"]
        approval = APPROVALS[request_id] = threading.Event()
        record("approval", thread=thread, turn=turn["id"], request=request_id)
        if claude:
            emit({"type": "control_request", "request_id": request_id,
                  "request": {"subtype": "can_use_tool", "tool_name": "Bash",
                              "input": {"command": "isolated-fake-tool"}}})
        else:
            emit({"id": request_id, "method": "item/commandExecution/requestApproval",
                  "params": {"threadId": thread, "turnId": turn["id"],
                             "command": "isolated-fake-tool"}})
        approval.wait()  # Never auto-approve or time out to make a swap pass.
    if role in {"tool", "child"}:
        record("tool_start", thread=thread, turn=turn["id"], tool=role)
        if not claude:
            event("item/started", thread, turn, item={"id": "tool-" + turn["id"],
                  "type": "commandExecution", "command": "isolated-fake-tool",
                  "status": "inProgress"})
        child = subprocess.Popen([sys.executable, __file__, "tool", turn["id"]],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        record("tool_pid", thread=thread, turn=turn["id"], child=child.pid)
        result = child.wait()
        record("tool_complete", thread=thread, turn=turn["id"], returncode=result)
        if not claude:
            event("item/completed", thread, turn, item={"id": "tool-" + turn["id"],
                  "type": "commandExecution", "command": "isolated-fake-tool",
                  "status": "completed", "exitCode": result})
    sequence = 0
    while True:
        sequence += 1
        delta = f"[{sequence}]"
        output["text"] += delta
        record("output", thread=thread, turn=turn["id"], sequence=sequence, delta=delta)
        if claude:
            emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                  "delta": {"type": "text_delta", "text": delta}}})
        else:
            event("item/agentMessage/delta", thread, turn, itemId=output["id"], delta=delta)
        if role != "stream" or (ROOT / "release").exists():
            break
        time.sleep(.05)
    turn["items"].append(output)
    turn["status"] = "completed"
    record("complete", thread=thread, turn=turn["id"])
    if claude:
        emit({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}})
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": output["text"]}]}})
        emit({"type": "result", "subtype": "success", "is_error": False,
              "user_message_uuids": [input_id], "result": output["text"]})
    else:
        event("item/completed", thread, turn, item=copy.deepcopy(output))
        event("turn/completed", thread, turn, turn=copy.deepcopy(turn))


def codex(request):
    if request.get("id") in APPROVALS and "result" in request:
        record("approval_answer", request=request["id"], result=request["result"])
        APPROVALS[request["id"]].set()
        return
    if "id" not in request:
        return
    method, params = request.get("method"), request.get("params") or {}
    identity = params.get("threadId")
    result = {}
    if method == "thread/start":
        identity = "fake-" + uuid.uuid4().hex
        THREADS[identity] = {"id": identity, "cwd": params["cwd"], "turns": []}
    if method in {"thread/start", "thread/read", "thread/resume"}:
        result = {"thread": copy.deepcopy(THREADS[identity]), "model": "fake-model", "reasoningEffort": "low"}
    if method == "model/list":
        result = {"data": [{"model": "fake-model", "isDefault": True, "supportedReasoningEfforts": []}]}
    if method == "turn/start":
        role = params["input"][0]["text"]
        turn = {"id": uuid.uuid4().hex, "status": "inProgress", "items": [
            {"id": uuid.uuid4().hex, "type": "userMessage", "content": params["input"]}]}
        THREADS[identity]["turns"].append(turn)
        emit({"id": request["id"], "result": {"turn": copy.deepcopy(turn)}})
        threading.Thread(target=work, args=(identity, turn, role), daemon=True).start()
        return
    emit({"id": request["id"], "result": result})


def claude(request):
    if request.get("type") == "control_response":
        response = request["response"]
        identity = response["request_id"]
        record("approval_answer", request=identity, result=response)
        APPROVALS[identity].set()
    elif request.get("type") == "control_request":
        subtype = request["request"].get("subtype")
        result = {"models": [{"value": "default", "displayName": "Fake Claude"}]}
        if subtype == "get_settings":
            result = {"applied": {"model": "default", "effort": "high"}}
        emit({"type": "control_response", "response": {"subtype": "success",
              "request_id": request["request_id"], "response": result}})
    elif request.get("type") == "user":
        identity = request["session_id"]
        role = request["message"]["content"][0]["text"]
        turn = {"id": request["uuid"], "items": [], "status": "inProgress"}
        threading.Thread(target=work, args=(identity, turn, role),
                         kwargs={"claude": True, "input_id": request["uuid"]}, daemon=True).start()


record("start", argv=sys.argv[2:])
if MODE == "tool":
    wait_release()
    record("tool_exit", turn=sys.argv[2])
else:
    try:
        for line in sys.stdin:
            request = json.loads(line)
            record("input", message=request)
            (codex if MODE == "codex" else claude)(request)
    finally:
        record("stdin_eof")
