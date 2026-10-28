from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent-coord/scripts"))
from agent_coord import desktop_runtime as runtime
from agent_coord.codex_app_server import BrowserBusyError, BrowserSessions
from agent_coord.store import CoordinationStore
from test_codex_app_server import FakeCodex

FAKE = r'''
import copy, json, os, pathlib, sys, threading, time
root = pathlib.Path(os.environ['RIBBON_FAKE_ROOT'])
(root / 'provider.pid').write_text(str(os.getpid()))
lock = threading.RLock()
approval = threading.Event()
threads = {}
counter = 0
def emit(value):
    with lock:
        print(json.dumps(value), flush=True)
def record(value):
    with lock:
        with (root / 'calls.jsonl').open('a') as f: f.write(json.dumps(value) + '\n')
def work(identity, turn, needs_approval):
    if needs_approval:
        for n in range(5):
            time.sleep(.1)
            emit({'method':'item/agentMessage/delta','params':{'threadId':identity,'turnId':turn['id'],'itemId':'output','delta':str(n)}})
        emit({'id':900,'method':'item/commandExecution/requestApproval','params':{'threadId':identity,'turnId':turn['id'],'itemId':'tool','command':'fake-tool'}})
        approval.wait(20)
    time.sleep(.15)
    item = {'id':'output','type':'agentMessage','text':'output while desktop disconnected'}
    turn['items'].append(item)
    turn['status'] = 'completed'
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
            with sqlite3.connect(self.database) as db:
                detail += "\nQUEUE: " + repr(db.execute("SELECT message,state,error,owner FROM browser_message_queue").fetchall())
        except sqlite3.Error:
            pass
        self.fail("Condition did not become true before timeout\n" + detail)

    def launch(self, resources=None, *, paused=False):
        resources = resources or self.resources
        result = subprocess.run([sys.executable, "-B", str(resources / "backend.py"), "--runtime-bootstrap",
                                 "--resources", str(resources), "--db", str(self.database)] + (["--runtime-start-paused"] if paused else []), env=self.env,
                                text=True, capture_output=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr + self.log())
        self.assertNotIn("ResourceWarning", result.stderr)
        return json.loads(result.stdout)

    def log(self):
        path = runtime.runtime_directory(self.database) / "runtime.log"
        return path.read_text() if path.exists() else ""

    def api(self, url, path, body=None):
        headers = {}
        if body is not None:
            headers = {"X-Agent-Coord-Token": self.api(url, "api/browser/config")["token"], "Content-Type":"application/json"}
        request = urllib.request.Request(url + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

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

    def test_busy_update_swaps_web_assets_then_activates_at_idle(self):
        first = self.launch()
        record = runtime.discover(self.database)
        thread = self.api(first["url"], "api/browser/sessions", {"cwd":str(self.root)})["session"]["thread_id"]
        self.api(first["url"], f"api/browser/sessions/{thread}/messages", {"message":"one"})
        self.wait(lambda: "pending approvals or questions" in runtime.control(record, "status")["busy"])
        new = self.root / "New/Contents/Resources"
        shutil.copytree(self.resources, new)
        page = new / "backend/agent_coord/web/index.html"
        page.write_text(page.read_text() + "\n<!-- new-interface-marker -->")
        second = self.launch(new)
        self.assertEqual(second["instance"], first["instance"])
        self.assertTrue(second["pending"])
        with urllib.request.urlopen(first["url"]) as response:
            self.assertIn(b"new-interface-marker", response.read())
        self.assertTrue(runtime.control(record, "decision", target=runtime.compatibility_metadata("2"))["busy"])
        key = self.api(first["url"], f"api/browser/sessions/{thread}")["requests"][0]["key"]
        self.api(first["url"], f"api/browser/sessions/{thread}/requests/{key}", {"decision":"accept"})
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

    def test_failed_candidate_start_restores_old_runtime_with_known_queue_owner(self):
        first = self.launch()
        record = runtime.discover(self.database)
        runtime.control(record, "pause")
        new = self.root / "New/Contents/Resources"
        shutil.copytree(self.resources, new)
        entry = new / "backend.py"
        entry.write_text("import sys\nif '--runtime-serve' in sys.argv: raise SystemExit(9)\n" + entry.read_text().replace("from __future__ import annotations", ""))
        self.launch(new)
        runtime.control(record, "resume")
        def restored():
            try:
                value = runtime.discover(self.database)
                if value:
                    status = runtime.control(value, "status")
                    return status if status.get("upgrade_error") else None
            except (RuntimeError, OSError):
                pass
        status = self.wait(restored, timeout=20)
        self.assertEqual(status["pid"], first["pid"])
        self.assertEqual(status["payload"], first["payload"])
        self.assertIn("retained runtime restored", status["upgrade_error"])

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
        with sqlite3.connect(self.database) as db:
            db.execute("""INSERT INTO scheduled_prompts
                (id,cwd,settings_json,message,run_at,timezone,state,request_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,'scheduled','{}',?,?)""", ("maintenance-test", str(self.root.resolve()),
                json.dumps({"cwd":str(self.root.resolve()),"client":"codex","model":None,"effort":None,"yolo":False}),
                "scheduled", now+.2, "UTC", now, now))
        time.sleep(1.2)
        with sqlite3.connect(self.database) as db:
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
        self.wait(lambda: runtime.owner_available(directory))
        self.assertNotEqual(decide(first["instance"]).returncode, 0)


class AdmissionTests(unittest.TestCase):
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
