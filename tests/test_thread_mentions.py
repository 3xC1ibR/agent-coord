from __future__ import annotations
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/agent-coord/scripts'))
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.store import CoordinationStore, CoordinationError
from agent_coord.thread_mentions import thread_mentions
from test_codex_app_server import FakeCodex
from test_browser_providers import claude_factory


class ThreadMentionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve(); self.now = 1800000000
        self.store = CoordinationStore(self.root/'state.sqlite3', clock=lambda:self.now)
        self.sessions = BrowserSessions(self.store, str(self.root), rpc_factory=FakeCodex, claude_factory=claude_factory)
        self.addCleanup(self.sessions.close)
        self.first = self.create(name='Same name')
        self.second = self.create(name='Same name')
        self.sessions.update(self.second, {'archived':True})
        self.text = '  Ask @Same name and @Same name. '
        self.refs = [{'start':6,'end':16,'text':'@Same name','session_id':self.first},
                     {'start':21,'end':31,'text':'@Same name','session_id':self.second}]
    def create(self, **values): return self.sessions.create(values)['session']['thread_id']
    def starts(self): return [p for m,p in self.sessions.rpc.calls if m=='turn/start']
    def payload(self, text):
        original, mapping = text.split('\n\n[Selected thread references; positions are UTF-16 offsets in the user text above]\n')
        self.assertEqual(original,self.text); self.assertEqual(json.loads(mapping),self.refs)
    def test_codex_start_and_steer_keep_exact_ids_after_rename_without_recipient_effects(self):
        self.sessions.update(self.first, {'name':'Renamed'})
        before=[self.store.threads.get(t) for t in (self.first,self.second)]
        recipient=self.create(name='Receiver')
        body={'message':self.text,'mentions':self.refs}
        self.sessions.send(recipient,body)
        self.payload(self.starts()[-1]['input'][0]['text'])
        self.sessions.send(recipient,body)
        self.payload([p for m,p in self.sessions.rpc.calls if m=='turn/steer'][-1]['input'][0]['text'])
        self.assertEqual(before,[self.store.threads.get(t) for t in (self.first,self.second)])
        self.assertEqual(self.store.inbox(self.first),[]);self.assertEqual(self.store.inbox(self.second),[])
        self.assertFalse(any(p.get('threadId') in (self.first,self.second) for m,p in self.sessions.rpc.calls if m in ('turn/start','turn/steer','thread/resume')))
    def test_actual_claude_connection_user_payload_contains_mapping(self):
        recipient=self.create(client='claude',name='Claude receiver')
        self.sessions.send(recipient, {'message':self.text,'mentions':self.refs})
        payload=self.sessions.claude.connections[recipient].writes[-1]
        self.assertEqual(payload['type'],'user')
        self.payload(payload['message']['content'][0]['text'])
        self.assertEqual(self.store.threads.get(self.second)['attention'],'archived')
    def test_plain_email_and_literal_mentions_do_not_add_mapping(self):
        recipient=self.create()
        self.sessions.send(recipient, {'message':'Ask @Same name or me@example.com'})
        self.assertEqual(self.starts()[-1]['input'][0]['text'],'Ask @Same name or me@example.com')
    def test_invalid_edited_overlapping_hidden_references_reject_before_provider_side_effect(self):
        for refs in ([{**self.refs[0],'text':'@Edited'}],[{**self.refs[0],'session_id':'missing'}],
                     [self.refs[0],{**self.refs[1],'start':7}],[{**self.refs[0],'start':True}],None):
            with self.subTest(refs=refs),self.assertRaises(CoordinationError):thread_mentions(self.text, {'mentions':refs},self.sessions)
        with patch.object(self.sessions,'list_work_threads',return_value=[]), self.assertRaises(CoordinationError):
            thread_mentions(self.text,{'mentions':self.refs},self.sessions)
    def test_utf16_positions_after_emoji_are_correct(self):
        text='😀 @Same name';refs=[{**self.refs[0],'start':3,'end':13}]
        self.assertEqual(thread_mentions(text,{'mentions':refs},self.sessions),refs)
        with self.assertRaises(CoordinationError):thread_mentions(text,{'mentions':[{**refs[0],'start':1}]},self.sessions)
    def test_queue_preserves_references_durably_and_dispatches_resolved_payload(self):
        recipient=self.create()
        with patch.object(self.sessions.queue,'wake'):
            self.sessions.queue.enqueue(recipient,{'message':self.text,'mentions':self.refs})
        self.assertEqual(self.sessions.queue.list(recipient)[0]['mentions'],self.refs)
        self.sessions.update(self.first,{'name':'Renamed while queued'})
        self.sessions.queue._dispatch(recipient)
        self.payload(self.starts()[-1]['input'][0]['text'])
        self.assertEqual(self.sessions.queue.list(recipient),[])
    def test_schedule_restart_edit_and_dispatch_preserve_refs_and_removal_drops_them(self):
        body={'id':str(uuid.uuid4()),'message':self.text,'mentions':self.refs,'run_at':self.now+60,'timezone':'America/Chicago',
              'settings':{'cwd':str(self.root),'client':'codex','model':'available-model','effort':'medium'}}
        item=self.sessions.schedules.create(body)
        from agent_coord.schedules import ScheduledPrompts
        reload=ScheduledPrompts(self.sessions);self.addCleanup(reload.close)
        self.assertEqual(reload.list()[0]['mentions'],self.refs)
        self.sessions.update(self.first,{'name':'Renamed before schedule'})
        self.now+=60;reload.process_once()
        self.payload(self.starts()[-1]['input'][0]['text'])
        self.assertEqual(self.store.threads.get(self.second)['attention'],'archived')
        body['id']=str(uuid.uuid4());body['run_at']=self.now+60
        item=reload.create(body)
        edit={k:v for k,v in body.items() if k not in ('id','mentions')};edit.update(action='edit',version=item['version'],message='Plain edited prompt')
        self.assertEqual(reload.change(item['id'],edit)['mentions'],[])
