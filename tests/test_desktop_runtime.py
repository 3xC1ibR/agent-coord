from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent-coord/scripts"))
from agent_coord import desktop_runtime as runtime
from agent_coord.codex_app_server import BrowserBusyError, BrowserSessions
from agent_coord.store import CoordinationError, CoordinationStore
from test_codex_app_server import FakeCodex

FAKE = r'''
import copy, json, os, pathlib, sys, threading, time
root = pathlib.Path(os.environ['RIBBON_FAKE_ROOT'])
(root / 'provider.pid').write_text(str(os.getpid()))
lock = threading.RLock()
approval = threading.Event()
threads = {}
counter = 0
persist = os.environ.get('RIBBON_FAKE_PERSIST') == '1'
history = root / 'native-history.json'
if persist and history.exists():
    threads = json.loads(history.read_text())
    counter = sum(len(t['turns']) for t in threads.values())
    for thread in threads.values():
        for turn in thread['turns']:
            if turn['status'] == 'inProgress': turn['status'] = 'interrupted'
def save():
    if persist:
        with lock: history.write_text(json.dumps(threads))
def emit(value):
    with lock:
        print(json.dumps(value), flush=True)
def record(value):
    with lock:
        with (root / 'calls.jsonl').open('a') as f: f.write(json.dumps(value) + '\n')
def work(identity, turn, needs_approval):
    # Native child activity insertion point.
    if (turn['items'][0]['content'][0].get('text') == 'held-work' or
            (os.environ.get('RIBBON_FAKE_HOLD_RECOVERY') == '1' and
             turn['items'][0]['content'][0].get('text', '').startswith('[Automatic continuation'))):
        while not (root / 'release-held-work').exists(): time.sleep(.01)
    if needs_approval:
        for n in range(5):
            time.sleep(.1)
            emit({'method':'item/agentMessage/delta','params':{'threadId':identity,'turnId':turn['id'],'itemId':'output','delta':str(n)}})
        emit({'id':900,'method':'item/commandExecution/requestApproval','params':{'threadId':identity,'turnId':turn['id'],'itemId':'tool','command':'fake-tool'}})
        approval.wait()
    time.sleep(.15)
    item = {'id':'output','type':'agentMessage','text':'output while desktop disconnected'}
    turn['items'].append(item)
    turn['status'] = 'completed'
    save()
    emit({'method':'item/completed','params':{'threadId':identity,'turnId':turn['id'],'item':item}})
    emit({'method':'turn/completed','params':{'threadId':identity,'turn':copy.deepcopy(turn)}})
for line in sys.stdin:
    req = json.loads(line)
    record(req)
    if req.get('id') == 900 and 'result' in req:
        approval.set()
        continue
    if 'id' not in req: continue
    method, p = req.get('method'), req.get('params') or {}
    identity = p.get('threadId', 'fake-thread')
    result = {}
    if method == 'thread/start':
        threads[identity] = {'id':identity,'cwd':p['cwd'],'turns':[]}
    if method in ('thread/start','thread/resume','thread/read'):
        threads.setdefault(identity, {'id':identity,'cwd':p.get('cwd',str(root)),'turns':[]})
        result = {'thread':copy.deepcopy(threads[identity]),'model':'fake-model','reasoningEffort':'low'}
    if method == 'model/list': result = {'data':[]}
    if method == 'turn/start':
        counter += 1
        turn = {'id':'turn-'+str(counter),'status':'inProgress','items':[{'id':'user','type':'userMessage','content':p['input']}]}
        threads[identity]['turns'].append(turn)
        save()
        result = {'turn':copy.deepcopy(turn)}
        emit({'id':req['id'],'result':result})
        emit({'method':'turn/started','params':{'threadId':identity,'turn':copy.deepcopy(turn)}})
        threading.Thread(target=work,args=(identity,turn,p['input'][0].get('text')=='one'),daemon=True).start()
        continue
    emit({'id':req['id'],'result':result})
'''


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ribbon-runtime-")
        self.root = Path(self.temporary.name)
        self.database = self.root / "state.sqlite3"
        self.resources = self.root / "App/Contents/Resources"
        self.resources.mkdir(parents=True)
        spec = importlib.util.spec_from_file_location("runtime_test_build", ROOT / "desktop/macos/build.py")
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        build.copy_backend(self.resources)
        shutil.copyfile(ROOT / "desktop/macos/backend.py", self.resources / "backend.py")
        (self.resources / "backend.json").write_text(json.dumps({"python":sys.executable, "database":None, "path":None,
                                                              **runtime.compatibility_metadata("1+1")}))
        tools = self.root / "bin"
        tools.mkdir()
        fake = tools / "codex"
        fake.write_text("#!" + sys.executable + "\n" + FAKE)
        fake.chmod(0o755)
        self.env = dict(os.environ, PYTHONPATH=str(self.resources / "backend"),
                        PATH=str(tools) + os.pathsep + os.environ["PATH"], RIBBON_FAKE_ROOT=str(self.root),
                        PYTHONWARNINGS="error::ResourceWarning")

    def tearDown(self):
        try:
            # Cleanup uses our private record even when a test deliberately
            # makes discovery fail; no PID from an untrusted/stale record is killed.
            discovery = runtime.runtime_directory(self.database) / "discovery.json"
            def stop_owned():
                if runtime.owner_available(runtime.runtime_directory(self.database)):
                    return True
                try:
                    if discovery.exists():
                        runtime.control(runtime.read_json(discovery), "stop")
                except (OSError, RuntimeError):
                    pass  # Handover may replace discovery between read and POST.
                return False
            self.wait(stop_owned, timeout=25)
        finally:
            self.temporary.cleanup()

    def wait(self, predicate, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            result = predicate()
            if result:
                return result
            time.sleep(.05)
        import sqlite3
        detail = self.log()[-2500:]
        if (self.root / "calls.jsonl").exists():
            detail += "\nCALLS: " + json.dumps(self.calls())[-5000:]
        try:
            with closing(sqlite3.connect(self.database)) as db:
                detail += "\nQUEUE: " + repr(db.execute("SELECT message,state,error,owner FROM browser_message_queue").fetchall())
        except sqlite3.Error:
            pass
        update_path = runtime.runtime_directory(self.database) / "update.json"
        if update_path.exists():
            detail += "\nUPDATE: " + update_path.read_text()
        try:
            status = runtime.control(runtime.discover(self.database), "status", timeout=2)
            detail += "\nSTATUS: " + json.dumps({k: status.get(k) for k in ("paused", "upgrade_error", "update")})
        except (RuntimeError, OSError):
            pass
        self.fail("Condition did not become true before timeout\n" + detail)

    def launch(self, resources=None, *, paused=False, confirm_updates=False):
        resources = resources or self.resources
        result = subprocess.run([sys.executable, "-B", str(resources / "backend.py"), "--runtime-bootstrap",
                                 "--resources", str(resources), "--db", str(self.database)] + (["--runtime-start-paused"] if paused else [])
                                + (["--runtime-confirm-updates"] if confirm_updates else []), env=self.env,
                                text=True, capture_output=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr + self.log())
        self.assertNotIn("ResourceWarning", result.stderr)
        return json.loads(result.stdout)

    def log(self):
        directory = runtime.runtime_directory(self.database)
        details = []
        for relative in ("runtime.log", "provider-transport/owner.log"):
            path = directory / relative
            if path.exists():
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 16_384))
                    details.append(relative + ":\n" + stream.read().decode(errors="replace"))
        return "\n".join(details)

    def api(self, url, path, body=None):
        headers = {}
        if body is not None:
            headers = {"X-Agent-Coord-Token": self.api(url, "api/browser/config")["token"], "Content-Type":"application/json"}
        request = urllib.request.Request(url + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:2000]
            error.close()
            error.msg = str(error.reason) + ": " + detail
            raise

    def assert_no_recovery_turns(self, thread):
        import sqlite3
        with closing(sqlite3.connect(self.database)) as db:
            table = db.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_update_recoveries'").fetchone()
            if table:
                self.assertEqual(db.execute("SELECT count(*) FROM runtime_update_recoveries WHERE thread_id=?", (thread,)).fetchone()[0], 0)

    def test_loopback_startup_does_not_resolve_hostnames(self):
        (self.resources / "backend/sitecustomize.py").write_text(
            "import socket\n"
            "def unavailable(*args, **kwargs):\n"
            "    raise RuntimeError('Reverse DNS is unavailable')\n"
            "socket.getfqdn = unavailable\n"
        )
        result = self.launch(paused=True)
        self.assertEqual(result["status"], "serving")
        record = runtime.discover(self.database)
        self.assertTrue(runtime.control(record, "status")["paused"])

    def test_detached_reconnect_preserves_provider_approval_output_and_queue_after_bundle_deletion(self):
        first = self.launch()
        url = first["url"]
        thread = self.api(url, "api/browser/sessions", {"cwd":str(self.root)})["session"]["thread_id"]
        self.api(url, f"api/browser/sessions/{thread}/messages", {"message":"one"})
        self.wait(lambda: (self.root / "provider.pid").exists())
        provider = int((self.root / "provider.pid").read_text())
        record = runtime.discover(self.database)
        self.wait(lambda: "pending approvals or questions" in runtime.control(record, "status")["busy"])
        self.api(url, f"api/browser/sessions/{thread}/queue", {"message":"two"})
        self.api(url, f"api/browser/sessions/{thread}/queue", {"message":"three"})
        reattached = self.launch()
        self.assertEqual((first["pid"], first["instance"]), (reattached["pid"], reattached["instance"]))
        shutil.rmtree(self.resources.parents[1])
        os.kill(provider, 0)
        detail = self.api(url, f"api/browser/sessions/{thread}")
        self.assertTrue(detail["requests"])
        key = detail["requests"][0]["key"]
        self.api(url, f"api/browser/sessions/{thread}/requests/{key}", {"decision":"accept"})
        self.wait(lambda: len([x for x in self.calls() if x.get("method") == "turn/start"]) == 3)
        self.wait(lambda: not runtime.control(record, "status")["busy"])
        self.assertEqual(len([x for x in self.calls() if x.get("id") == 900 and "result" in x]), 1)
        self.assertEqual([x["params"]["input"][0]["text"] for x in self.calls() if x.get("method") == "turn/start"], ["one","two","three"])
        detail = self.api(url, f"api/browser/sessions/{thread}")
        self.assertIn("output while desktop disconnected", json.dumps(detail))
        self.assertEqual(provider, int((self.root / "provider.pid").read_text()))
        self.assertNotIn("ResourceWarning", self.log())

    def calls(self):
        return [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]

    def test_parallel_launches_have_one_owner_and_controls_reject_web_token(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: self.launch(), range(2)))
        self.assertEqual(values[0]["instance"], values[1]["instance"])
        record = runtime.discover(self.database)
        web_token = self.api(values[0]["url"], "api/browser/config")["token"]
        req = urllib.request.Request(record["control_url"], data=b'{}', headers={"Authorization":"Bearer " + web_token})
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(req)
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()
        self.assertEqual((runtime.runtime_directory(self.database) / "discovery.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(runtime.control(record, "decision", target=runtime.compatibility_metadata("2"))["instance"], record["instance"])
        loser = subprocess.run([sys.executable, str(Path(record["payload"]) / "backend.py"), "--runtime-serve", "--db", str(self.database)],
                               env=dict(self.env, PYTHONPATH=str(Path(record["payload"]) / "backend")), capture_output=True, timeout=5)
        self.assertNotEqual(loser.returncode, 0)
        self.assertEqual(runtime.discover(self.database)["instance"], record["instance"])

    def test_bootstrap_waits_for_standalone_operation(self):
        directory = runtime.runtime_directory(self.database)
        payload = runtime.stage_payload(self.resources, directory)
        descriptor = runtime.lock_file(directory / "owner.lock")
        release = threading.Timer(.5, os.close, args=(descriptor,))
        release.start()
        try:
            with patch.object(runtime, "stage_payload", return_value=payload), patch.dict(os.environ, self.env):
                result = runtime.bootstrap(self.database, self.resources, start_paused=True)
            self.assertEqual(result["status"], "serving")
            self.assertTrue(result["paused"])
        finally:
            release.join()

    def test_bootstrap_reserves_ownership_before_spawning_child(self):
        directory = runtime.runtime_directory(self.database)
        spawn = subprocess.Popen

        def inspect_reservation(*args, **kwargs):
            try:
                descriptor = runtime.lock_file(directory / "owner.lock")
            except BlockingIOError:
                pass
            else:
                os.close(descriptor)
                self.fail("A standalone writer can take ownership before the runtime starts.")
            return spawn(*args, **kwargs)

        with patch.object(runtime.subprocess, "Popen", side_effect=inspect_reservation), patch.dict(os.environ, self.env):
            first = runtime.bootstrap(self.database, self.resources, start_paused=True)
        self.assertFalse(runtime.owner_available(directory))
        second = self.launch(paused=True)
        self.assertEqual(first["instance"], second["instance"])

    def test_bootstrap_releases_reservation_when_spawn_fails(self):
        with patch.object(runtime.subprocess, "Popen", side_effect=OSError("Cannot spawn runtime")):
            with self.assertRaisesRegex(OSError, "Cannot spawn runtime"):
                runtime.bootstrap(self.database, self.resources)
        self.assertTrue(runtime.owner_available(runtime.runtime_directory(self.database)))

    def test_bootstrap_does_not_spawn_when_ownership_stays_busy(self):
        directory = runtime.runtime_directory(self.database)
        payload = runtime.stage_payload(self.resources, directory)
        descriptor = runtime.lock_file(directory / "owner.lock")
        try:
            with patch.object(runtime, "stage_payload", return_value=payload), \
                    patch.object(runtime.time, "monotonic", side_effect=(0, 20)), \
                    patch.object(runtime.subprocess, "Popen") as spawn:
                with self.assertRaisesRegex(RuntimeError, "no runtime was started"):
                    runtime.bootstrap(self.database, self.resources)
                spawn.assert_not_called()
            self.assertFalse(runtime.owner_available(directory))
        finally:
            os.close(descriptor)

    def test_hook_owner_handshake_does_not_wait_for_readiness_locks(self):
        source = self.resources / "backend/agent_coord/desktop_runtime.py"
        source.write_text(source.read_text() + '''
_original_status = Runtime.status
def _status_with_barrier(self):
    if not getattr(self, '_barrier_started', False):
        self._barrier_started = True
        def hold_readiness_locks():
            root = Path(os.environ['RIBBON_FAKE_ROOT'])
            while not (root / 'hold-locks').exists():
                if self.stopping.wait(.01): return
            sessions = self.server.browser_sessions
            with self.mutex, sessions.lock, sessions.persistence.condition:
                (root / 'locks-held').touch()
                deadline = time.monotonic() + 15
                while not (root / 'release-locks').exists() and time.monotonic() < deadline:
                    time.sleep(.01)
        threading.Thread(target=hold_readiness_locks, daemon=True).start()
    return _original_status(self)
Runtime.status = _status_with_barrier
''')
        first = self.launch(paused=True)
        record = runtime.discover(self.database, timeout=2)
        (self.root / "hold-locks").touch()
        try:
            self.wait(lambda: (self.root / "locks-held").exists())
            current = runtime.discover(self.database, timeout=2)
            identity = runtime.control(current, "identity", timeout=2)
            self.assertEqual((identity["pid"], identity["instance"]), (first["pid"], first["instance"]))
            hook = {"hook_event_name": "PreToolUse", "session_id": "contention-test", "cwd": str(self.root),
                    "tool_name": "Bash", "tool_input": {"command": "pwd"}}
            env = dict(self.env, AGENT_COORD_DB=str(self.database), AGENT_COORD_SESSION_ID="contention-test",
                       AGENT_COORD_CLIENT="codex")
            result = subprocess.run([sys.executable, str(ROOT / "plugins/agent-coord/scripts/agent-coord-hook")],
                                    input=json.dumps(hook), text=True, capture_output=True, env=env, timeout=8)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((self.root / "release-locks").exists())
            bad = {**record, "control_token": "wrong"}
            with self.assertRaises(urllib.error.HTTPError) as denied:
                runtime.control(bad, "identity", timeout=2)
            self.assertEqual(denied.exception.code, 403)
            denied.exception.close()
        finally:
            (self.root / "release-locks").touch()
        self.assertEqual(runtime.control(record, "status")["instance"], first["instance"])

    def prepare_requested_update(self, *, corrupt=False, message="one", legacy=False):
        # Explicit-update tests hold only automatic activation until the public
        # activate-update request is accepted. The independent live gate covers
        # passive activation; this barrier removes a race with the new path.
        runtime_source = self.resources / "backend/agent_coord/desktop_runtime.py"
        runtime_source.write_text(runtime_source.read_text() + '''
_explicit_test_readiness = uninterrupted_readiness
def uninterrupted_readiness(sessions):
    from . import runtime_update as test_updates
    reasons = _explicit_test_readiness(sessions)
    update = test_updates.read(runtime_directory(sessions.store.database_path))
    if not update or update.get("phase") not in {"accepted", "draining", "stopping"}:
        reasons.append("automatic activation held by explicit-update fixture")
    return reasons
''')
        candidate_runtime_source = runtime_source.read_text()
        if legacy:
            # Frozen 6d3a900 control code reproduces the real legacy launch,
            # shutdown drain and recovery boundary, including direct trial entry.
            runtime_source.write_bytes((ROOT / "tests/fixtures/legacy_desktop_runtime.py").read_bytes())
        first = self.launch()
        record = runtime.discover(self.database)
        thread = self.api(first["url"], "api/browser/sessions", {"cwd": str(self.root)})["session"]["thread_id"]
        self.api(first["url"], f"api/browser/sessions/{thread}/messages", {"message": message})
        self.wait(lambda: ("pending approvals or questions" if message == "one" else "active turns") in runtime.control(record, "status")["busy"])
        new = self.root / "Candidate/Contents/Resources"
        shutil.copytree(self.resources, new)
        if legacy:
            candidate_source = new / "backend/agent_coord/desktop_runtime.py"
            candidate_source.write_text(candidate_runtime_source)
        metadata = json.loads((new / "backend.json").read_text())
        metadata["runtime_version"] = "2+2"
        (new / "backend.json").write_text(json.dumps(metadata))
        if corrupt:
            source = new / "backend/agent_coord/desktop_runtime.py"
            source.write_text(source.read_text() + "\nfrom contextlib import closing\n_original_serve = Runtime.serve\ndef fail_trial(self):\n    if self.commit_fd is not None:\n        with closing(sqlite3.connect(self.database)) as db:\n            db.execute('CREATE TABLE trial_migration (id INTEGER)')\n            db.commit()\n        atomic_json(self.directory / 'schema.json', {'schema': 999, 'fingerprint': 'trial'})\n        raise RuntimeError('injected candidate failure')\n    return _original_serve(self)\nRuntime.serve = fail_trial\n")
            if corrupt == "cancel":
                source.write_text(source.read_text().replace("        raise RuntimeError('injected candidate failure')",
                    "        (self.directory / 'trial-entered').touch()\n        while not (self.directory / 'release-trial').exists(): time.sleep(.01)\n        raise RuntimeError('injected candidate failure')"))
        staged = self.launch(new)
        return first, record, thread, staged, new

    def wait_update(self, phase):
        def ready():
            try:
                record = runtime.discover(self.database, timeout=1)
                status = runtime.control(record, "status", timeout=1) if record else None
                return status if status and (status.get("update") or {}).get("phase") == phase and status["status"] == "serving" else None
            except (OSError, RuntimeError):
                return None
        return self.wait(ready, timeout=35)

    def hold_admitted_operation(self, *, drain_timeout=30):
        source = self.resources / "backend/agent_coord/desktop_runtime.py"
        source.write_text(source.read_text() + '''
_install_admission_before_held_operation = install_session_admission
def install_session_admission(sessions):
    gate = _install_admission_before_held_operation(sessions)
    root = Path(os.environ["RIBBON_FAKE_ROOT"])
    if (root / "admitted-operation-entered").exists():
        return gate
    def hold_operation():
        with gate.enter() as allowed:
            if not allowed:
                return
            (root / "admitted-operation-entered").touch()
            while not (root / "release-admitted-operation").exists():
                time.sleep(.01)
            with gate.enter() as nested:
                if nested:
                    (root / "admitted-nested-operation-finished").touch()
    threading.Thread(target=hold_operation, daemon=True).start()
    return gate
''' + f"\nUPDATE_ADMISSION_SECONDS = {drain_timeout!r}\n")

    def test_requested_update_closes_admission_before_waiting_for_requests(self):
        self.hold_admitted_operation()
        first, record, thread, staged, _ = self.prepare_requested_update()
        self.wait(lambda: (self.root / "admitted-operation-entered").exists())
        original = runtime.control(record, "status")["transport"]
        try:
            runtime.control(record, "activate-update", request_id="drain-admitted-operation", payload=staged["pending"])
            def draining():
                status = runtime.control(record, "status")
                return status if status["update"]["phase"] == "draining" else None
            status = self.wait(draining, timeout=5)
            self.assertTrue(status["paused"])
            self.assertIn("admitted operations", status["update"]["blocked"])
            rejected = runtime.control(record, "client-request", request={})
            self.assertIn("pausing new coordination requests", rejected["failure"]["message"])
        finally:
            (self.root / "release-admitted-operation").touch()
        current = self.wait_update("committed")
        self.assertEqual(current["payload"], staged["pending"])
        self.assertFalse(current["paused"])
        self.assertTrue((self.root / "admitted-nested-operation-finished").exists())
        self.assertEqual(current["transport"]["pid"], original["pid"])
        self.assertEqual(current["transport"]["providers"], original["providers"])
        self.assertGreaterEqual(current["update"]["admission_drain_seconds"], 0)
        snapshot = self.api(current["url"], f"api/browser/sessions/{thread}")
        self.assertEqual(len(snapshot["requests"]), 1)
        self.api(current["url"], f"api/browser/sessions/{thread}/requests/{snapshot['requests'][0]['key']}", {"decision":"accept"})
        self.assertEqual(len([call for call in self.calls() if call.get("method") == "turn/start"]), 1)

    def test_requested_update_admission_timeout_reopens_original_backend(self):
        self.hold_admitted_operation(drain_timeout=.5)
        first, record, thread, staged, _ = self.prepare_requested_update()
        original = runtime.control(record, "status")["transport"]
        try:
            runtime.control(record, "activate-update", request_id="admission-timeout", payload=staged["pending"])
            current = self.wait_update("failed")
            self.assertEqual(current["instance"], first["instance"])
            self.assertEqual(current["transport"]["pid"], original["pid"])
            self.assertFalse(current["paused"])
            self.assertIsNone(current["pending"])
            self.assertIn("did not drain", current["update"]["error"])
            self.assertGreaterEqual(current["update"]["admission_drain_seconds"], .5)
            self.assertEqual(runtime.control(record, "client-request", request={})["failure"]["message"],
                             "Invalid runtime client request.")
            snapshot = self.api(current["url"], f"api/browser/sessions/{thread}")
            self.api(current["url"], f"api/browser/sessions/{thread}/requests/{snapshot['requests'][0]['key']}", {"decision":"accept"})
        finally:
            (self.root / "release-admitted-operation").touch()

    def test_update_confirmation_holds_an_idle_runtime_until_install(self):
        first = self.launch()
        record = runtime.discover(self.database)
        new = self.root / "Candidate/Contents/Resources"
        shutil.copytree(self.resources, new)
        metadata = json.loads((new / "backend.json").read_text())
        metadata["runtime_version"] = "2+2"
        (new / "backend.json").write_text(json.dumps(metadata))
        staged = self.launch(new, confirm_updates=True)
        self.assertTrue(staged["pending_confirmation"])
        self.assertEqual(staged["runtime_update_prompts"], 1)
        time.sleep(1.2)  # More than two idle-handover polling intervals.
        status = runtime.control(record, "status")
        self.assertEqual(status["instance"], first["instance"])
        self.assertFalse(status["paused"])
        self.assertEqual(status["busy"], [])
        # A legacy attachment must not silently undo the user's deferral.
        self.assertTrue(self.launch(new)["pending_confirmation"])
        runtime.control(record, "activate-update", request_id="confirmed-install", payload=staged["pending"])
        current = self.wait_update("committed")
        self.assertEqual(current["payload"], staged["pending"])
        self.assertFalse(current["pending_confirmation"])

    def test_selecting_current_payload_clears_a_deferred_update(self):
        first = self.launch()
        new = self.root / "Candidate/Contents/Resources"
        shutil.copytree(self.resources, new)
        (new / "notification-test").write_text("new payload")
        staged = self.launch(new, confirm_updates=True)
        self.assertTrue(staged["pending_confirmation"])
        selected = self.launch()
        self.assertEqual(selected["instance"], first["instance"])
        self.assertIsNone(selected["pending"])
        self.assertFalse(selected["pending_confirmation"])

    def test_requested_update_replaces_coordinator_and_preserves_original_approval(self):
        first, record, thread, staged, _ = self.prepare_requested_update()
        original = self.api(first["url"], f"api/browser/sessions/{thread}")
        provider = int((self.root / "provider.pid").read_text())
        accepted = runtime.control(record, "activate-update", request_id="acceptance-update", payload=staged["pending"])
        duplicate = runtime.control(record, "activate-update", request_id="acceptance-update", payload=staged["pending"])
        self.assertEqual(accepted["update"]["id"], duplicate["update"]["id"])
        current = self.wait_update("committed")
        self.assertEqual(current["payload"], staged["pending"])
        self.assertNotEqual(current["pid"], first["pid"])
        self.assertEqual(provider, int((self.root / "provider.pid").read_text()))
        detail = self.api(current["url"], f"api/browser/sessions/{thread}")
        self.assertEqual(detail["activeTurn"], original["activeTurn"])
        self.assertEqual([r["key"] for r in detail["requests"]], [r["key"] for r in original["requests"]])
        self.assertTrue(current["update"]["uninterrupted"])
        self.assert_no_recovery_turns(thread)
        self.api(current["url"], f"api/browser/sessions/{thread}/requests/{original['requests'][0]['key']}", {"decision": "accept"})
        self.wait(lambda: not self.api(current["url"], f"api/browser/sessions/{thread}")["running"])
        self.assertEqual(len([call for call in self.calls() if call.get("method") == "turn/start"]), 1)
        self.assertNotIn("ResourceWarning", self.log())

    def test_legacy_adoption_then_upgrade_retains_original_native_work(self):
        self.env["RIBBON_FAKE_PERSIST"] = "1"
        self.env["RIBBON_FAKE_HOLD_RECOVERY"] = "1"
        first, record, thread, staged, candidate = self.prepare_requested_update(message="held-work", legacy=True)
        runtime.control(record, "activate-update", request_id="legacy-adoption", payload=staged["pending"])
        current = self.wait_update("committed")
        self.wait(lambda: runtime.control(runtime.discover(self.database), "status")["update"]["recovery"]["counts"] == {"continued": 1})
        current = runtime.control(runtime.discover(self.database), "status")
        self.assertFalse(current["update"].get("uninterrupted", False))
        original = self.api(current["url"], f"api/browser/sessions/{thread}")["activeTurn"]
        provider = int((self.root / "provider.pid").read_text())
        turns = len([call for call in self.calls() if call.get("method") == "turn/start"])
        # The directly adopted coordinator also needs crash recovery before it
        # has performed its first normal supervised replacement.
        os.kill(current["pid"], signal.SIGKILL)
        def recovered():
            try:
                record = runtime.discover(self.database, timeout=.5)
                status = runtime.control(record, "status", timeout=1) if record else None
                return status if status and status["pid"] != current["pid"] else None
            except (RuntimeError, OSError):
                return None
        current = self.wait(recovered)
        self.assertEqual(provider, int((self.root / "provider.pid").read_text()))
        self.assertEqual(self.api(current["url"], f"api/browser/sessions/{thread}")["activeTurn"], original)
        self.assertEqual(len([call for call in self.calls() if call.get("method") == "turn/start"]), turns)
        second = self.root / "Second/Contents/Resources"
        shutil.copytree(candidate, second)
        metadata = json.loads((second / "backend.json").read_text())
        metadata["runtime_version"] = "3+3"
        (second / "backend.json").write_text(json.dumps(metadata))
        staged = self.launch(second, confirm_updates=True)
        runtime.control(runtime.discover(self.database), "activate-update", request_id="following-upgrade", payload=staged["pending"])
        upgraded = self.wait_update("committed")
        self.assertEqual(upgraded["payload"], staged["pending"])
        self.assertTrue(upgraded["update"]["uninterrupted"])
        self.assertNotEqual(upgraded["pid"], current["pid"])
        self.assertEqual(upgraded["transport"]["pid"], current["transport"]["pid"])
        self.assertEqual(provider, int((self.root / "provider.pid").read_text()))
        self.assertEqual(self.api(upgraded["url"], f"api/browser/sessions/{thread}")["activeTurn"], original)
        self.assertEqual(len([call for call in self.calls() if call.get("method") == "turn/start"]), turns)
        (self.root / "release-held-work").touch()
        self.wait(lambda: not self.api(upgraded["url"], f"api/browser/sessions/{thread}")["running"])

    def test_requested_update_rolls_back_candidate_migration(self):
        first, record, thread, staged, candidate = self.prepare_requested_update(corrupt=True)
        runtime.control(record, "activate-update", request_id="rollback-update", payload=staged["pending"])
        current = self.wait_update("rolled-back")
        self.assertEqual(current["payload"], first["payload"])
        import sqlite3
        with closing(sqlite3.connect(self.database)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='trial_migration'").fetchone())
        self.assertEqual(runtime.read_json(runtime.runtime_directory(self.database) / "schema.json")["schema"], runtime.SCHEMA_VERSION)
        attached = self.launch(candidate)
        self.assertEqual(attached["payload"], first["payload"])
        self.assertEqual(attached["update"]["phase"], "rolled-back")
        self.assertNotIn("ResourceWarning", self.log())

    def test_requested_update_preserves_native_turn_and_stop_stays_stopped(self):
        import sqlite3
        from contextlib import closing
        self.env["RIBBON_FAKE_PERSIST"] = "1"
        first, record, thread, staged, candidate = self.prepare_requested_update(message="held-work")
        provider = int((self.root / "provider.pid").read_text())
        original = self.api(first["url"], f"api/browser/sessions/{thread}")["activeTurn"]
        runtime.control(record, "activate-update", request_id="continue-update", payload=staged["pending"])
        current = self.wait_update("committed")
        self.assertNotEqual(current["pid"], first["pid"])
        self.assertEqual(provider, int((self.root / "provider.pid").read_text()))
        self.assertEqual(self.api(current["url"], f"api/browser/sessions/{thread}")["activeTurn"], original)
        self.assert_no_recovery_turns(thread)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT original_request FROM work_threads WHERE thread_id=?", (thread,)).fetchone()[0], "held-work")
        turns = [call for call in self.calls() if call.get("method") == "turn/start"]
        self.assertEqual([call["params"]["threadId"] for call in turns], [thread])
        runtime.control(runtime.discover(self.database), "stop")
        self.wait(lambda: runtime.owner_available(runtime.runtime_directory(self.database)))
        result = subprocess.run([sys.executable, "-B", str(candidate / "backend.py"), "--db", str(self.database),
                                 "--runtime-command", "status", "--resources", str(candidate)], env=self.env,
                                text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(runtime.owner_available(runtime.runtime_directory(self.database)))
        self.launch(candidate)
        self.assertEqual(len([call for call in self.calls() if call.get("method") == "turn/start"]), 1)
        self.assertNotIn("ResourceWarning", self.log())

    def test_stop_during_trial_rolls_back_without_restarting(self):
        first, record, thread, staged, candidate = self.prepare_requested_update(corrupt="cancel")
        runtime.control(record, "activate-update", request_id="cancel-update", payload=staged["pending"])
        directory = runtime.runtime_directory(self.database)
        self.wait(lambda: (directory / "trial-entered").exists(), timeout=20)
        with subprocess.Popen([sys.executable, "-B", str(candidate / "backend.py"), "--db", str(self.database),
                               "--runtime-command", "stop"], env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as helper:
            try:
                self.wait(lambda: (directory / "update-cancellation.json").exists())
            finally:
                (directory / "release-trial").touch()
            stdout, stderr = helper.communicate(timeout=15)
            self.assertEqual(helper.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout)["status"], "stopped")
        self.assertTrue(runtime.owner_available(directory))
        self.assertEqual(runtime.read_json(directory / "stop-proof.json")["instance"], first["instance"])
        decision = subprocess.run([sys.executable, "-B", str(candidate / "backend.py"), "--db", str(self.database),
                                   "--runtime-command", "decision"], env=self.env, text=True, capture_output=True,
                                  input=json.dumps({"stopped_instance": first["instance"], "target": runtime.compatibility_metadata("2+2")}), timeout=10)
        self.assertEqual(decision.returncode, 0, decision.stderr)
        self.assertEqual(runtime.read_json(directory / "schema.json")["schema"], runtime.SCHEMA_VERSION)
        from agent_coord import runtime_update
        self.assertTrue(runtime_update.read(directory)["cancelled"])
        self.assertEqual(runtime_update.read(directory)["phase"], "rolled-back")

    def test_requested_update_prefers_queued_user_input_without_duplicate_continuation(self):
        self.env["RIBBON_FAKE_PERSIST"] = "1"
        first, record, thread, staged, _ = self.prepare_requested_update(message="held-work")
        self.api(first["url"], f"api/browser/sessions/{thread}/queue", {"message": "user follow-up"})
        runtime.control(record, "activate-update", request_id="queue-update", payload=staged["pending"])
        current = self.wait_update("committed")
        self.assertEqual(len([call for call in self.calls() if call.get("method") == "turn/start"]), 1)
        self.assertTrue(self.api(current["url"], f"api/browser/sessions/{thread}")["running"])
        (self.root / "release-held-work").touch()
        self.wait(lambda: len([call for call in self.calls() if call.get("method") == "turn/start"]) == 2)
        self.wait(lambda: not runtime.control(runtime.discover(self.database), "status")["busy"])
        turns = [call["params"]["input"][0]["text"] for call in self.calls() if call.get("method") == "turn/start"]
        self.assertEqual(turns, ["held-work", "user follow-up"])

    def test_failed_projection_freeze_rejects_update_and_preserves_running_work(self):
        self.env["RIBBON_FAKE_PERSIST"] = "1"
        source = self.resources / "backend/agent_coord/coordinator_projection.py"
        source.write_text(source.read_text() + "\ndef fail_freeze(self):\n    raise RuntimeError('injected unsaved projection')\nCoordinatorProjection.freeze = fail_freeze\n")
        first, record, thread, staged, candidate = self.prepare_requested_update(message="held-work")
        provider = int((self.root / "provider.pid").read_text())
        original = self.api(first["url"], f"api/browser/sessions/{thread}")["activeTurn"]
        runtime.control(record, "activate-update", request_id="incomplete-update", payload=staged["pending"])
        current = self.wait(lambda: (value if (value := runtime.control(record, "status")).get("upgrade_error") else None))
        self.assertIn("injected unsaved projection", current["upgrade_error"])
        self.assertEqual(current["pid"], first["pid"])
        self.assertEqual(provider, int((self.root / "provider.pid").read_text()))
        self.assertFalse(current["paused"])
        self.assertEqual(self.api(first["url"], f"api/browser/sessions/{thread}")["activeTurn"], original)
        self.assertEqual(current["update"]["phase"], "failed")
        self.assertEqual(len([call for call in self.calls() if call.get("method") == "turn/start"]), 1)
        (self.root / "release-held-work").touch()
        self.wait(lambda: not self.api(first["url"], f"api/browser/sessions/{thread}")["running"])

    def test_incomplete_shutdown_keeps_all_work_paused_across_restart(self):
        self.env["RIBBON_FAKE_PERSIST"] = "1"
        source = self.resources / "backend/agent_coord/runtime_recovery.py"
        source.write_text(source.read_text() + "\ndef fail_finalize(self, update_id):\n    raise RuntimeError('injected incomplete shutdown')\nRuntimeRecovery.finalize = fail_finalize\n")
        first, record, thread, staged, candidate = self.prepare_requested_update(message="held-work", legacy=True)
        runtime.control(record, "activate-update", request_id="incomplete-update", payload=staged["pending"])
        current = self.wait_update("committed")
        self.assertTrue(current["paused"])
        self.assertTrue(current["update"]["requires_review"])
        self.assertEqual(current["update"]["recovery"]["counts"], {"uncertain": 1})
        self.assertEqual(len([call for call in self.calls() if call.get("method") == "turn/start"]), 1)
        self.assert_review_notice(current)
        runtime.control(runtime.discover(self.database), "stop")
        self.wait(lambda: runtime.owner_available(runtime.runtime_directory(self.database)))
        restarted = self.launch(candidate)
        self.assertTrue(restarted["paused"])
        self.assert_review_notice(restarted)

    def assert_review_notice(self, status):
        # A committed update awaiting review is not transient maintenance.
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            self.api(status["url"], "api/browser/sessions", {"cwd": str(self.root)})
        self.assertEqual(rejected.exception.code, 503)
        self.assertIn(runtime.REVIEW_NOTICE, str(rejected.exception))
        rejected.exception.close()
        failure = runtime.control(runtime.discover(self.database), "client-request", request={})["failure"]
        self.assertEqual(failure, {"type": "RuntimeRequestUnavailable", "message": runtime.REVIEW_NOTICE})
        self.assertIn("shutdown was incomplete", runtime.control(runtime.discover(self.database), "status")["upgrade_error"])

    def test_bookkeeping_accepted_during_shutdown_drain_completes_update_cleanly(self):
        # agent-coord-clfw: provider events arriving after the drain begins,
        # briefly busy, previously left the update paused for review.
        self.env["RIBBON_FAKE_PERSIST"] = "1"
        source = self.resources / "backend/agent_coord/codex_app_server.py"
        source.write_text(source.read_text() + """
import os as _os, pathlib as _pathlib, sqlite3 as _sqlite3, threading as _threading, time as _time
_original_close_components = BrowserSessions._close_components
def _close_with_late_bookkeeping(self):
    persistence, attempts, closing = self.persistence, [], _threading.Event()
    def late():
        attempts.append(1)
        if len(attempts) < 4:
            raise _sqlite3.OperationalError("database is locked")
        (_pathlib.Path(_os.environ["RIBBON_FAKE_ROOT"]) / "late-bookkeeping-saved").touch()
    def earlier():
        closing.wait(10)
        _time.sleep(.2)  # The drain is already waiting when this event arrives.
        persistence.submit(late, key="late-provider-event")
    original_close = persistence.close
    def close(*args, **kwargs):
        closing.set()
        return original_close(*args, **kwargs)
    persistence.close = close
    persistence.submit(earlier, key="earlier-provider-event")
    return _original_close_components(self)
BrowserSessions._close_components = _close_with_late_bookkeeping
""")
        first, record, thread, staged, _ = self.prepare_requested_update(message="held-work", legacy=True)
        runtime.control(record, "activate-update", request_id="late-bookkeeping-update", payload=staged["pending"])
        self.wait_update("committed")
        # Admission stays closed only while the recovery decision runs.
        self.wait(lambda: runtime.control(runtime.discover(self.database), "status")["update"]["recovery"]["counts"] == {"continued": 1})
        current = runtime.control(runtime.discover(self.database), "status")
        self.assertFalse(current["paused"], current["update"])
        self.assertFalse(current["update"].get("requires_review", False))
        self.assertFalse(current["update"].get("recovery_uncertain", False))
        shutdown = current["update"]["shutdown"]
        self.assertTrue(shutdown["clean"], shutdown)
        self.assertGreaterEqual(shutdown["submitted_while_closing"], 1)
        self.assertGreaterEqual(shutdown["retries_while_closing"], 3)
        self.assertIn("queue_depth", shutdown["writer"])
        self.assertTrue((self.root / "late-bookkeeping-saved").exists())
        self.assertNotIn("did not stop cleanly", self.log())

    def test_reviewed_resume_is_durable_without_replaying_uncertain_work(self):
        self.env["RIBBON_FAKE_PERSIST"] = "1"
        source = self.resources / "backend/agent_coord/runtime_recovery.py"
        source.write_text(source.read_text() + "\ndef fail_finalize(self, update_id):\n    raise RuntimeError('injected incomplete shutdown')\nRuntimeRecovery.finalize = fail_finalize\n")
        _, record, _, staged, candidate = self.prepare_requested_update(message="held-work", legacy=True)
        runtime.control(record, "activate-update", request_id="reviewed-update", payload=staged["pending"])
        current = self.wait_update("committed")
        self.assertTrue(current["paused"])
        self.assertFalse(current["update"].get("cancelled", False))
        # Explicit resume acknowledges the operator's review durably. It must
        # not retry uncertain provider submissions, including on the next boot.
        record = runtime.discover(self.database)
        resumed = runtime.control(record, "resume")
        self.assertFalse(resumed["paused"])
        # Admission now accepts requests; this empty one fails validation instead.
        self.assertEqual(runtime.control(record, "client-request", request={})["failure"]["message"],
                         "Invalid runtime client request.")
        self.assertFalse(resumed["update"]["requires_review"])
        self.assertTrue(resumed["update"]["recovery_uncertain"])
        self.assertIsNone(resumed["upgrade_error"])
        reviewed_at = resumed["update"]["reviewed_at"]
        self.assertGreater(reviewed_at, resumed["update"]["created_at"])
        self.assertEqual(runtime.control(record, "resume")["update"]["reviewed_at"], reviewed_at)
        self.assertEqual(resumed["update"]["recovery"]["counts"], {"uncertain": 1})
        runtime.control(record, "stop")
        self.wait(lambda: runtime.owner_available(runtime.runtime_directory(self.database)))
        restarted = self.launch(candidate)
        self.assertFalse(restarted["paused"])
        self.assertFalse(restarted["update"]["requires_review"])
        self.assertEqual(len([call for call in self.calls() if call.get("method") == "turn/start"]), 1)

    def test_busy_update_serves_new_assets_and_accepts_approval_before_handover(self):
        # Hold the handover boundary while an approval is pending. The accepted
        # turn must remain answerable before admission is closed for replacement.
        runtime_source = self.resources / "backend/agent_coord/desktop_runtime.py"
        runtime_source.write_text(runtime_source.read_text() + '''
_readiness_without_test_barrier = uninterrupted_readiness
def uninterrupted_readiness(sessions):
    result = _readiness_without_test_barrier(sessions)
    root = Path(os.environ["RIBBON_FAKE_ROOT"])
    if (not result and threading.current_thread() is threading.main_thread()
            and (root / "hold-idle-probe").exists()
            and not (root / "idle-probe-entered").exists()):
        atomic_json(root / "idle-probe-entered", readiness(sessions))
        deadline = time.monotonic() + 10
        while not (root / "release-idle-probe").exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Test did not release the idle probe")
            time.sleep(.01)
    return result
''')
        first = self.launch()
        record = runtime.discover(self.database)
        thread = self.api(first["url"], "api/browser/sessions", {"cwd":str(self.root)})["session"]["thread_id"]
        self.api(first["url"], f"api/browser/sessions/{thread}/messages", {"message":"one"})
        self.wait(lambda: "pending approvals or questions" in runtime.control(record, "status")["busy"])
        key = self.api(first["url"], f"api/browser/sessions/{thread}")["requests"][0]["key"]
        self.assertTrue(runtime.control(record, "decision", target=runtime.compatibility_metadata("2"))["busy"])
        new = self.root / "New/Contents/Resources"
        shutil.copytree(self.resources, new)
        page = new / "backend/agent_coord/web/index.html"
        page.write_text(page.read_text() + "\n<!-- new-interface-marker -->")
        (self.root / "hold-idle-probe").touch()
        second = self.launch(new)
        self.assertEqual(second["instance"], first["instance"])
        self.assertTrue(second["pending"])
        with urllib.request.urlopen(first["url"]) as response:
            self.assertIn(b"new-interface-marker", response.read())
        try:
            entered = self.root / "idle-probe-entered"
            self.wait(entered.exists)
            self.assertIn("pending approvals or questions", json.loads(entered.read_text()))
            self.api(first["url"], f"api/browser/sessions/{thread}/requests/{key}", {"decision":"accept"})
        finally:
            (self.root / "release-idle-probe").touch()
        def changed():
            try:
                value = runtime.discover(self.database)
                return value if value and value["instance"] != first["instance"] else None
            except (RuntimeError, OSError):
                return None
        new_record = self.wait(changed, timeout=20)
        self.assertEqual(new_record["payload"], second["pending"])
        self.assertNotEqual(new_record["pid"], first["pid"])
        self.assertTrue(runtime.control(new_record, "status")["status"] == "serving")

    def test_rollback_attach_cancels_pending_payload_and_corrupt_candidate_preserves_owner(self):
        first = self.launch()
        record = runtime.discover(self.database)
        runtime.control(record, "pause")
        new = self.root / "New/Contents/Resources"
        shutil.copytree(self.resources, new)
        (new / "backend/agent_coord/invalid_candidate.py").write_text("broken syntax !!!")
        second = self.launch(new)
        self.assertTrue(second["pending"])
        rollback = self.launch()
        self.assertIsNone(rollback["pending"])
        second = self.launch(new)
        runtime.control(record, "resume")
        status = self.wait(lambda: (s if (s := runtime.control(record, "status")).get("upgrade_error") else None))
        self.assertIn("Candidate validation failed", status["upgrade_error"])
        self.assertEqual(status["instance"], first["instance"])
        self.assertFalse(status["paused"])

    def test_schema_one_runtime_stages_schema_two_until_idle_then_rejects_downgrade(self):
        legacy = self.root / "Legacy/Contents/Resources"
        shutil.copytree(self.resources, legacy)
        source = legacy / "backend/agent_coord/desktop_runtime.py"
        source.write_text(source.read_text().replace("SCHEMA_VERSION = 2", "SCHEMA_VERSION = 1"))
        metadata = json.loads((legacy / "backend.json").read_text())
        metadata["runtime_schema_max"] = 1
        (legacy / "backend.json").write_text(json.dumps(metadata))
        first = self.launch(legacy, paused=True)
        record = runtime.discover(self.database)
        self.assertEqual(record["schema"], 1)
        candidate_metadata = json.loads((self.resources / "backend.json").read_text())
        candidate_metadata["runtime_version"] = "2+2"
        (self.resources / "backend.json").write_text(json.dumps(candidate_metadata))
        second = self.launch()
        self.assertEqual(second["instance"], first["instance"])
        self.assertTrue(second["pending"])
        self.assertEqual(runtime.read_json(runtime.runtime_directory(self.database) / "schema.json")["schema"], 1)
        runtime.control(record, "resume")
        def upgraded():
            try:
                current = runtime.discover(self.database)
                return current if current and current["instance"] != first["instance"] else None
            except (OSError, RuntimeError):
                return None
        current = self.wait(upgraded, timeout=20)
        self.assertEqual(current["schema"], 2)
        self.assertEqual(runtime.read_json(runtime.runtime_directory(self.database) / "schema.json")["schema"], 2)
        with patch.object(runtime, "SCHEMA_VERSION", 1):
            with self.assertRaisesRegex(CoordinationError, "downgrade is unsafe"):
                CoordinationStore(self.database)

    def test_failed_candidate_start_restores_retained_payload(self):
        first = self.launch()
        record = runtime.discover(self.database)
        runtime.control(record, "pause")
        new = self.root / "New/Contents/Resources"
        shutil.copytree(self.resources, new)
        entry = new / "backend.py"
        entry.write_text("import sys\nif '--runtime-serve' in sys.argv: raise SystemExit(9)\n" + entry.read_text().replace("from __future__ import annotations", ""))
        self.launch(new)
        runtime.control(record, "resume")
        status = self.wait_update("rolled-back")
        self.assertNotEqual(status["pid"], first["pid"])
        self.assertEqual(status["payload"], first["payload"])
        self.assertIn("previous coordinator restored", status["update"]["error"])

    def test_failed_candidate_after_schema_advance_restores_snapshot(self):
        first = self.launch(paused=True)
        record = runtime.discover(self.database)
        new = self.root / "New/Contents/Resources"
        shutil.copytree(self.resources, new)
        source = new / "backend/agent_coord/desktop_runtime.py"
        source.write_text(source.read_text().replace("SCHEMA_VERSION = 2", "SCHEMA_VERSION = 3"))
        metadata = json.loads((new / "backend.json").read_text())
        metadata.update(runtime_version="3+3", runtime_schema_max=3)
        (new / "backend.json").write_text(json.dumps(metadata))
        entry = new / "backend.py"
        entry.write_text("import sys\nif '--runtime-serve' in sys.argv:\n"
                         "    from pathlib import Path\n"
                         "    from agent_coord import desktop_runtime\n"
                         "    database = sys.argv[sys.argv.index('--db') + 1]\n"
                         "    desktop_runtime._OWNED_DATABASES.add(str(Path(database).resolve()))\n"
                         "    from agent_coord.store import CoordinationStore\n"
                         "    CoordinationStore(database)\n"
                         "    raise SystemExit(9)\n" +
                         entry.read_text().replace("from __future__ import annotations", ""))
        self.launch(new)
        runtime.control(record, "resume")
        status = self.wait_update("rolled-back")
        self.assertEqual(status["payload"], first["payload"])
        self.assertEqual(runtime.read_json(runtime.runtime_directory(self.database) / "schema.json")["schema"], runtime.SCHEMA_VERSION)
        self.assertTrue(status["update"]["backup"])

    def test_idle_handover_adopts_accepted_queued_work_once(self):
        first = self.launch()
        record = runtime.discover(self.database)
        url = first["url"]
        thread = self.api(url, "api/browser/sessions", {"cwd":str(self.root)})["session"]["thread_id"]
        self.api(url, f"api/browser/sessions/{thread}/messages", {"message":"one"})
        self.wait(lambda: "pending approvals or questions" in runtime.control(record, "status")["busy"])
        self.api(url, f"api/browser/sessions/{thread}/queue", {"message":"two"})
        self.api(url, f"api/browser/sessions/{thread}/queue", {"message":"three"})
        key = self.api(url, f"api/browser/sessions/{thread}")["requests"][0]["key"]
        self.api(url, f"api/browser/sessions/{thread}/requests/{key}", {"decision":"accept"})
        runtime.control(record, "pause")
        self.wait(lambda: not runtime.control(record, "status")["busy"])
        self.assertEqual(len([x for x in self.calls() if x.get("method") == "turn/start"]), 1)
        new = self.root / "New/Contents/Resources"
        shutil.copytree(self.resources, new)
        (new / "new-version").write_text("new")
        staged = self.launch(new)
        runtime.control(record, "resume")
        def finished():
            try:
                value = runtime.discover(self.database)
                if value and value["instance"] != first["instance"]:
                    calls = [x for x in self.calls() if x.get("method") == "turn/start"]
                    return value if len(calls) == 3 and not runtime.control(value, "status")["busy"] else None
            except (RuntimeError, OSError):
                pass
        new_record = self.wait(finished, timeout=20)
        self.assertEqual(new_record["payload"], staged["pending"])
        self.assertEqual([x["params"]["input"][0]["text"] for x in self.calls() if x.get("method") == "turn/start"], ["one", "two", "three"])

    def test_paused_http_admission_rejects_before_accepting_work(self):
        first = self.launch()
        record = runtime.discover(self.database)
        runtime.control(record, "pause")
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            self.api(first["url"], "api/browser/sessions", {"cwd":str(self.root)})
        self.assertEqual(rejected.exception.code, 503)
        rejected.exception.close()
        self.assertFalse((self.root / "provider.pid").exists())
        runtime.control(record, "resume")
        self.assertTrue(self.api(first["url"], "api/browser/sessions", {"cwd":str(self.root)})["session"])

    def test_stale_record_never_signals_unrelated_process(self):
        with subprocess.Popen(["/bin/sleep", "30"]) as unrelated:
            try:
                directory = runtime.runtime_directory(self.database)
                runtime.atomic_json(directory / "discovery.json", {"pid":unrelated.pid, "instance":"stale", "database":str(self.database.resolve()),
                                    "control_url":"http://127.0.0.1:1/", "control_token":"wrong"})
                live = self.launch()
                self.assertNotEqual(live["pid"], unrelated.pid)
                self.assertIsNone(unrelated.poll())
            finally:
                unrelated.terminate()
                unrelated.wait(timeout=5)

    def test_maintenance_bootstrap_leaves_scheduled_start_pending_until_resume(self):
        first = self.launch(paused=True)
        record = runtime.discover(self.database)
        self.assertTrue(first["paused"])
        # Simulate an external scheduling request accepted while maintenance is
        # active; the saved item must not be claimed by a background consumer.
        import sqlite3
        now = time.time()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("""INSERT INTO scheduled_prompts
                (id,cwd,settings_json,message,run_at,timezone,state,request_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,'scheduled','{}',?,?)""", ("maintenance-test", str(self.root.resolve()),
                json.dumps({"cwd":str(self.root.resolve()),"client":"codex","model":None,"effort":None,"yolo":False}),
                "scheduled", now+.2, "UTC", now, now))
            db.commit()
        time.sleep(1.2)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT state,owner FROM scheduled_prompts WHERE id='maintenance-test'").fetchone(), ("scheduled",None))
        self.assertFalse((self.root / "provider.pid").exists())
        runtime.control(record, "resume")
        self.wait(lambda: (self.root / "calls.jsonl").exists() and any(x.get("method") == "turn/start" for x in self.calls()))

    def test_stop_proof_matches_instance_and_is_invalidated_by_next_start(self):
        first = self.launch()
        record = runtime.discover(self.database)
        runtime.control(record, "stop")
        directory = runtime.runtime_directory(self.database)
        self.wait(lambda: runtime.owner_available(directory))
        proof = runtime.read_json(directory / "stop-proof.json")
        self.assertEqual(proof["instance"], first["instance"])
        def decide(identity):
            return subprocess.run([sys.executable, str(self.resources / "backend.py"), "--db", str(self.database), "--runtime-command", "decision"],
                                  input=json.dumps({"target":runtime.compatibility_metadata("2"), "stopped_instance":identity}),
                                  text=True, capture_output=True, env=self.env, timeout=5)
        self.assertEqual(decide(first["instance"]).returncode, 0)
        self.assertNotEqual(decide("other-instance").returncode, 0)
        second = self.launch(paused=True)
        self.assertFalse((directory / "stop-proof.json").exists())
        # A signal stop is not an authenticated explicit-stop proof.
        os.kill(second["pid"], 15)
        def recovered():
            try:
                record = runtime.discover(self.database)
                return record if record and record["pid"] != second["pid"] else None
            except (OSError, RuntimeError):
                return None
        current = self.wait(recovered)
        self.assertFalse((directory / "stop-proof.json").exists())
        self.assertFalse(runtime.owner_available(directory))
        decision = decide(first["instance"])
        self.assertEqual(decision.returncode, 0, decision.stderr)
        self.assertEqual(json.loads(decision.stdout)["instance"], current["instance"])
        self.assertEqual(json.loads(decision.stdout)["status"], "serving")


class AdmissionTests(unittest.TestCase):
    def test_provider_becoming_unsupported_during_drain_reopens_admission(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = runtime.Runtime(Path(temporary) / "state.sqlite3", Path(temporary) / "previous", -1)
            owner.pending = Path(temporary) / "candidate"
            sessions = SimpleNamespace(admission=runtime.AdmissionGate(), lock=threading.RLock(),
                active={}, loaded_clients={}, projection=SimpleNamespace(error=None),
                muse=SimpleNamespace(), queue=SimpleNamespace(wake=Mock()), dispatch=SimpleNamespace(wake=Mock()))
            owner.server = SimpleNamespace(browser_sessions=sessions)
            owner.request_update("unsupported-during-drain", str(owner.pending))
            with sessions.admission.enter() as allowed:
                self.assertTrue(allowed)
                self.assertFalse(owner.prepare_handover())
                self.assertTrue(sessions.admission.paused)
                # The admitted request starts work before it returns. That
                # provider cannot transfer and must remain answerable.
                sessions.active["kimi-thread"] = "native-turn"
                sessions.loaded_clients["kimi-thread"] = "kimi"
                self.assertFalse(owner.prepare_handover())
                self.assertFalse(sessions.admission.paused)
                self.assertEqual(owner.update_requested["phase"], "accepted")
                self.assertIsNone(owner.update_drain_started)
            sessions.active.clear()
            self.assertTrue(owner.prepare_handover())
            self.assertTrue(sessions.admission.paused)

    def test_pause_excludes_new_entries_but_admitted_operation_and_nested_calls_finish(self):
        gate = runtime.AdmissionGate()
        entered, release = threading.Event(), threading.Event()
        outcome = []
        def worker():
            with gate.enter() as allowed:
                outcome.append(allowed)
                entered.set()
                release.wait(2)
                with gate.enter() as nested:
                    outcome.append(nested)
        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(entered.wait(2))
        self.assertEqual(gate.pause(), 1)
        with gate.enter() as admitted:
            self.assertFalse(admitted)
        release.set()
        thread.join(2)
        self.assertEqual(outcome, [True, True])
        self.assertEqual(gate.inflight, 0)

    def test_every_automatic_starter_and_user_send_share_admission(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CoordinationStore(Path(temporary) / "state.sqlite3")
            sessions = BrowserSessions(store, temporary, rpc_factory=FakeCodex, claude_factory=FakeCodex,
                                       antigravity_factory=FakeCodex, kimi_factory=FakeCodex, muse_factory=FakeCodex)
            try:
                sessions.admission.pause()
                for worker in (sessions.schedules, sessions.inbox_wake, sessions.event_subscriptions,
                               sessions.dispatch, sessions.reviews, sessions.sidecars, sessions.watchdog):
                    self.assertFalse(worker.process_once())
                self.assertFalse(sessions.queue._dispatch("missing"))
                with self.assertRaises(BrowserBusyError):
                    sessions.create({"cwd":temporary})
                with self.assertRaises(BrowserBusyError):
                    sessions.send("missing", {"message":"never accepted"})
                self.assertEqual(sessions.rpc.calls, [])
            finally:
                sessions.close()


if __name__ == "__main__":
    unittest.main()
