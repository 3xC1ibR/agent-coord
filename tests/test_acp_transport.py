"""Stage 2 provider_transport broker, restart attach, replay reconciliation, and 2.0s interrupt tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_coord import history_persistence
from agent_coord.acp import (
    ACPConnection,
    accepts_keyword,
    strip_coordination_preamble,
)
from agent_coord.antigravity import AntigravityConnection, AntigravityRPC
from agent_coord.claude_code import ClaudeConnection, ClaudeRPC
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.coordinator_projection import CoordinatorProjection, ProcessHandle
from agent_coord.history_persistence import read_history
from agent_coord.kimi import KimiConnection, KimiRPC
from agent_coord.muse_code import MuseConnection, MuseRPC
from agent_coord.provider_transport import Client as TransportClient, Owner as TransportOwner
from agent_coord.store import CoordinationStore
from test_codex_app_server import FakeCodex


class _ProjectionFakeCodex(FakeCodex):
    def __init__(self, store, callback):
        super().__init__(store, callback)
        self.next_id = 1
        self.ready = True
        self.initialized_response = True
        self.pending_methods = {}


PROVIDER_FIXTURE_SOURCE = r'''
import json
import os
from pathlib import Path
import sys
import threading
import time

MODE = sys.argv[1]
STATE_PATH = Path(os.environ["STAGE2_FIXTURE_STATE"])
WRITE_LOCK = threading.Lock()
CANCEL_EVENTS = {}


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"sessions": {}, "events": []}


def save_state(state):
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def record_event(entry):
    with WRITE_LOCK:
        state = load_state()
        state.setdefault("events", []).append({**entry, "pid": os.getpid()})
        save_state(state)


def send_frame(payload, *, stream=sys.stdout):
    with WRITE_LOCK:
        stream.write(json.dumps(payload) + "\n")
        stream.flush()


def extract_prompt_text(parts):
    texts = []
    for part in parts or []:
        if isinstance(part, dict) and part.get("type") == "text":
            texts.append(part.get("text", ""))
        elif isinstance(part, str):
            texts.append(part)
    return "\n".join(texts)


def wait_for_gate(gate_name, cancel_event=None):
    gate_path = STATE_PATH.parent / f"gate-{gate_name}.flag"
    ready_path = STATE_PATH.parent / f"ready-{gate_name}.flag"
    ready_temp = ready_path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    ready_temp.write_text(str(os.getpid()), encoding="utf-8")
    ready_temp.replace(ready_path)
    while not gate_path.exists():
        if cancel_event is not None and cancel_event.is_set():
            return False
        time.sleep(0.02)
    return True


def run_acp_provider(provider_name):
    pending_permissions = {}
    permission_counter = 0

    def handle_prompt(req_id, params):
        nonlocal permission_counter
        session_id = params["sessionId"]
        prompt_parts = params.get("prompt", [])
        text = extract_prompt_text(prompt_parts)
        cancel_event = threading.Event()
        CANCEL_EVENTS[session_id] = cancel_event
        record_event({"provider": provider_name, "kind": "prompt", "session_id": session_id, "text": text, "parts": prompt_parts})

        if "EMIT_AUTH_URL_AFTER_START" in text:
            send_frame({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Checking credentials..."}},
                },
            })
            wait_for_gate(f"{provider_name}-auth", cancel_event)
            with WRITE_LOCK:
                sys.stderr.write(
                    "Open the following link to authenticate the ACP server: "
                    "https://accounts.google.com/o/oauth2/v2/auth?client_id=reauth\n"
                )
                sys.stderr.flush()
            return

        if "HOLD_STREAM" in text:
            send_frame({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"{provider_name}-part-1 "}},
                },
            })
            if "ASK_APPROVAL" in text:
                permission_counter += 1
                perm_id = f"perm-{provider_name}-{permission_counter}"
                perm_ready = threading.Event()
                pending_permissions[perm_id] = {"event": perm_ready, "result": None}
                send_frame({
                    "jsonrpc": "2.0",
                    "id": perm_id,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": session_id,
                        "toolCall": {
                            "toolCallId": f"call-{permission_counter}",
                            "title": f"{provider_name} shell command",
                            "rawInput": {"command": "echo approved"},
                        },
                        "options": [
                            {"optionId": "allow-once", "kind": "allow_once", "name": "Allow once"},
                            {"optionId": "reject-once", "kind": "reject_once", "name": "Reject"},
                        ],
                    },
                })
                while not perm_ready.is_set() and not cancel_event.is_set():
                    time.sleep(0.02)
            wait_for_gate(f"{provider_name}-stream", cancel_event)
            if cancel_event.is_set():
                send_frame({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "cancelled"}})
                return
            send_frame({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"{provider_name}-part-2"}},
                },
            })
            send_frame({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
            return

        if "CANCEL_RESPONSIVE" in text:
            send_frame({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Working before cancel"}},
                },
            })
            (STATE_PATH.parent / f"running-{provider_name}-responsive.flag").write_text(str(os.getpid()), encoding="utf-8")
            cancel_event.wait(timeout=10)
            send_frame({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "cancelled"}})
            return

        if "CANCEL_HUNG" in text:
            send_frame({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Hung tool execution"}},
                },
            })
            (STATE_PATH.parent / f"running-{provider_name}-hung.flag").write_text(str(os.getpid()), encoding="utf-8")
            time.sleep(30)
            return

        send_frame({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"{provider_name} reply"}},
            },
        })
        send_frame({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})

    def session_payload(session_id, current_mode="default"):
        return {
            "sessionId": session_id,
            "modes": {
                "currentModeId": current_mode,
                "availableModes": [
                    {"id": "default", "name": "Default"},
                    {"id": "yolo", "name": "YOLO"},
                ],
            },
            "models": {
                "currentModelId": f"{provider_name}-pro",
                "availableModels": [{"modelId": f"{provider_name}-pro", "name": f"{provider_name.title()} Pro"}],
            },
            "configOptions": [
                {
                    "id": "mode",
                    "category": "mode",
                    "currentValue": current_mode,
                    "options": [{"value": "default", "name": "Default"}, {"value": "yolo", "name": "YOLO"}],
                },
                {
                    "id": "model",
                    "category": "model",
                    "currentValue": f"{provider_name}-pro",
                    "options": [{"value": f"{provider_name}-pro", "name": f"{provider_name.title()} Pro"}],
                },
                {
                    "id": "thinking",
                    "category": "thought_level",
                    "currentValue": "medium",
                    "options": [{"value": "medium", "name": "Medium"}],
                },
            ],
        }

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if method is None and req_id in pending_permissions:
            pending_permissions[req_id]["result"] = msg.get("result")
            pending_permissions[req_id]["event"].set()
            record_event({"provider": provider_name, "kind": "permission_answer", "id": req_id, "result": msg.get("result")})
            continue

        if method == "initialize":
            agent_name = "antigravity-acp" if provider_name == "antigravity" else "Kimi Code CLI"
            send_frame({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": 1,
                    "agentInfo": {"name": agent_name, "version": "1.0.0"},
                    "agentCapabilities": {
                        "loadSession": True,
                        "promptCapabilities": {"image": True},
                        "sessionCapabilities": {},
                    },
                },
            })
        elif method == "authenticate":
            with WRITE_LOCK:
                state = load_state()
                emit_oauth = state.get("emit_oauth_on_authenticate", False)
            if emit_oauth:
                with WRITE_LOCK:
                    sys.stderr.write(
                        "Open the following link to authenticate the ACP server: "
                        "https://accounts.google.com/o/oauth2/v2/auth?client_id=initial\n"
                    )
                    sys.stderr.flush()
            send_frame({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif method == "session/new":
            with WRITE_LOCK:
                state = load_state()
                idx = len(state["sessions"]) + 1
                session_id = f"{provider_name}-native-{idx}"
                state["sessions"][session_id] = {"mode": "default", "replay": []}
                save_state(state)
            send_frame({"jsonrpc": "2.0", "id": req_id, "result": session_payload(session_id)})
        elif method == "session/load":
            session_id = params["sessionId"]
            with WRITE_LOCK:
                state = load_state()
                replay = list(state.get("sessions", {}).get(session_id, {}).get("replay", []))
                mode = state.get("sessions", {}).get(session_id, {}).get("mode", "default")
            for update in replay:
                send_frame({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {"sessionId": session_id, "update": update},
                })
            send_frame({"jsonrpc": "2.0", "id": req_id, "result": session_payload(session_id, mode)})
        elif method in {"session/set_config_option", "session/set_mode"}:
            session_id = params["sessionId"]
            value = params.get("value") or params.get("modeId") or "default"
            with WRITE_LOCK:
                state = load_state()
                state.setdefault("sessions", {}).setdefault(session_id, {})
                if params.get("configId") in {None, "mode"}:
                    state["sessions"][session_id]["mode"] = value
                mode = state["sessions"][session_id].get("mode", "default")
                save_state(state)
            send_frame({"jsonrpc": "2.0", "id": req_id, "result": session_payload(session_id, mode)})
        elif method == "session/cancel":
            session_id = params.get("sessionId")
            record_event({"provider": provider_name, "kind": "cancel", "session_id": session_id})
            ev = CANCEL_EVENTS.get(session_id)
            if ev is not None:
                ev.set()
        elif method == "session/prompt":
            threading.Thread(target=handle_prompt, args=(req_id, params), daemon=True).start()
        else:
            send_frame({"jsonrpc": "2.0", "id": req_id, "result": {}})


def run_muse_provider():
    def session_snapshot(session_id, profile=":ask-me"):
        with WRITE_LOCK:
            state = load_state()
            extra = state.get("sessions", {}).get(session_id, {})
        snap = {
            "sessionId": session_id,
            "modelId": "muse-pro-1",
            "permissionState": {"profileId": extra.get("profile", profile)},
            "turns": extra.get("turns_meta", []),
        }
        if "turnCount" in extra:
            snap["turnCount"] = extra["turnCount"]
        return snap

    def stream_muse_turn(session_id, turn_id, text):
        cancel_event = threading.Event()
        CANCEL_EVENTS[turn_id] = cancel_event
        if "WITH_WORKFLOW" in text:
            wf_turn_id = turn_id
            send_frame({
                "jsonrpc": "2.0",
                "method": "turn/started",
                "params": {"sessionId": session_id, "turnId": wf_turn_id, "viewCursor": "c1"},
            })
            send_frame({
                "jsonrpc": "2.0",
                "method": "item/started",
                "params": {
                    "sessionId": session_id,
                    "turnId": wf_turn_id,
                    "viewCursor": "c2",
                    "item": {"itemId": "wf-1", "kind": "workflow", "status": "inProgress", "message": "Running pipeline", "revision": 1},
                },
            })
            send_frame({
                "jsonrpc": "2.0",
                "method": "item/started",
                "params": {
                    "sessionId": session_id,
                    "turnId": wf_turn_id,
                    "viewCursor": "c3",
                    "item": {"itemId": "msg-wf", "kind": "agentMessage", "status": "inProgress", "text": "Workflow launched.", "revision": 1},
                },
            })
            send_frame({
                "jsonrpc": "2.0",
                "method": "item/completed",
                "params": {
                    "sessionId": session_id,
                    "turnId": wf_turn_id,
                    "viewCursor": "c4",
                    "item": {"itemId": "msg-wf", "kind": "agentMessage", "status": "completed", "text": "Workflow launched.", "revision": 2},
                },
            })
            send_frame({
                "jsonrpc": "2.0",
                "method": "turn/completed",
                "params": {"sessionId": session_id, "turnId": wf_turn_id, "terminal": "completed", "viewCursor": "c5"},
            })

            def finish_workflow_later():
                wait_for_gate("muse-workflow")
                send_frame({
                    "jsonrpc": "2.0",
                    "method": "item/completed",
                    "params": {
                        "sessionId": session_id,
                        "turnId": wf_turn_id,
                        "viewCursor": "c20",
                        "item": {"itemId": "wf-1", "kind": "workflow", "status": "completed", "message": "Pipeline done", "revision": 2},
                    },
                })

            threading.Thread(target=finish_workflow_later, daemon=True).start()
            return

        if "HOLD_STREAM" in text:
            send_frame({
                "jsonrpc": "2.0",
                "method": "turn/started",
                "params": {"sessionId": session_id, "turnId": turn_id, "viewCursor": "c10"},
            })
            send_frame({
                "jsonrpc": "2.0",
                "method": "item/started",
                "params": {
                    "sessionId": session_id,
                    "turnId": turn_id,
                    "viewCursor": "c11",
                    "item": {"itemId": "muse-msg-1", "kind": "agentMessage", "status": "inProgress", "text": "", "revision": 1},
                },
            })
            send_frame({
                "jsonrpc": "2.0",
                "method": "item/delta",
                "params": {
                    "sessionId": session_id,
                    "turnId": turn_id,
                    "itemId": "muse-msg-1",
                    "viewCursor": "c12",
                    "field": "text",
                    "delta": "muse-part-1 ",
                },
            })
            wait_for_gate("muse-stream", cancel_event)
            if cancel_event.is_set():
                send_frame({
                    "jsonrpc": "2.0",
                    "method": "turn/completed",
                    "params": {"sessionId": session_id, "turnId": turn_id, "terminal": "cancelled", "viewCursor": "c13"},
                })
                return
            send_frame({
                "jsonrpc": "2.0",
                "method": "item/delta",
                "params": {
                    "sessionId": session_id,
                    "turnId": turn_id,
                    "itemId": "muse-msg-1",
                    "viewCursor": "c14",
                    "field": "text",
                    "delta": "muse-part-2",
                },
            })
            send_frame({
                "jsonrpc": "2.0",
                "method": "item/completed",
                "params": {
                    "sessionId": session_id,
                    "turnId": turn_id,
                    "viewCursor": "c15",
                    "item": {"itemId": "muse-msg-1", "kind": "agentMessage", "status": "completed", "text": "muse-part-1 muse-part-2", "revision": 2},
                },
            })
            send_frame({
                "jsonrpc": "2.0",
                "method": "turn/completed",
                "params": {"sessionId": session_id, "turnId": turn_id, "terminal": "completed", "viewCursor": "c16"},
            })
            return

        if "CANCEL_RESPONSIVE" in text:
            send_frame({
                "jsonrpc": "2.0",
                "method": "turn/started",
                "params": {"sessionId": session_id, "turnId": turn_id, "viewCursor": "r1"},
            })
            (STATE_PATH.parent / "running-muse-responsive.flag").write_text(str(os.getpid()), encoding="utf-8")
            cancel_event.wait(timeout=10)
            send_frame({
                "jsonrpc": "2.0",
                "method": "turn/completed",
                "params": {"sessionId": session_id, "turnId": turn_id, "terminal": "cancelled", "viewCursor": "r2"},
            })
            return

        if "CANCEL_HUNG" in text:
            send_frame({
                "jsonrpc": "2.0",
                "method": "turn/started",
                "params": {"sessionId": session_id, "turnId": turn_id, "viewCursor": "h1"},
            })
            (STATE_PATH.parent / "running-muse-hung.flag").write_text(str(os.getpid()), encoding="utf-8")
            time.sleep(30)
            return

        send_frame({
            "jsonrpc": "2.0",
            "method": "turn/started",
            "params": {"sessionId": session_id, "turnId": turn_id, "viewCursor": "d1"},
        })
        send_frame({
            "jsonrpc": "2.0",
            "method": "item/completed",
            "params": {
                "sessionId": session_id,
                "turnId": turn_id,
                "viewCursor": "d2",
                "item": {"itemId": "m-default", "kind": "agentMessage", "status": "completed", "text": "muse reply", "revision": 1},
            },
        })
        send_frame({
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": {"sessionId": session_id, "turnId": turn_id, "terminal": "completed", "viewCursor": "d3"},
        })

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            send_frame({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "schema": {"version": 1, "fingerprint": "test-fingerprint"},
                    "sessionDurability": "durable",
                    "permissionCatalog": {
                        "defaultProfileId": ":ask-me",
                        "profiles": [
                            {"id": ":ask-me", "label": "Ask me", "description": "Prompt before writes", "available": True},
                            {"id": ":autonomous", "label": "Autonomous", "description": "Full access", "available": True},
                        ],
                    },
                },
            })
        elif method == "initialized":
            continue
        elif method == "model/list":
            send_frame({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "models": [{
                        "modelId": "muse-pro-1",
                        "displayLabel": "Muse Pro 1",
                        "isDefault": True,
                        "defaultReasoningEffort": "medium",
                        "variants": ["medium"],
                    }],
                },
            })
        elif method == "session/start":
            profile = params.get("permissionProfile") or ":ask-me"
            session_id = params.get("sessionId") or f"muse-native-{len(load_state()['sessions']) + 1}"
            with WRITE_LOCK:
                state = load_state()
                state["sessions"][session_id] = {"profile": profile}
                save_state(state)
            send_frame({"jsonrpc": "2.0", "id": req_id, "result": {"session": session_snapshot(session_id, profile)}})
        elif method == "session/resume":
            session_id = params["sessionId"]
            profile = params.get("permissionProfile") or ":ask-me"
            with WRITE_LOCK:
                state = load_state()
                session_data = state.setdefault("sessions", {}).setdefault(session_id, {})
                session_data["profile"] = profile
                replay_events = list(session_data.get("replay_events", []))
                resume_turns = list(session_data.get("resume_turns", []))
                history_meta = dict(session_data.get("history_meta") or {})
                save_state(state)
            for event in replay_events:
                send_frame({"jsonrpc": "2.0", "method": event["method"], "params": {"sessionId": session_id, **event["params"]}})
            snap = session_snapshot(session_id, profile)
            if resume_turns:
                snap["turns"] = resume_turns
            res_payload = {"session": snap}
            if history_meta:
                res_payload["history"] = history_meta
            send_frame({"jsonrpc": "2.0", "id": req_id, "result": res_payload})
        elif method == "session/read":
            session_id = params["sessionId"]
            send_frame({"jsonrpc": "2.0", "id": req_id, "result": {"session": session_snapshot(session_id)}})
        elif method == "view/page":
            session_id = params["sessionId"]
            cursor_key = params.get("cursor") or "initial"
            with WRITE_LOCK:
                state = load_state()
                pages = state.get("sessions", {}).get(session_id, {}).get("view_pages", {})
            page = pages.get(cursor_key, {"events": [], "nextCursor": None, "hasMore": False})
            send_frame({"jsonrpc": "2.0", "id": req_id, "result": page})
        elif method == "turn/start":
            session_id = params["sessionId"]
            turn_id = params["commandId"]
            text = extract_prompt_text(params.get("input", []))
            record_event({"provider": "muse", "kind": "prompt", "session_id": session_id, "turn_id": turn_id, "text": text})
            send_frame({"jsonrpc": "2.0", "id": req_id, "result": {"turnId": turn_id, "disposition": "started"}})
            threading.Thread(target=stream_muse_turn, args=(session_id, turn_id, text), daemon=True).start()
        elif method == "turn/interrupt":
            turn_id = params.get("turnId")
            record_event({"provider": "muse", "kind": "cancel", "turn_id": turn_id})
            if "hung" in str(turn_id) or (STATE_PATH.parent / "running-muse-hung.flag").exists():
                time.sleep(10)
                continue
            ev = CANCEL_EVENTS.get(turn_id)
            if ev is not None:
                ev.set()
            send_frame({"jsonrpc": "2.0", "id": req_id, "result": {"status": "interrupted"}})
        else:
            send_frame({"jsonrpc": "2.0", "id": req_id, "result": {}})


def run_claude_provider():
    if sys.argv[-1] == "--append-system-prompt-file":
        sys.exit(0)
    interrupt_event = threading.Event()

    def stream_claude_turn(user_uuid, text):
        interrupt_event.clear()
        record_event({"provider": "claude", "kind": "prompt", "text": text})
        if "HOLD_STREAM" in text:
            send_frame({
                "type": "stream_event",
                "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            })
            send_frame({
                "type": "stream_event",
                "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "claude-part-1 "}},
            })
            wait_for_gate("claude-stream", interrupt_event)
            if interrupt_event.is_set():
                send_frame({"type": "result", "subtype": "error_during_execution", "user_message_uuid": user_uuid})
                return
            send_frame({
                "type": "stream_event",
                "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "claude-part-2"}},
            })
            send_frame({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}})
            send_frame({"type": "result", "subtype": "success", "user_message_uuid": user_uuid})
            return

        if "CANCEL_RESPONSIVE" in text:
            send_frame({
                "type": "stream_event",
                "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "Working"}},
            })
            (STATE_PATH.parent / "running-claude-responsive.flag").write_text(str(os.getpid()), encoding="utf-8")
            interrupt_event.wait(timeout=10)
            send_frame({"type": "result", "subtype": "success", "user_message_uuid": user_uuid})
            return

        if "CANCEL_HUNG" in text:
            (STATE_PATH.parent / "running-claude-hung.flag").write_text(str(os.getpid()), encoding="utf-8")
            time.sleep(30)
            return

        send_frame({"type": "assistant", "message": {"content": [{"type": "text", "text": "claude reply"}]}})
        send_frame({"type": "result", "subtype": "success", "user_message_uuid": user_uuid})

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("type") == "control_request":
            subtype = msg["request"]["subtype"]
            if subtype == "initialize":
                resp = {
                    "models": [{"value": "default", "resolvedModel": "claude-sonnet-4-6", "supportedEffortLevels": ["medium"]}],
                    "commands": [],
                }
                send_frame({"type": "control_response", "response": {"subtype": "success", "request_id": msg["request_id"], "response": resp}})
            elif subtype == "get_settings":
                send_frame({
                    "type": "control_response",
                    "response": {"subtype": "success", "request_id": msg["request_id"], "response": {"applied": {"model": "claude-sonnet-4-6", "effort": "medium"}}},
                })
            elif subtype == "interrupt":
                record_event({"provider": "claude", "kind": "cancel"})
                if (STATE_PATH.parent / "running-claude-hung.flag").exists():
                    continue
                interrupt_event.set()
                send_frame({"type": "control_response", "response": {"subtype": "success", "request_id": msg["request_id"], "response": {}}})
            else:
                send_frame({"type": "control_response", "response": {"subtype": "success", "request_id": msg["request_id"], "response": {}}})
        elif msg.get("type") == "user":
            content = msg.get("message", {}).get("content", [])
            text = extract_prompt_text(content if isinstance(content, list) else [{"type": "text", "text": str(content)}])
            threading.Thread(target=stream_claude_turn, args=(msg.get("uuid"), text), daemon=True).start()


if MODE in {"antigravity", "kimi"}:
    run_acp_provider(MODE)
elif MODE == "muse":
    run_muse_provider()
elif MODE == "claude":
    run_claude_provider()
'''


class _Stage2ProjectionAdapter(CoordinatorProjection):
    """Test projection bridge matching Track B's pinned stderr_callback + attach_channel contract."""

    def attach(self, channel, pid, callback, *, stderr_callback=None, provider, thread_id=None):
        process = super().attach(channel, pid, callback, provider=provider, thread_id=thread_id)
        self.channels[channel]["stderr_callback"] = stderr_callback
        return process

    def open(self, channel, args, env, cwd, callback, *, stderr_callback=None, provider, thread_id=None):
        identity = "open:" + channel
        value = {"args": args, "env": env, "cwd": cwd}
        role = "history" if provider == "codex" and getattr(callback, "__self__", None) is not self.sessions.rpc else "main"
        with self.lock:
            prepared, completions, encoded = self.prepare_checkpoint()
            value_json = json.dumps(value)
            with self.store.database_operation("coordinator.open"), self.store._connection() as db:
                self.publish_histories(db, prepared)
                self._save(db, encoded, self.cursor)
                db.execute(
                    "INSERT INTO coordinator_channels(channel,provider,thread_id,role) VALUES (?,?,?,?)",
                    (channel, provider, thread_id, role),
                )
                db.execute(
                    "INSERT INTO coordinator_commands VALUES (?,?,?,?, 'pending',NULL)",
                    (identity, channel, "open", value_json),
                )
            self.remember_histories(prepared, completions)
        result = self.deliver(identity, channel, "open", value)
        return self.attach(
            channel,
            result["pid"],
            callback,
            stderr_callback=stderr_callback,
            provider=provider,
            thread_id=thread_id,
        )

    def project_batch(self, events):
        if not events:
            return
        for event in events:
            if event["stream"] == "stderr":
                entry = self.channels.get(event["channel"])
                stderr_cb = entry.get("stderr_callback") if entry else None
                if stderr_cb is not None:
                    stderr_cb(event["data"])
        super().project_batch(events)

    def load(self):
        with self.store.reading(), self.store._connection() as db:
            row = db.execute("SELECT * FROM coordinator_projection WHERE singleton=1").fetchone()
            acp_rows = (
                db.execute(
                    "SELECT channel, provider, thread_id, pid, closed FROM coordinator_channels WHERE provider IN ('antigravity','kimi','muse')"
                ).fetchall()
                if row is not None
                else []
            )
        acp_channels = {r["channel"]: dict(r) for r in acp_rows}
        if acp_channels:
            with self.store._connection() as db:
                db.execute("DELETE FROM coordinator_channels WHERE provider IN ('antigravity','kimi','muse')")
        try:
            super().load()
        finally:
            if acp_channels:
                with self.store._connection() as db:
                    for item in acp_channels.values():
                        db.execute(
                            "INSERT OR REPLACE INTO coordinator_channels(channel,provider,thread_id,role,pid,closed) VALUES (?,?,?,'main',?,?)",
                            (item["channel"], item["provider"], item["thread_id"], item["pid"], item["closed"]),
                        )
        for channel_name in list(self.channels):
            if channel_name in acp_channels:
                self.channels.pop(channel_name, None)
                for tid, conn in list(self.sessions.claude.connections.items()):
                    if getattr(conn, "channel", None) == channel_name:
                        self.sessions.claude.connections.pop(tid, None)
        for channel_name, item in acp_channels.items():
            provider = item["provider"]
            pid = item["pid"]
            if pid is None:
                continue
            if item["closed"]:
                proc = self.attach(channel_name, pid, lambda _: None, provider=provider, thread_id=item["thread_id"])
                proc.returncode = 0
                self.channels[channel_name]["closed"] = True
                continue
            adapter = {
                "antigravity": self.sessions.antigravity,
                "kimi": self.sessions.kimi,
                "muse": self.sessions.muse,
            }[provider]
            adapter.attach_channel(item["thread_id"], channel_name, pid, returncode=None)


