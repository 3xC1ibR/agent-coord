import copy
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from test_codex_app_server import BrowserSessions, CoordinationStore, FakeCodex
from agent_coord.timeline_clock import preserve_timing, restore_timing, seconds


class TimelineClockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.now = 100.
        self.store = CoordinationStore(self.root / 'state.sqlite3', clock=lambda: self.now)
        self.env = patch.dict(os.environ, {'CLAUDE_CONFIG_DIR':str(self.root / 'claude'), 'CODEX_HOME':str(self.root / 'codex')})
        self.env.start()
        self.addCleanup(self.env.stop)

    def transcript(self, client, sid, records):
        path = (self.root / 'claude/projects/project' / (sid + '.jsonl') if client == 'claude' else
                self.root / 'codex/sessions/day' / ('rollout-' + sid + '.jsonl'))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps(record) for record in records) + '\npartial record')

    def test_claude_legacy_turns_match_user_ids_not_similar_prompts(self):
        sid = str(uuid.uuid4())
        self.store.register(session_id=sid, client='claude', cwd=str(self.root))
        history = {'id':sid, 'turns':[{'id':'turn-a', 'startedAt':None, 'items':[
            {'id':'user-a', 'type':'userMessage'}, {'id':'tool-a','type':'mcpToolCall'}]},
            {'id':'turn-b','items':[{'id':'user-b','type':'userMessage'}]}]}
        self.transcript('claude', sid, [
            {'type':'user','uuid':'user-a','timestamp':'2026-10-08T19:00:00Z'},
            {'type':'user','uuid':'unrelated','timestamp':'2026-10-08T19:01:00Z'},
            {'type':'assistant','timestamp':'2026-10-08T19:02:00Z','message':{'content':[{'type':'tool_use','id':'tool-a'}]}},
            {'type':'user','uuid':'user-b','timestamp':'2026-10-08T21:35:00Z'},
        ])
        restore_timing(self.store, history)
        a, b = history['turns']
        self.assertEqual(a['startedAt'], seconds('2026-10-08T19:00:00Z'))
        self.assertEqual(a['items'][1]['timelineAt'], seconds('2026-10-08T19:02:00Z'))
        self.assertGreater(b['startedAt'], a['startedAt'])
        self.assertEqual(b['items'][0]['timelineAt'], b['startedAt'])

    def test_codex_native_start_survives_context_updates(self):
        sid = str(uuid.uuid4())
        self.store.register(session_id=sid, client='codex', cwd=str(self.root))
        history = {'id':sid,'turns':[{'id':'turn-a','items':[]}]}
        self.transcript('codex', sid, [
            {'type':'event_msg','timestamp':'2026-10-08T19:00:00Z','payload':{'type':'task_started','turn_id':'turn-a'}},
            {'type':'turn_context','timestamp':'2026-10-08T19:01:00Z','payload':{'turn_id':'turn-a'}},
        ])
        restore_timing(self.store, history)
        self.assertEqual(history['turns'][0]['startedAt'], seconds('2026-10-08T19:00:00Z'))

    def test_missing_transcript_uses_confirmed_current_turn_and_completion_only(self):
        sid = str(uuid.uuid4())
        self.store.register(session_id=sid, client='claude', cwd=str(self.root))
        self.store.threads.start_turn(sid, turn_id='known')
        self.now = 120
        self.store.threads.finish_turn(sid, turn_id='known')
        history={'id':sid,'turns':[{'id':'unknown','items':[]},{'id':'known','items':[]}]}
        restore_timing(self.store, history)
        self.assertNotIn('startedAt', history['turns'][0])
        self.assertEqual(history['turns'][1]['startedAt'], 100)
        self.assertEqual(history['turns'][1]['completedAt'], 120)

    def test_stream_timing_survives_completion_and_timestamp_free_provider_reads(self):
        sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex)
        self.addCleanup(sessions.close)
        sid = sessions.create({})['session']['thread_id']
        events = sessions.sequence
        sessions.send(sid, {'message':'First request'})
        self.now = 110
        sessions._event({'method':'item/started','params':{'threadId':sid,'turnId':'turn-0',
                        'item':{'id':'answer','type':'agentMessage','text':'Answer'}}})
        self.now = 120
        sessions._event({'method':'item/completed','params':{'threadId':sid,'turnId':'turn-0',
                        'item':{'id':'answer','type':'agentMessage','text':'Full answer'}}})
        self.now = 130
        snapshot={'id':sid,'turns':[{'id':'turn-0','status':'completed','items':[
            {'id':'user','type':'userMessage','content':[{'type':'text','text':'First request'}]},
            {'id':'answer','type':'agentMessage','text':'Full answer'}]}]}
        sessions._event({'method':'turn/completed','params':{'threadId':sid,'turn':copy.deepcopy(snapshot['turns'][0])}})
        restored = sessions._remember_thread(snapshot)['turns'][0]
        self.assertEqual(restored['startedAt'], 100)
        self.assertEqual(restored['completedAt'], 130)
        self.assertEqual([i['timelineAt'] for i in restored['items']], [100,110])
        stream=sessions.events_after(events, timeout=0)['events']
        start=next(e for e in stream if e['method']=='turn/started')
        self.assertEqual(start['params']['turn']['startedAt'],100)
        item=next(e for e in stream if e['method']=='item/completed')
        self.assertEqual(item['params']['item']['timelineAt'],110)
        self.assertEqual(item['params']['turnStartedAt'],100)
        with self.store._connection() as db:
            persisted=json.loads(db.execute('SELECT history_json FROM browser_history WHERE thread_id=?',(sid,)).fetchone()[0])
        self.assertEqual(persisted['turns'][0]['startedAt'],100)

    def test_preservation_does_not_reuse_timing_for_unrelated_items_or_turns(self):
        saved={'turns':[{'id':'a','startedAt':100,'items':[{'id':'old','timelineAt':105}]}]}
        fresh={'turns':[{'id':'a','items':[{'id':'new'}]},{'id':'b','items':[{'id':'old'}]}]}
        preserve_timing(fresh,saved)
        self.assertEqual(fresh['turns'][0]['startedAt'],100)
        self.assertNotIn('timelineAt',fresh['turns'][0]['items'][0])
        self.assertNotIn('startedAt',fresh['turns'][1])
        self.assertNotIn('timelineAt',fresh['turns'][1]['items'][0])

    def test_invalid_time_is_not_a_sort_key(self):
        for value in (None, '', True, float('nan'), float('inf'), 'invalid'):
            self.assertIsNone(seconds(value))
        self.assertEqual(seconds(1791491768000),1791491768)
