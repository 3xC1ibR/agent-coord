from contextlib import closing
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from urllib.parse import quote
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/agent-coord/scripts"))
from agent_coord.coordinator_projection import pack
from agent_coord.desktop_runtime import runtime_directory
from agent_coord.history_persistence import prepare_history, publish_history, stage_history, ensure_schema
from agent_coord.runtime_saved_views import SavedRuntimeViews
from agent_coord.schedules import ScheduledPrompts
from agent_coord.thread_inventory import ThreadInventory
from agent_coord.store import CoordinationError, CoordinationStore
from agent_coord.views import ViewStore


class SavedViewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.other = self.root / "other"
        self.other.mkdir()
        self.path = self.root / "state.sqlite3"
        self.store = CoordinationStore(self.path)
        self.identity = "café thread 雪"
        self.route = "sessions/" + quote(self.identity, safe="")
        with self.store._connection() as db:
            db.executescript("""
                CREATE TABLE browser_sessions(thread_id TEXT PRIMARY KEY,cwd TEXT,client TEXT,yolo INTEGER,
                    permission_mode TEXT, name TEXT,archived INTEGER DEFAULT 0);
                CREATE TABLE browser_history(thread_id TEXT PRIMARY KEY, history_json TEXT);
                CREATE TABLE coordinator_projection(singleton INTEGER PRIMARY KEY,version INTEGER,state BLOB);
                CREATE TABLE coordinator_projection_payloads(digest TEXT PRIMARY KEY, compressed BLOB);
                CREATE TABLE sidecar_watches(id TEXT, target_id TEXT, sidecar_id TEXT, created_at REAL);
            """)
            ensure_schema(db)
        for identity, cwd in ((self.identity, self.cwd), ("hidden", self.other)):
            self.store.register(session_id=identity, cwd=str(cwd), client="codex", name="Inventory " + identity)
            with self.store._connection() as db:
                db.execute("INSERT INTO browser_sessions VALUES(?,?,?,0,?,?,0)",
                           (identity, str(cwd), "codex", "read-only", "Inventory " + identity))
                db.execute("INSERT INTO browser_history VALUES(?,?)", (identity, json.dumps({
                    "id": identity, "turns": [{"id": "last", "status": "completed",
                    "items": [{"type": "agentMessage", "text": "visible", "id": "note"}]}]})))
        self.views = SavedRuntimeViews(self.path, str(self.cwd))
        self.addCleanup(self.views.close)

    def test_new_window_can_read_previously_unopened_unicode_thread_without_writes(self):
        with closing(sqlite3.connect(self.path)) as audit:
            before = audit.execute("PRAGMA data_version").fetchone()
            session = self.views.get(self.route, recent=True)
            self.assertEqual(session["thread"]["turns"][-1]["id"], "last")
            self.assertFalse(session["requestsAvailable"])
            self.assertEqual(session["session"]["permission_mode"], "read-only")
            key = quote(self.identity, safe="")
            self.assertEqual(self.views.get("threads/" + key)["thread_id"], self.identity)
            self.assertEqual(self.views.get("threads/" + key + "/sidecars")["watches"], [])
            self.assertEqual([t["thread_id"] for t in self.views.get("threads")["data"]], [self.identity])
            result = self.views.get("search", {"q": ["Inventory"]})
            self.assertEqual([t["thread_id"] for t in result["items"]], [self.identity])
            for route in ("sessions/hidden", "threads/hidden", "threads/hidden/sidecars"):
                with self.assertRaises(CoordinationError):
                    self.views.get(route)
            self.assertEqual(audit.execute("PRAGMA data_version").fetchone(), before)
        with self.views.store._connection() as db, self.assertRaises(sqlite3.DatabaseError):
            db.execute("DELETE FROM browser_sessions")

    def test_recent_and_complete_responses_keep_last_saved_turn_and_bound_large_tool_output(self):
        history = {"id": self.identity, "turns": [
            {"id": str(i), "status": "completed", "items": [{"id": str(i), "type": "commandExecution",
             "aggregatedOutput": "a" * 300000}]} for i in range(50)]}
        prepared = prepare_history(history)
        stage_history(self.store, prepared)
        with self.store._connection() as db:
            publish_history(db, self.identity, prepared)
        recent = self.views.get(self.route, recent=True)["thread"]
        self.assertEqual(recent["turns"][-1]["id"], "49")
        self.assertLess(len(recent["turns"]), 50)
        self.assertLess(len(json.dumps(recent)), 100000)
        full = self.views.get(self.route)["thread"]
        self.assertEqual(full, history)

    def test_retains_only_selected_thread_pending_request_without_claiming_it(self):
        long_question = "q" * 65536
        digest = hashlib.sha256(long_question.encode()).hexdigest()
        request = {"id": 42, "key": "saved-approval", "method": "item/fileChange/requestApproval",
                   "params": {"threadId": self.identity, "question": long_question}, "notification_claimed": True}
        hidden_request = {"id": 43, "key": "other", "method": "item/fileChange/requestApproval", "params": {"threadId": "hidden"}}
        state = {"browser": {"active": {self.identity: "turn"}, "requests": {"k": request, "h": hidden_request}}}
        blob = zlib.compress(json.dumps(pack(state, lambda text: digest)).encode())
        with self.store._connection() as db:
            db.execute("INSERT INTO coordinator_projection VALUES(1,3,?)", (blob,))
            db.execute("INSERT INTO coordinator_projection_payloads VALUES(?,?)", (digest,zlib.compress(long_question.encode())))
        for _ in range(2):
            saved = self.views.get(self.route, recent=True, thread_controls=True)
            self.assertEqual(saved["work_thread"]["close_when_done"]["status"], "none")
            self.assertEqual(saved["activeTurn"], "turn")
            item, = saved["requests"]
            self.assertEqual((item["key"], item["params"]["question"]), ("saved-approval",long_question))
            self.assertNotIn("id", item)
            self.assertNotIn("notification_claimed", item)

    def test_previous_compatible_layout_is_read_only_and_does_not_attempt_schema_migration(self):
        marker = "a" * 64
        metadata = runtime_directory(self.path) / "schema.json"
        record = json.loads(metadata.read_text())
        record["fingerprint"] = marker
        metadata.write_text(json.dumps(record))
        with self.store._connection() as db:
            db.execute("UPDATE coordination_schema SET fingerprint=?", (marker,))
            db.execute("DROP TABLE app_agent_stall_incidents")
        with patch.object(CoordinationStore, "__init__", side_effect=AssertionError("cross-version migrations are forbidden")):
            previous = SavedRuntimeViews(self.path, str(self.cwd))
            self.addCleanup(previous.close)
            self.assertEqual(previous.get(self.route, recent=True)["thread"]["turns"][-1]["id"], "last")
            self.assertEqual(len(previous.get("threads")["data"]), 1)
            with previous.store._connection() as db, self.assertRaises(sqlite3.DatabaseError):
                db.execute("DELETE FROM browser_sessions")
        record["schema"] = 99
        metadata.write_text(json.dumps(record))
        with self.assertRaisesRegex(CoordinationError, "Unsupported"):
            SavedRuntimeViews(self.path)

    def save_projection(self, *, active=None, requests=None, extra=None):
        state = {"browser": {"active": active or {}, "requests": requests or {}}, "extra": extra}
        blob = zlib.compress(json.dumps(pack(state)).encode())
        with self.store._connection() as db:
            db.execute("INSERT OR REPLACE INTO coordinator_projection(singleton,version,state) VALUES(1,1,?)", (blob,))

    def test_untargeted_input_can_queue_while_a_native_turn_start_awaits_its_identity(self):
        message = {"message": "send after pending messages", "requestId": "next"}
        self.save_projection(active={self.identity: None})
        self.assertTrue(self.views.authorize_composer(self.identity, message))
        with self.assertRaisesRegex(CoordinationError, "active turn changed"):
            self.views.authorize_composer(self.identity, message, legacy_delivery=True)
        with self.assertRaisesRegex(CoordinationError, "active turn changed"):
            self.views.authorize_composer(self.identity, dict(message, expectedTurnId="previous"))
        self.save_projection(active={self.identity: "ready"})
        self.assertTrue(self.views.authorize_composer(self.identity, message, legacy_delivery=True))

    def test_admission_rejects_mismatched_turn_approval_closed_routing_and_unsupported_input(self):
        body = {"message":"continue", "requestId":"one", "expectedTurnId":"running"}
        self.save_projection(active={self.identity:"running"})
        self.assertTrue(self.views.authorize_thread(self.identity))
        self.assertTrue(self.views.authorize_composer(self.identity, body))
        self.assertTrue(self.views.authorize_composer(self.identity, dict(body, expectedTurnId="other"),
                                                      receiver_owns_turn=True))
        for invalid in (dict(body, expectedTurnId="other"),dict(body, review={}),dict(body, permission_mode="full-access"),
                        dict(body, message="/yolo"),dict(body, windowId="bad"),dict(body,activity={"event_id":None}),
                        dict(body,message=" "), dict(body,requestId=""), dict(body,mentions=[{"session_id":"hidden"}])):
            with self.assertRaises(CoordinationError):
                self.views.authorize_composer(self.identity, invalid)
        with self.assertRaises(CoordinationError):
            self.views.authorize_thread("hidden")
        with self.store._connection() as db:
            db.execute("CREATE TABLE dispatch_settings(thread_id TEXT, enabled INTEGER, defaults_json TEXT)")
            db.execute("INSERT INTO dispatch_settings VALUES(?,1,'{}')",(self.identity,))
        with self.assertRaisesRegex(CoordinationError, "Routed"):
            self.views.authorize_composer(self.identity, body)
        with self.store._connection() as db:
            db.execute("DELETE FROM dispatch_settings")
            db.execute("UPDATE browser_sessions SET archived=1 WHERE thread_id=?",(self.identity,))
        with self.assertRaisesRegex(CoordinationError, "Reopen"):
            self.views.authorize_composer(self.identity, body)
        with self.store._connection() as db:
            db.execute("UPDATE browser_sessions SET archived=0 WHERE thread_id=?",(self.identity,))
        req = {"method":"item/tool/requestUserInput","params":{"threadId":self.identity},"key":"k"}
        self.save_projection(active={self.identity:"running"},requests={"k":req})
        with self.assertRaisesRegex(CoordinationError, "decision is pending"):
            self.views.authorize_composer(self.identity, body)
        with self.assertRaisesRegex(CoordinationError, "decision is pending"):
            self.views.authorize_composer(self.identity, dict(body, expectedTurnId="other"), receiver_owns_turn=True)
        self.assertEqual(self.views.get(self.route)["requests"],[req])
        self.save_projection(active={self.identity:"running"})
        self.assertTrue(self.views.authorize_composer(self.identity, body))
        with self.store._connection() as db:
            db.execute("DELETE FROM coordinator_projection")
        with self.assertRaisesRegex(CoordinationError,"unavailable"):
            self.views.authorize_composer(self.identity,body)

    @unittest.skipUnless(hasattr(sqlite3.Connection, "blobopen"), "requires incremental blob reads")
    def test_large_changing_output_does_not_delay_or_hide_a_new_approval(self):
        import base64
        body = {"message":"continue","requestId":"input","expectedTurnId":"live"}
        request = {"key":"permission","method":"item/fileChange/requestApproval","params":{"threadId":self.identity}}
        for requests in ({}, {"permission":request}, {}):
            state = {"version":1, "browser":{"active":{self.identity:"live"},"requests":requests},
                     "claude":{"turns":base64.b64encode(os.urandom(512*1024)).decode()}}
            blob = zlib.compress(json.dumps(pack(state),separators=(",",":")).encode())
            with self.store._connection() as db:
                db.execute("INSERT OR REPLACE INTO coordinator_projection VALUES(1,1,?)",(blob,))
            with patch("agent_coord.runtime_saved_views.zlib.decompress", side_effect=AssertionError("decoded unrelated tool output")):
                if requests:
                    with self.assertRaisesRegex(CoordinationError, "decision is pending"):
                        self.views.authorize_composer(self.identity,body)
                else:
                    self.assertTrue(self.views.authorize_composer(self.identity,body))

    def test_unrelated_commits_and_identical_packed_rows_reuse_the_decoded_input(self):
        self.save_projection(active={self.identity: "running"})
        body = {"message": "continue", "requestId": "input", "expectedTurnId": "running"}
        with patch("agent_coord.runtime_saved_views.zlib.decompress", wraps=zlib.decompress) as decode:
            self.assertTrue(self.views.authorize_composer(self.identity, body))
            for revision in range(8):
                with self.store._connection() as db:
                    db.execute("UPDATE browser_sessions SET name=? WHERE thread_id=?", (str(revision), "hidden"))
                self.assertTrue(self.views.authorize_composer(self.identity, body))
            self.save_projection(active={self.identity: "running"})
            self.assertTrue(self.views.authorize_composer(self.identity, body))
            self.assertEqual(decode.call_count, 1)
            with self.store._connection() as db:
                db.execute("UPDATE coordinator_projection SET version=99 WHERE singleton=1")
            with self.assertRaisesRegex(CoordinationError, "unavailable"):
                self.views.authorize_composer(self.identity, body)

    def test_changed_packed_rows_at_one_cursor_immediately_require_then_release_approval(self):
        with self.store._connection() as db:
            db.execute("ALTER TABLE coordinator_projection ADD COLUMN cursor INTEGER NOT NULL DEFAULT 41")
        active = {self.identity: "running"}
        body = {"message": "continue", "requestId": "input", "expectedTurnId": "running"}
        request = {"key": "permission", "method": "item/fileChange/requestApproval",
                   "params": {"threadId": self.identity}}
        with patch("agent_coord.runtime_saved_views.zlib.decompress", wraps=zlib.decompress) as decode:
            self.save_projection(active=active)
            self.assertTrue(self.views.authorize_composer(self.identity, body))
            self.save_projection(active=active, requests={"permission": request})
            with self.assertRaisesRegex(CoordinationError, "decision is pending"):
                self.views.authorize_composer(self.identity, body)
            self.save_projection(active=active)
            self.assertTrue(self.views.authorize_composer(self.identity, body))
            self.assertEqual(decode.call_count, 3)
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT cursor FROM coordinator_projection").fetchone()[0], 41)

    def test_reads_share_committed_projection_and_file_replacement_invalidates_it(self):
        self.save_projection(active={self.identity:"one"},extra="step" * 3000000)
        body = {"message":"continue","requestId":"r","expectedTurnId":"one"}
        original = zlib.decompress
        with patch("agent_coord.runtime_saved_views.zlib.decompress", wraps=original) as decoded:
            with ThreadPoolExecutor(max_workers=8) as workers:
                accepted = list(workers.map(lambda _: self.views.authorize_composer(self.identity,body), range(32)))
            self.assertTrue(all(accepted))
            self.assertEqual(decoded.call_count,1)
            self.assertTrue(self.views.authorize_thread(self.identity))
            self.assertEqual(decoded.call_count,1)
        # Native rollback replaces the DB inode and removes its sidecars.
        # A previously open mode=ro connection must not keep old input eligible.
        replacement = self.root / 'replacement.sqlite3'
        with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(replacement)) as target:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
            req={"method":"item/fileChange/requestApproval","params":{"threadId":self.identity},"key":"p"}
            restored={"browser":{"active":{self.identity:"one"},"requests":{"p":req}}}
            target.execute("UPDATE coordinator_projection SET state=?", (zlib.compress(json.dumps(pack(restored)).encode()),))
            target.commit()
        for suffix in ("-shm","-wal"):
            Path(str(self.path)+suffix).unlink(missing_ok=True)
        os.replace(replacement,self.path)
        with self.assertRaisesRegex(CoordinationError, "decision is pending"):
            self.views.authorize_composer(self.identity,body)

    def test_fresh_window_can_restore_registered_folders_scoped_views_and_organization_without_writing(self):
        mine = str(self.cwd)
        other = str(self.other)
        view_store = ViewStore(self.store)
        mine_view = view_store.create({"name": "Current work", "cwd": mine})
        view_store.create({"name": "Other work", "cwd": other})
        view_store.create({"name": "Global work"})
        organization = self.store.threads.organization
        mine_project = organization.create_project("Retained group", mine)
        organization.create_project("Other group", other)
        for folder in (self.cwd, self.other):
            folder.joinpath(".git").mkdir()
            organization.add_repository(str(folder))
        new = self.cwd / "unknown"
        new.mkdir()
        (new / ".git").mkdir()
        with closing(sqlite3.connect(self.path)) as audit:
            before = audit.execute("PRAGMA data_version").fetchone()[0]
            self.assertEqual(self.views.get("views", {"cwd": [mine]})["data"], [mine_view])
            self.assertEqual(self.views.get("views")["data"], [mine_view])
            with self.assertRaises(CoordinationError):
                self.views.get("views", {"cwd": [other]})
            org = self.views.get("organization")
            self.assertEqual([r["id"] for r in org["projects"]], [mine_project["id"]])
            repo, = org["repositories"]
            self.assertEqual(repo["root"], mine)
            self.assertEqual(self.views.open_workspace({"path": mine})["repository"], repo)
            with self.assertRaisesRegex(CoordinationError, "Register this repository"):
                self.views.open_workspace({"path": str(new)})
            with self.assertRaises(CoordinationError):
                self.views.open_workspace({"path": other})
            workspaces = {r["cwd"] for r in self.views.get("workspaces")["data"]}
            self.assertIn(mine, workspaces)
            self.assertNotIn(other, workspaces)
            self.assertEqual(audit.execute("PRAGMA data_version").fetchone()[0], before)

    def test_legacy_unscoped_saved_views_are_only_visible_to_an_unrestricted_window(self):
        with self.store._connection() as db:
            db.execute("""CREATE TABLE saved_views(id TEXT,name TEXT,filters TEXT,group_by TEXT,position INT,version INT)""")
            db.execute("""INSERT INTO saved_views VALUES('all','Old view','{}','phase',0,1)""")
        self.assertEqual(self.views.get("views")["data"], [])
        global_views = SavedRuntimeViews(self.path)
        self.addCleanup(global_views.close)
        row, = global_views.get("views")["data"]
        self.assertEqual((row["name"], row["cwd"]), ("Old view", None))
        self.assertEqual(global_views.get("views", {"cwd": [str(self.cwd)]})["data"], [])

    def test_open_asks_preserve_scope_status_since_and_existing_decisions(self):
        asks = self.store.asks
        original = asks.create(self.identity, "Waiting for the user", blocking=True)
        prior = asks.create(self.identity, "Withdrawn decision", blocking=False, context="Proceed with the saved option.")
        asks.withdraw(prior["id"], self.identity)
        asks.create("hidden", "Outside workspace", blocking=True)
        with closing(sqlite3.connect(self.path)) as audit:
            before = audit.execute("PRAGMA data_version").fetchone()[0]
            result, = self.views.get("asks")["data"]
            self.assertEqual((result["id"], result["status"], result["blocking"], result["answer"]),
                             (original["id"], "open", True, None))
            resolved, = self.views.get("asks", {"status": ["withdrawn"]})["data"]
            self.assertEqual(resolved["id"], prior["id"])
            self.assertEqual(self.views.get("asks", {"since": ["2099-01-01T00:00:00Z"]})["data"], [])
            with self.assertRaises(CoordinationError):
                self.views.get("asks", {"status": ["bad"]})
            self.assertEqual(audit.execute("PRAGMA data_version").fetchone()[0], before)

    def test_fresh_window_reads_actual_schedule_state_and_last_run_without_a_worker(self):
        ScheduledPrompts(SimpleNamespace(store=self.store, cwd=None))
        with self.store._connection() as db:
            for identity, cwd in (("mine", self.cwd), ("other", self.other)):
                db.execute("""INSERT INTO scheduled_prompts(id,cwd,settings_json,message,run_at,timezone,state,
                              request_json,created_at,updated_at) VALUES(?,?,'{}','Retained prompt',3,'UTC',
                              'scheduled','{}',1,2)""", (identity, str(cwd)))
            db.execute("""INSERT INTO scheduled_prompt_runs(schedule_id,run_at,state,error,finished_at)
                       VALUES('mine',1,'failed','Saved failure',2)""")
        with closing(sqlite3.connect(self.path)) as audit:
            before = audit.execute("PRAGMA data_version").fetchone()[0]
            item, = self.views.get("schedules")["data"]
            self.assertEqual((item["id"], item["state"], item["last_run"]["error"]),
                             ("mine", "scheduled", "Saved failure"))
            self.assertEqual(audit.execute("PRAGMA data_version").fetchone()[0], before)

    def test_inventory_reuses_committed_batches_until_a_write_and_refreshes_the_visible_rows(self):
        with patch("agent_coord.runtime_saved_views.ThreadInventory", wraps=ThreadInventory) as inventory:
            self.assertEqual([r["thread_id"] for r in self.views.get("threads")["data"]], [self.identity])
            self.views.get("threads")
            self.assertEqual(inventory.call_count, 1)
            with self.store._connection() as db:
                db.execute("UPDATE work_threads SET title='New saved title' WHERE thread_id=?", (self.identity,))
            item, = self.views.get("threads")["data"]
            self.assertEqual(item["title"], "New saved title")
            self.assertEqual(inventory.call_count, 2)
            with self.store._connection() as db:
                db.execute("UPDATE work_threads SET attention='archived' WHERE thread_id=?", (self.identity,))
            self.assertEqual(self.views.get("threads")["data"], [])
            self.assertEqual(self.views.get("threads", {"archived": ["true"]})["data"][0]["thread_id"], self.identity)

    def test_message_search_only_uses_current_eligible_published_index_entries(self):
        with self.store._connection() as db:
            db.executescript("""
                CREATE TABLE conversation_search_heads(thread_id TEXT PRIMARY KEY,generation TEXT,revision INT);
                CREATE TABLE conversation_search_revisions(thread_id TEXT PRIMARY KEY,revision INT,indexed_revision INT);
                CREATE TABLE conversation_search_document_entries(generation TEXT,position INT,turn_id TEXT,
                    item_id TEXT,role TEXT,timestamp REAL,document TEXT,PRIMARY KEY(generation,position));
                CREATE TABLE conversation_search_documents(document TEXT PRIMARY KEY,excerpt TEXT);
            """)
            for i, identity in enumerate((self.identity, "hidden")):
                text = "alpha-indexed-message " + identity
                db.execute("INSERT INTO conversation_search_heads VALUES(?,?,1)",(identity,str(i)))
                db.execute("INSERT INTO conversation_search_revisions VALUES(?,1,1)",(identity,))
                db.execute("INSERT INTO conversation_search_document_entries VALUES(?,0,'turn','item','user',1,?)",(str(i),str(i)))
                db.execute("INSERT INTO conversation_search_documents VALUES(?,?)",(str(i),text))
        result = self.views.get('search', {'q':['alpha-indexed-message'],'my_messages':['true']})
        item, = result['items']
        self.assertEqual(item['thread_id'],self.identity)
        self.assertIn(self.identity,item['matches'][0]['excerpt'])
        self.assertTrue(result['coverage']['partial'])
        with self.store._connection() as db:
            db.execute("UPDATE conversation_search_revisions SET revision=2 WHERE thread_id=?",(self.identity,))
        self.assertEqual(self.views.get('search',{'q':['alpha-indexed-message']})['items'],[])