PROJECTION_CLASS = (
    CoordinatorProjection
    if accepts_keyword(CoordinatorProjection.open, "stderr_callback")
    else _Stage2ProjectionAdapter
)


class Stage2BrokerTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.fixture_script = self.root / "provider_fixture.py"
        self.fixture_script.write_text(PROVIDER_FIXTURE_SOURCE, encoding="utf-8")
        self.state_path = self.root / "fixture_state.json"
        self.state_path.write_text(json.dumps({"sessions": {}, "events": []}), encoding="utf-8")

        env_patch = patch.dict(
            os.environ,
            {
                "STAGE2_FIXTURE_STATE": str(self.state_path),
                "AGENT_COORD_ZELLIJ_WAKE": "0",
                "STOIC_ANTIGRAVITY_SYNC_STATE_DIR": str(self.root / "stoic-sync"),
            },
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

        self.transport_dir = self.root / "provider-transport"
        self.transport_dir.mkdir(parents=True, mode=0o700)
        self.owner = TransportOwner(self.transport_dir)
        self.owner_thread = threading.Thread(target=self.owner.serve, name="test-transport-owner", daemon=True)
        self.owner_thread.start()
        self.addCleanup(self._stop_transport_owner)
        self._wait_until(lambda: (self.transport_dir / "owner.json").exists())

        self.database = self.root / "state.sqlite3"
        self.active_coordinators = []

    def _stop_transport_owner(self):
        for entry in reversed(self.active_coordinators):
            self._close_coordinator(entry, handover=False)
        self.active_coordinators.clear()
        try:
            record_path = self.transport_dir / "owner.json"
            if record_path.exists():
                client = TransportClient.connect(self.transport_dir, "test-shutdown")
                client.call("stop")
        except Exception:
            self.owner.stopping.set()
            self.owner.journal.stop_waits()
        self.owner_thread.join(timeout=10)

    def _fixture_command(self, mode: str) -> list[str]:
        return [sys.executable, "-u", str(self.fixture_script), mode]

    def _make_coordinator(self, name: str, *, load_existing: bool = False):
        store = CoordinationStore(self.database)
        client = TransportClient.connect(self.transport_dir, name)

        def ag_conn_factory(st, cb, opts):
            return AntigravityConnection(st, cb, opts, command=self._fixture_command("antigravity"))

        ag_conn_factory.attach = AntigravityConnection.attach

        def kimi_conn_factory(st, cb, opts):
            return KimiConnection(st, cb, opts, command=self._fixture_command("kimi"))

        kimi_conn_factory.attach = KimiConnection.attach

        def muse_conn_factory(st, cb, opts):
            return MuseConnection(st, cb, opts, command=self._fixture_command("muse"))

        muse_conn_factory.attach = MuseConnection.attach

        def claude_conn_factory(st, cb, opts):
            return ClaudeConnection(st, cb, opts, command=self._fixture_command("claude"))

        claude_conn_factory.attach = ClaudeConnection.attach

        sessions = BrowserSessions(
            store,
            str(self.workspace),
            rpc_factory=_ProjectionFakeCodex,
            claude_factory=lambda st, cb: ClaudeRPC(st, cb, connection_factory=claude_conn_factory),
            antigravity_factory=lambda st, cb: AntigravityRPC(st, cb, connection_factory=ag_conn_factory),
            kimi_factory=lambda st, cb: KimiRPC(st, cb, connection_factory=kimi_conn_factory),
            muse_factory=lambda st, cb: MuseRPC(st, cb, connection_factory=muse_conn_factory),
        )
        projection = PROJECTION_CLASS(sessions, client)
        store.provider_projection = projection
        sessions.projection = projection
        if load_existing:
            projection.load()
        projection.start()
        bundle = {"store": store, "sessions": sessions, "projection": projection, "client": client}
        self.active_coordinators.append(bundle)
        return bundle

    def _close_coordinator(self, bundle, *, handover: bool):
        if bundle.get("_closed"):
            return
        bundle["_closed"] = True
        sessions = bundle["sessions"]
        projection = bundle["projection"]
        store = bundle["store"]
        sessions.transport_handover = handover
        if handover:
            projection.freeze()
        else:
            projection.stopped.set()
            projection.command_delivered.set()
            if projection.worker is not None:
                projection.worker.join(timeout=5)
        sessions.close()
        store.provider_projection = None

    def _release_gate(self, gate_name: str) -> None:
        (self.root / f"gate-{gate_name}.flag").write_text("go", encoding="utf-8")

    def _wait_gate_ready(self, gate_name: str, timeout: float = 10.0) -> int:
        ready = self.root / f"ready-{gate_name}.flag"
        self._wait_until(ready.exists, timeout=timeout)
        return int(ready.read_text(encoding="utf-8").strip())

    def _wait_until(self, predicate, timeout: float = 12.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        self.fail("Timed out waiting for condition.")

    def _fixture_events(self, *, provider: str | None = None, kind: str | None = None) -> list[dict]:
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        events = data.get("events", [])
        if provider is not None:
            events = [e for e in events if e.get("provider") == provider]
        if kind is not None:
            events = [e for e in events if e.get("kind") == kind]
        return events

    def test_unmodified_projection_signature_compatibility_for_acp_connection(self):
        store = CoordinationStore(self.root / "compat.sqlite3")
        client = TransportClient.connect(self.transport_dir, "compat-coord")
        sessions = BrowserSessions(store, str(self.workspace), rpc_factory=_ProjectionFakeCodex)
        self.addCleanup(sessions.close)
        projection = CoordinatorProjection(sessions, client)
        store.provider_projection = projection
        projection.start()
        self.addCleanup(lambda: (projection.stopped.set(), projection.worker.join(timeout=5)))

        received = []
        conn = ACPConnection(
            received.append,
            command=self._fixture_command("kimi"),
            cwd=str(self.workspace),
            env=dict(os.environ),
            store=store,
            provider="kimi",
            thread_id="compat-thread",
        )
        self.addCleanup(conn.close)
        self.assertIsInstance(conn.process, ProcessHandle)
        init_res = conn.request("initialize", {"protocolVersion": 1}, timeout=5)
        self.assertEqual(init_res["protocolVersion"], 1)

    def test_mid_stream_handover_preserves_pids_approvals_workflows_and_completions(self):
        coord1 = self._make_coordinator("coord-1")
        s1 = coord1["sessions"]

        ag_thread = s1.create({"client": "antigravity", "cwd": str(self.workspace)})["session"]["thread_id"]
        kimi_thread = s1.create({"client": "kimi", "cwd": str(self.workspace)})["session"]["thread_id"]
        muse_thread = s1.create({"client": "muse", "cwd": str(self.workspace)})["session"]["thread_id"]
        claude_thread = s1.create({"client": "claude", "cwd": str(self.workspace)})["session"]["thread_id"]

        # Start a Muse turn that leaves an in-progress background workflow running after turn completion.
        s1.send(muse_thread, {"message": "Run WITH_WORKFLOW first"})
        self._wait_until(lambda: not s1.read(muse_thread)["running"] and s1.muse.background_active(muse_thread))

        # Start concurrent mid-stream foreground turns across all 4 subprocess providers, plus Kimi approval.
        s1.send(ag_thread, {"message": "HOLD_STREAM antigravity"})
        s1.send(kimi_thread, {"message": "HOLD_STREAM ASK_APPROVAL kimi"})
        s1.send(muse_thread, {"message": "HOLD_STREAM muse"})
        s1.send(claude_thread, {"message": "HOLD_STREAM claude"})

        ag_pid = self._wait_gate_ready("antigravity-stream")
        muse_pid = self._wait_gate_ready("muse-stream")
        claude_pid = self._wait_gate_ready("claude-stream")
        self._wait_until(lambda: len(s1.pending_requests(kimi_thread)) == 1)
        kimi_pid = s1.kimi.connections[kimi_thread].process.pid

        self.assertEqual(s1.antigravity.connections[ag_thread].process.pid, ag_pid)
        self.assertEqual(s1.muse.connections[muse_thread].process.pid, muse_pid)
        self.assertEqual(s1.claude.connections[claude_thread].process.pid, claude_pid)

        # Handover from Coordinator 1 to Coordinator 2 without killing any child process!
        self._close_coordinator(coord1, handover=True)
        os.kill(ag_pid, 0)
        os.kill(kimi_pid, 0)
        os.kill(muse_pid, 0)
        os.kill(claude_pid, 0)

        coord2 = self._make_coordinator("coord-2", load_existing=True)
        s2 = coord2["sessions"]

        self.assertEqual(s2.antigravity.connections[ag_thread].process.pid, ag_pid)
        self.assertEqual(s2.kimi.connections[kimi_thread].process.pid, kimi_pid)
        self.assertEqual(s2.muse.connections[muse_thread].process.pid, muse_pid)
        self.assertEqual(s2.claude.connections[claude_thread].process.pid, claude_pid)
        self.assertTrue(s2.muse.background_active(muse_thread))

        # Answer the pending Kimi approval on Coordinator 2!
        pending = s2.pending_requests(kimi_thread)
        self.assertEqual(len(pending), 1)
        s2.answer(kimi_thread, pending[0]["key"], {"decision": "accept"})
        self._wait_gate_ready("kimi-stream")

        # Release all streaming gates and background workflow gate.
        self._release_gate("antigravity-stream")
        self._release_gate("kimi-stream")
        self._release_gate("muse-stream")
        self._release_gate("claude-stream")
        self._release_gate("muse-workflow")

        for tid in (ag_thread, kimi_thread, muse_thread, claude_thread):
            self._wait_until(lambda t=tid: not s2.read(t)["running"])
        self._wait_until(lambda: not s2.muse.background_active(muse_thread))
        self.assertTrue(s2.persistence.flush())

        ag_turns = s2.read(ag_thread)["thread"]["turns"]
        self.assertEqual(ag_turns[-1]["status"], "completed")
        ag_text = "".join(i.get("text", "") for i in ag_turns[-1]["items"] if i["type"] == "agentMessage")
        self.assertEqual(ag_text, "antigravity-part-1 antigravity-part-2")

        kimi_turns = s2.read(kimi_thread)["thread"]["turns"]
        self.assertEqual(kimi_turns[-1]["status"], "completed")
        kimi_text = "".join(i.get("text", "") for i in kimi_turns[-1]["items"] if i["type"] == "agentMessage")
        self.assertEqual(kimi_text, "kimi-part-1 kimi-part-2")

        muse_turns = s2.read(muse_thread)["thread"]["turns"]
        self.assertEqual(len(muse_turns), 2)
        wf_items = [i for i in muse_turns[0]["items"] if i.get("nativeKind") == "workflow"]
        self.assertEqual(wf_items[0]["status"], "completed")
        self.assertEqual(muse_turns[1]["status"], "completed")
        muse_text = "".join(i.get("text", "") for i in muse_turns[1]["items"] if i["type"] == "agentMessage")
        self.assertEqual(muse_text, "muse-part-1 muse-part-2")

        claude_turns = s2.read(claude_thread)["thread"]["turns"]
        self.assertEqual(claude_turns[-1]["status"], "completed")
        claude_text = "".join(i.get("text", "") for i in claude_turns[-1]["items"] if i["type"] == "agentMessage")
        self.assertEqual(claude_text, "claude-part-1 claude-part-2")

    def test_antigravity_turn1_post_restart_lazy_skills_and_reattached_stderr_auth_failure(self):
        # Verify initial stderr OAuth URL scraping over provider_transport.
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["emit_oauth_on_authenticate"] = True
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

        seen_urls = []
        coord1 = self._make_coordinator("ag-coord-1")
        s1 = coord1["sessions"]
        original_factory = s1.antigravity.connection_factory

        def capturing_factory(st, cb, opts):
            return original_factory(st, cb, {**opts, "authorize": lambda url: seen_urls.append(url)})

        capturing_factory.attach = original_factory.attach
        s1.antigravity.connection_factory = capturing_factory

        skill_dir = self.workspace / ".agents/skills/sample-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: sample-skill\ndescription: Demo skill\n---\nInstructions\n")

        ag_thread = s1.create({"client": "antigravity", "cwd": str(self.workspace)})["session"]["thread_id"]
        initial_pid = s1.antigravity.connections[ag_thread].process.pid
        self._wait_until(lambda: any("client_id=initial" in u for u in seen_urls))

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["emit_oauth_on_authenticate"] = False
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

        # Restart coordinator BEFORE Turn 1 is sent!
        self._close_coordinator(coord1, handover=True)
        coord2 = self._make_coordinator("ag-coord-2", load_existing=True)
        s2 = coord2["sessions"]
        self.assertNotIn(ag_thread, s2.antigravity.skill_contexts)
        self.assertEqual(s2.antigravity.connections[ag_thread].process.pid, initial_pid)

        # Send Turn 1 after restart: lazy prepare_skills must succeed without KeyError and reuse the same PID.
        s2.send(ag_thread, {"message": "First turn after coordinator restart"})
        self._wait_until(lambda: not s2.read(ag_thread)["running"])
        self.assertEqual(s2.antigravity.connections[ag_thread].process.pid, initial_pid)
        prompts = self._fixture_events(provider="antigravity", kind="prompt")
        self.assertEqual(len(prompts), 1)
        self.assertIn("This session is registered with agent-coord", prompts[0]["text"])
        self.assertIn("sample-skill", prompts[0]["text"])

        # Now start Turn 2, handover mid-turn (so self.pending has no authenticate waiter), and emit OAuth URL on stderr!
        s2.send(ag_thread, {"message": "EMIT_AUTH_URL_AFTER_START"})
        self._wait_gate_ready("antigravity-auth")
        self._close_coordinator(coord2, handover=True)

        coord3 = self._make_coordinator("ag-coord-3", load_existing=True)
        s3 = coord3["sessions"]
        self.assertTrue(s3.read(ag_thread)["running"])
        self._release_gate("antigravity-auth")

        # Active turn must fail cleanly instead of hanging inProgress!
        self._wait_until(lambda: not s3.read(ag_thread)["running"])
        turns = s3.read(ag_thread)["thread"]["turns"]
        self.assertEqual(turns[-1]["status"], "failed")
        self.assertIn("Sign in to Antigravity", turns[-1]["error"]["message"])

    def test_native_session_load_reconciles_missing_cli_turns_images_and_strips_bootstrap(self):
        coord = self._make_coordinator("replay-coord")
        sessions = coord["sessions"]
        store = coord["store"]

        ag_thread = sessions.create({"client": "antigravity", "cwd": str(self.workspace)})["session"]["thread_id"]
        kimi_thread = sessions.create({"client": "kimi", "cwd": str(self.workspace)})["session"]["thread_id"]
        muse_thread = sessions.create({"client": "muse", "cwd": str(self.workspace)})["session"]["thread_id"]

        # Run Turn 1 on Muse so muse_sessions has a native_id.
        sessions.send(muse_thread, {"message": "Initial Muse UI turn"})
        self._wait_until(lambda: not sessions.read(muse_thread)["running"])

        ag_native = sessions.antigravity.native[ag_thread]
        kimi_native = sessions.kimi.native[kimi_thread]
        muse_native = sessions.muse.native[muse_thread]

        # Close active connections to force session/load and session/resume on next send.
        sessions.antigravity.request("thread/archive", {"threadId": ag_thread})
        sessions.kimi.request("thread/archive", {"threadId": kimi_thread})
        sessions.muse.request("thread/archive", {"threadId": muse_thread})

        bootstrap_text = (
            f"This session is registered with agent-coord as `{ag_thread}`.\n"
            f'Thread context (saved data): {{"session_id": "{ag_thread}", "title": "Bootstrap"}}\n\n'
            "Read a named skill from its listed directory before acting. Resolve its resources relative to SKILL.md.\n"
            "Directory: /tmp/skills\n"
            "- demo (/tmp/skills/demo/SKILL.md)\n\n"
            "Recovered CLI prompt"
        )
        self.assertEqual(strip_coordination_preamble(bootstrap_text), "Recovered CLI prompt")

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        # Antigravity replay: Turn 1 with bootstrap + skill header, Turn 2 image-leading, Turn 3 image-only!
        state["sessions"][ag_native]["replay"] = [
            {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": bootstrap_text}},
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Recovered CLI answer"}},
            {"sessionUpdate": "user_message_chunk", "content": {"type": "image", "mimeType": "image/png", "data": "aGVsbG8="}},
            {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "Inspect screenshot"}},
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Screenshot analyzed"}},
            {"sessionUpdate": "user_message_chunk", "content": {"type": "image", "url": "data:image/png;base64,d29ybGQ="}},
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Image-only response"}},
        ]
        # Kimi replay: CLI turn run outside UI.
        state["sessions"][kimi_native]["replay"] = [
            {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "Kimi terminal prompt"}},
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Kimi terminal answer"}},
        ]
        # Muse replay: Turn 1 (matching saved UI turn) + history mode=none + paginated Turn 2 via view/page!
        state["sessions"][muse_native]["turnCount"] = 2
        state["sessions"][muse_native]["history_meta"] = {"mode": "none"}
        state["sessions"][muse_native]["view_pages"] = {
            "initial": {
                "events": [
                    {"method": "turn/started", "params": {"turnId": "cli-muse-turn-2", "viewCursor": "g1"}},
                    {
                        "method": "item/completed",
                        "params": {
                            "turnId": "cli-muse-turn-2",
                            "viewCursor": "g2",
                            "item": {"itemId": "u-gap", "kind": "userMessage", "status": "completed", "content": [{"type": "text", "text": "Muse terminal turn after gap"}], "revision": 1},
                        },
                    },
                    {
                        "method": "item/completed",
                        "params": {
                            "turnId": "cli-muse-turn-2",
                            "viewCursor": "g3",
                            "item": {"itemId": "a-gap", "kind": "agentMessage", "status": "completed", "text": "Muse gap answer", "revision": 1},
                        },
                    },
                    {"method": "turn/completed", "params": {"turnId": "cli-muse-turn-2", "terminal": "completed", "viewCursor": "g4"}},
                ],
                "nextCursor": None,
                "hasMore": False,
            }
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

        # Verify read_history is called with sessions.lock NOT held by the current thread during reconciliation!
        lock_states = []
        real_read_history = history_persistence.read_history
        lock_owner = [None]
        orig_lock = sessions.lock

        class TrackedLock:
            def acquire(self, *args, **kwargs):
                res = orig_lock.acquire(*args, **kwargs)
                if res:
                    lock_owner[0] = threading.get_ident()
                return res

            def release(self):
                lock_owner[0] = None
                return orig_lock.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *exc):
                self.release()

        sessions.lock = TrackedLock()

        def checked_read_history(st, tid):
            lock_states.append(lock_owner[0] == threading.get_ident())
            return real_read_history(st, tid)

        with patch("agent_coord.acp.read_history", side_effect=checked_read_history):
            sessions.send(ag_thread, {"message": "Follow-up UI turn"})
            self._wait_until(lambda: not sessions.read(ag_thread)["running"])
            sessions.send(kimi_thread, {"message": "Follow-up Kimi UI turn"})
            self._wait_until(lambda: not sessions.read(kimi_thread)["running"])
            sessions.send(muse_thread, {"message": "Follow-up Muse UI turn"})
            self._wait_until(lambda: not sessions.read(muse_thread)["running"])

        sessions.lock = orig_lock
        self.assertTrue(lock_states)
        self.assertFalse(any(lock_states), "read_history must never run while holding BrowserSessions.lock")

        ag_turns = sessions.read(ag_thread)["thread"]["turns"]
        self.assertEqual(len(ag_turns), 4)
        self.assertEqual(ag_turns[0]["items"][0]["content"], [{"type": "text", "text": "Recovered CLI prompt"}])
        self.assertEqual(
            ag_turns[1]["items"][0]["content"],
            [{"type": "image", "url": "data:image/png;base64,aGVsbG8="}, {"type": "text", "text": "Inspect screenshot"}],
        )
        self.assertEqual(
            ag_turns[2]["items"][0]["content"],
            [{"type": "image", "url": "data:image/png;base64,d29ybGQ="}],
        )

        # Repeated session/load must NOT duplicate any turns!
        sessions.antigravity.request("thread/archive", {"threadId": ag_thread})
        sessions.send(ag_thread, {"message": "Fifth turn after second session/load"})
        self._wait_until(lambda: not sessions.read(ag_thread)["running"])
        self.assertEqual(len(sessions.read(ag_thread)["thread"]["turns"]), 5)

        kimi_turns = sessions.read(kimi_thread)["thread"]["turns"]
        self.assertEqual(len(kimi_turns), 2)
        self.assertEqual(kimi_turns[0]["items"][0]["content"][0]["text"], "Kimi terminal prompt")

        muse_turns = sessions.read(muse_thread)["thread"]["turns"]
        self.assertEqual(len(muse_turns), 3)
        self.assertEqual(muse_turns[1]["items"][0]["content"][0]["text"], "Muse terminal turn after gap")

    def test_graceful_two_second_protocol_interrupt_before_sigterm_across_all_providers(self):
        coord = self._make_coordinator("interrupt-coord")
        sessions = coord["sessions"]

        # 1. Responsive interrupt (< 1.5s, child PID remains alive without SIGTERM).
        for provider in ("antigravity", "kimi", "muse", "claude"):
            with self.subTest(provider=provider, mode="responsive"):
                tid = sessions.create({"client": provider, "cwd": str(self.workspace)})["session"]["thread_id"]
                sessions.send(tid, {"message": "CANCEL_RESPONSIVE"})
                flag = self.root / f"running-{provider}-responsive.flag"
                self._wait_until(flag.exists)
                pid = int(flag.read_text(encoding="utf-8").strip())
                flag.unlink()

                t0 = time.monotonic()
                sessions.interrupt(tid)
                elapsed = time.monotonic() - t0
                self.assertLess(elapsed, 1.8)
                self._wait_until(lambda t=tid: not sessions.read(t)["running"])
                os.kill(pid, 0)  # Process survived graceful protocol interrupt!
                self.assertTrue(any(e["provider"] == provider for e in self._fixture_events(kind="cancel")))

        # 2. Hung interrupt (~2.0s protocol grace, then escalates to SIGTERM well under 6.0s — never 60s!).
        for provider in ("antigravity", "kimi", "muse", "claude"):
            with self.subTest(provider=provider, mode="hung"):
                tid = sessions.create({"client": provider, "cwd": str(self.workspace)})["session"]["thread_id"]
                sessions.send(tid, {"message": "CANCEL_HUNG"})
                flag = self.root / f"running-{provider}-hung.flag"
                self._wait_until(flag.exists)
                pid = int(flag.read_text(encoding="utf-8").strip())

                t0 = time.monotonic()
                sessions.interrupt(tid)
                elapsed = time.monotonic() - t0
                flag.unlink(missing_ok=True)
                self.assertGreaterEqual(elapsed, 1.8)
                self.assertLess(elapsed, 8.0)
                self.assertFalse(sessions.read(tid)["running"])
                self.assertEqual(sessions.read(tid)["thread"]["turns"][-1]["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
