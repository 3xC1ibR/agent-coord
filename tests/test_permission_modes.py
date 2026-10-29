"""Provider-specific permissions persist without collapsing to a YOLO bit."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from test_codex_app_server import BrowserSessions, CoordinationStore, CoordinationError, FakeCodex
from test_browser_providers import claude_factory
import test_claude_code as claude_transport_tests
from test_kimi import FakeKimi
from test_antigravity import FakeACP, CONFIG
from test_muse import FakeMuse
from agent_coord.antigravity import AntigravityRPC
from agent_coord.kimi import KimiRPC
from agent_coord.muse_code import MuseRPC
from agent_coord.permission_modes import (CLAUDE_MODES, CODEX_PRESETS, acp_modes, codex_options,
                                         legacy_mode, requested_mode, selected_mode)


class PermissionCodex(FakeCodex):
    def request(self, method, params=None):
        if method == "permissionProfile/list":
            self.calls.append((method, params))
            if params.get("cursor"):
                return {"data": [{"id": "managed-denied", "allowed": False}]}
            return {"data": [{"id": "custom-files", "allowed": True, "description": "Company files"}], "nextCursor": "next"}
        return super().request(method, params)


class PermissionACP(FakeACP):
    def request(self, method, params, **kwargs):
        result = super().request(method, params, **kwargs)
        if method in {"session/new", "session/resume", "session/load", "session/set_config_option"}:
            options = copy.deepcopy(CONFIG)
            options[1]["options"].insert(1, {"value": "auto_edit", "name": "Auto Edit"})
            options[1]["currentValue"] = params.get("value", "default") if params.get("configId") == "mode" else "default"
            result["configOptions"] = options
        return result


class PermissionMuse(FakeMuse):
    def request(self, method, params, **kwargs):
        if method == "permissionProfile/list":
            self.calls.append((method, params))
            return {"profiles": [{"profileId": value, "label": value, "available": True}
                                 for value in (":read-only", ":ask-me", ":auto-review", ":unrestricted", "custom-unknown")]}
        return super().request(method, params, **kwargs)


class PermissionModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = CoordinationStore(self.root / "state.sqlite3")
        self.sessions = self.make_sessions()

    def make_sessions(self):
        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=PermissionCodex,
            claude_factory=claude_factory,
            kimi_factory=lambda store, callback: KimiRPC(store, callback, connection_factory=FakeKimi),
            antigravity_factory=lambda store, callback: AntigravityRPC(store, callback, connection_factory=PermissionACP),
            muse_factory=lambda store, callback: MuseRPC(store, callback, connection_factory=PermissionMuse))
        self.addCleanup(sessions.close)
        return sessions

    def create(self, client, mode=None, **values):
        return self.sessions.create({"client": client, **({"permission_mode": mode} if mode else {}), **values})["session"]["thread_id"]

    def finish(self, client, thread):
        provider = self.sessions._provider(client)
        if client == "claude":
            provider.connections[thread].callback({"type": "result", "subtype": "success"})
        else:
            # A user Stop is also a valid fresh/resume transition and avoids
            # fabricating provider text simply to complete these settings tests.
            self.sessions.interrupt(thread)

    def test_catalogs_keep_native_modes_profiles_and_managed_availability(self):
        expected = {"claude": {row["id"] for row in CLAUDE_MODES},
                    "kimi": {"default", "plan", "auto", "yolo"},
                    "antigravity": {"default", "auto_edit", "yolo"},
                    "muse": {":read-only", ":ask-me", ":auto-review", ":unrestricted"}}
        for client, values in expected.items():
            with self.subTest(client=client):
                rows = self.sessions.permission_modes(client)
                self.assertEqual({row["id"] for row in rows if row.get("available") is not False}, values)
        codex = self.sessions.permission_modes("codex")
        self.assertEqual({row["id"] for row in codex}, set(CODEX_PRESETS) | {"profile:custom-files", "profile:managed-denied"})
        self.assertFalse(next(row for row in codex if row["id"] == "profile:managed-denied")["available"])

    def test_each_native_mode_reaches_new_and_resumed_provider(self):
        for client in ("claude", "kimi", "antigravity", "muse"):
            for row in self.sessions.permission_modes(client):
                if row.get("available") is False:
                    continue
                mode = row["id"]
                with self.subTest(client=client, mode=mode):
                    thread = self.create(client, mode)
                    self.sessions.send(thread, {"message": "Check permissions"})
                    provider = self.sessions._provider(client)
                    connection = provider.connections[thread]
                    self.assertEqual(connection.options["permission_mode"], mode)
                    if client == "kimi":
                        self.assertEqual(connection.mode, mode)
                    elif client == "muse":
                        self.assertEqual(connection.profile, mode)
                    elif client == "antigravity":
                        self.assertIn(mode, [p["value"] for method, p in connection.calls
                                             if method == "session/set_config_option" and p["configId"] == "mode"])
                    self.finish(client, thread)
                    self.sessions.send(thread, {"message": "Resume"})
                    self.assertEqual(provider.connections[thread].options["permission_mode"], mode)
                    self.finish(client, thread)

    def test_codex_presets_apply_all_axes_to_start_and_turn(self):
        for mode in CODEX_PRESETS:
            with self.subTest(mode=mode):
                thread = self.create("codex", mode)
                start = next(p for method, p in reversed(self.sessions.rpc.calls) if method == "thread/start")
                for key, value in codex_options(mode, [str(self.root)]).items():
                    self.assertEqual(start[key], value)
                self.sessions.send(thread, {"message": "Check"})
                turn = next(p for method, p in reversed(self.sessions.rpc.calls) if method == "turn/start")
                for key, value in codex_options(mode, [str(self.root)], turn=True).items():
                    self.assertEqual(turn[key], value)
                self.sessions.interrupt(thread)

    def test_named_codex_profile_is_passed_by_identity_not_approximated(self):
        thread = self.create("codex", "profile:custom-files")
        start = next(p for method, p in reversed(self.sessions.rpc.calls) if method == "thread/start")
        self.assertEqual(start["permissions"], "custom-files")
        self.assertNotIn("sandbox", start)
        self.assertNotIn("sandbox_workspace_write.writable_roots", start["config"])
        self.sessions.send(thread, {"message": "Check"})
        turn = next(p for method, p in reversed(self.sessions.rpc.calls) if method == "turn/start")
        self.assertEqual(turn["permissions"], "custom-files")
        self.assertNotIn("sandboxPolicy", turn)

    def test_mode_persists_across_backend_recreation_and_unrelated_edits(self):
        threads = {client: self.create(client, mode) for client, mode in (
            ("claude", "plan"), ("kimi", "auto"), ("antigravity", "auto_edit"),
            ("muse", ":auto-review"), ("codex", "auto-review"))}
        expected = {client: self.sessions._record(thread)["permission_mode"] for client, thread in threads.items()}
        native_threads = copy.deepcopy(self.sessions.rpc.threads)
        native_settings = copy.deepcopy(self.sessions.rpc.settings)
        self.sessions.close()
        self.sessions = self.make_sessions()
        # The fake server stores native history in memory; retain it separately
        # from the real SQLite settings database, as a restarted CLI would.
        self.sessions.rpc.threads = native_threads
        self.sessions.rpc.settings = native_settings
        for client, thread in threads.items():
            with self.subTest(client=client):
                self.sessions.update(thread, {"name": "Renamed"})
                self.assertEqual(self.sessions._record(thread)["permission_mode"], expected[client])
                self.sessions.send(thread, {"message": "Resume stored mode"})
                if client != "codex":
                    self.assertEqual(self.sessions._provider(client).connections[thread].options["permission_mode"], expected[client])
                self.finish(client, thread)

    def test_invalid_unavailable_and_conflicting_modes_do_not_change_settings(self):
        thread = self.create("codex")
        original = self.sessions._record(thread)["permission_mode"]
        for body in ({"permission_mode": "missing"}, {"permission_mode": "profile:managed-denied"},
                     {"permission_mode": "full-access", "yolo": False}, {"permission_mode": None},
                     {"permission_mode": " full-access"}, {"yolo": "false"}):
            with self.subTest(body=body):
                with self.assertRaises(CoordinationError):
                    self.sessions.update(thread, body)
                self.assertEqual(self.sessions._record(thread)["permission_mode"], original)

    def test_legacy_yolo_round_trip_and_nullable_migration_preserve_behavior(self):
        for client in ("codex", "claude", "kimi", "antigravity", "muse"):
            for yolo in (False, True):
                with self.subTest(client=client, yolo=yolo):
                    thread = self.create(client, yolo=yolo)
                    with self.store._connection() as db:
                        db.execute("UPDATE browser_sessions SET permission_mode=NULL WHERE thread_id=?", (thread,))
                    record = self.sessions._record(thread)
                    self.assertEqual(record["permission_mode"], legacy_mode(client, yolo))
                    self.sessions.update(thread, {"yolo": not yolo})
                    record = self.sessions._record(thread)
                    self.assertEqual(record["permission_mode"], legacy_mode(client, not yolo))
                    self.assertEqual(bool(record["yolo"]), not yolo)

    def test_fork_inherits_saved_mode_without_resetting_approval_reviewer(self):
        parent = self.create("codex", "auto-review")
        child = self.sessions.fork_work_thread(parent)["thread_id"]
        self.assertEqual(self.sessions._record(child)["permission_mode"], "auto-review")
        params = next(p for method, p in reversed(self.sessions.rpc.calls) if method == "thread/fork")
        self.assertEqual(params["approvalsReviewer"], "auto_review")

    def test_claude_native_fallback_is_reported_separately_from_saved_selection(self):
        thread = self.create("claude", "auto")
        self.sessions.send(thread, {"message": "Check"})
        self.sessions.claude.connections[thread].callback({"type": "system", "subtype": "status", "permissionMode": "default"})
        record = self.sessions._record(thread)
        self.assertEqual(record["permission_mode"], "auto")
        self.assertEqual(record["permission_mode_effective"], "default")

    def test_codex_native_readback_distinguishes_saved_and_applied_permissions(self):
        thread = self.create("codex", "full-access")
        self.sessions._capture_settings(thread, {"model": "available-model", "approvalPolicy": "on-request",
            "approvalsReviewer": "user", "sandbox": {"type": "readOnly"}})
        record = self.sessions._record(thread)
        self.assertEqual(record["permission_mode"], "full-access")
        self.assertEqual(record["permission_mode_effective"], "read-only")
        self.sessions.send(thread, {"message": "Apply explicit turn overrides"})
        self.assertEqual(self.sessions._record(thread)["permission_mode_effective"], "full-access")

    def test_kimi_mode_readback_mismatch_prevents_prompt_submission(self):
        class IgnoredMode(FakeKimi):
            def request(self, method, params, **kwargs):
                if method == "session/set_config_option" and params["configId"] == "mode":
                    return super().request(method, {**params, "value": "default"}, **kwargs)
                return super().request(method, params, **kwargs)
        thread = self.create("kimi", "auto")
        self.sessions.kimi._shutdown(thread)
        self.sessions.kimi.connection_factory = IgnoredMode
        with self.assertRaisesRegex(CoordinationError, "did not apply"):
            self.sessions.send(thread, {"message": "Must not execute"})
        self.assertNotIn(thread, self.sessions.kimi.turns)
        self.assertEqual(self.sessions._record(thread)["permission_mode"], "auto")

    def test_opening_saved_history_does_not_probe_native_mode_catalog(self):
        thread = self.create("muse")
        self.sessions.close()
        self.sessions = self.make_sessions()
        with patch.object(self.sessions.muse, "permission_modes", side_effect=AssertionError("Must stay offline")):
            self.assertEqual(self.sessions.read(thread)["session"]["permission_mode"], ":ask-me")

    def test_saved_unavailable_choice_stays_visible_and_menu_reads_do_not_mutate(self):
        thread = self.create("codex")
        with self.store._connection() as db:
            db.execute("UPDATE browser_sessions SET permission_mode='profile:removed' WHERE thread_id=?", (thread,))
        record = self.sessions._record(thread)
        result = self.sessions.permission_selection("codex", permission_mode=record["permission_mode"])
        self.assertEqual(result["permission_mode"], "profile:removed")
        self.assertFalse(next(row for row in result["permission_modes"] if row["id"] == "profile:removed")["available"])
        self.assertEqual(self.sessions._record(thread)["permission_mode"], "profile:removed")


class ClaudeNativeModeTransportTests(unittest.TestCase):
    setUp = claude_transport_tests.ClaudeTransportTests.setUp
    connection = claude_transport_tests.ClaudeTransportTests.connection

    def test_all_native_modes_use_exact_cli_flags(self):
        for entry in CLAUDE_MODES:
            mode = entry["id"]
            with self.subTest(mode=mode):
                connection = self.connection(permission_mode=mode, yolo=True)
                args = connection.control("initialize")["args"]
                self.assertEqual(args[args.index("--permission-mode") + 1], mode)
                self.assertEqual("--allow-dangerously-skip-permissions" in args, mode == "bypassPermissions")


class PermissionIdentityTests(unittest.TestCase):
    def test_unrelated_edit_keeps_explicit_mode_and_aliases_are_not_interchangeable(self):
        self.assertEqual(requested_mode("claude", {"model": "opus"}, {"client": "claude", "permission_mode": "plan"}), "plan")
        self.assertEqual(selected_mode({"client": "kimi", "yolo": True}), "yolo")
        self.assertNotEqual(legacy_mode("claude", True), legacy_mode("muse", True))

    def test_acp_grouped_choices_keep_native_values_and_labels(self):
        modes = acp_modes({"configOptions": [{"id": "mode", "options": [{"name": "Group", "options": [
            {"value": "native-custom", "name": "Custom", "description": "Native behavior"}]}]}]})
        self.assertEqual(modes, [{"id": "native-custom", "label": "Custom", "description": "Native behavior"}])


class PermissionHTTPTests(unittest.TestCase):
    make_sessions = PermissionModeTests.make_sessions

    def setUp(self):
        PermissionModeTests.setUp(self)
        from agent_coord.ui import make_ui_server
        self.server = make_ui_server(self.store, port=0, cwd=str(self.root), browser_sessions=self.sessions)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_server)
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.token = self.request("/api/browser/config")[1]["token"]

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def request(self, path, body=None):
        request = urllib.request.Request(self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "X-Agent-Coord-Token": getattr(self, "token", "")})
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def test_native_catalog_and_mode_create_update_read_round_trip(self):
        status, catalog = self.request("/api/browser/permission-modes?client=claude&yolo=true")
        self.assertEqual(status, 200)
        self.assertEqual(catalog["permission_mode"], "bypassPermissions")
        self.assertEqual({row["id"] for row in catalog["permission_modes"]}, {row["id"] for row in CLAUDE_MODES})
        status, created = self.request("/api/browser/sessions", {"client": "claude", "permission_mode": "plan"})
        self.assertEqual(status, 201)
        path = "/api/browser/sessions/" + created["session"]["thread_id"]
        self.assertEqual(created["session"]["permission_mode"], "plan")
        self.assertEqual(self.request(path, {"permission_mode": "auto"})[0], 200)
        self.assertEqual(self.request(path)[1]["session"]["permission_mode"], "auto")
        self.assertEqual(self.request(path, {"permission_mode": "unrecognized"})[0], 400)
        self.assertEqual(self.request(path)[1]["session"]["permission_mode"], "auto")

    def test_native_mode_routes_preserve_selection_and_busy_guard_for_every_provider(self):
        for client, mode in (("claude", "plan"), ("kimi", "auto"), ("antigravity", "auto_edit"),
                             ("muse", ":auto-review"), ("codex", "auto-review")):
            with self.subTest(client=client):
                status, created = self.request("/api/browser/sessions", {"client": client, "permission_mode": mode})
                self.assertEqual(status, 201)
                path = "/api/browser/sessions/" + created["session"]["thread_id"]
                self.assertEqual(self.request(path)[1]["session"]["permission_mode"], mode)
                self.assertEqual(self.request(path + "/messages", {"message": "Start"})[0], 200)
                self.assertEqual(self.request(path, {"permission_mode": legacy_mode(client)})[0], 409)
                self.assertEqual(self.request(path)[1]["session"]["permission_mode"], mode)

    def test_catalog_read_is_nonmutating_and_rejects_invalid_context(self):
        with self.store._connection() as db:
            before = db.execute("SELECT count(*) FROM browser_sessions").fetchone()[0]
        for client in ("codex", "claude", "kimi", "antigravity", "muse"):
            status, catalog = self.request("/api/browser/permission-modes?client=" + client)
            self.assertEqual(status, 200)
            self.assertEqual(catalog["permission_mode"], legacy_mode(client))
        self.assertEqual(self.request("/api/browser/permission-modes?client=missing")[0], 400)
        self.assertEqual(self.request("/api/browser/permission-modes?client=claude&yolo=1")[0], 400)
        self.assertEqual(self.request("/api/browser/permission-modes?client=claude&cwd=/")[0], 400)
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM browser_sessions").fetchone()[0], before)

    def test_unavailable_saved_mode_is_not_silently_replaced(self):
        status, catalog = self.request("/api/browser/permission-modes?client=codex&permission_mode=removed")
        self.assertEqual(status, 200)
        self.assertEqual(catalog["permission_mode"], "removed")
        self.assertFalse(next(row for row in catalog["permission_modes"] if row["id"] == "removed")["available"])
