import test_thread_mentions as thread_tests
from pathlib import Path
from unittest.mock import patch
from agent_coord.workspace_files import workspace_files
from agent_coord.thread_mentions import thread_mentions
from agent_coord.store import CoordinationError
import unittest


class FileMentionTests(thread_tests.ThreadMentionTests):
    def setUp(self):
        super().setUp()
        (self.root/'docs').mkdir()
        (self.root/'docs'/'same name.md').write_text('Current contents')
        self.text = '  Read @docs/same name.md '
        self.refs = [{'start':7,'end':24,'text':'@docs/same name.md','kind':'file',
                      'root':str(self.root),'path':'docs/same name.md'}]
        self.refs[0]['end'] = 7 + len(self.refs[0]['text'])
    def payload(self, text):
        import json
        original, mapping = text.split('\n\n[Selected workspace and thread references; positions are UTF-16 offsets in the user text above]\n')
        self.assertEqual(original,self.text);self.assertEqual(json.loads(mapping),self.refs)
    # Inherited provider/queue/schedule tests intentionally exercise the exact same paths for files.
    def test_invalid_edited_overlapping_hidden_references_reject_before_provider_side_effect(self):
        for ref in ({**self.refs[0],'text':'@Edited'}, {**self.refs[0],'path':'../outside'},
                    {**self.refs[0],'root':'/etc'}, {**self.refs[0],'path':'missing'},
                    {**self.refs[0],'kind':'thread'}):
            with self.assertRaises(CoordinationError):thread_mentions(self.text,{'mentions':[ref]},self.sessions)
    def test_utf16_positions_after_emoji_are_correct(self):
        ref={**self.refs[0],'start':3,'end':3+len(self.refs[0]['text'])}
        self.assertEqual(thread_mentions('😀 '+ref['text'],{'mentions':[ref]},self.sessions),[ref])
    def test_search_duplicate_names_ignore_rules_and_escape(self):
        (self.root/'same name.md').write_text('Other')
        (self.root/'.gitignore').write_text('ignored.md\n')
        (self.root/'ignored.md').write_text('Ignored')
        result=workspace_files('search',{'root':str(self.root),'query':'same'},str(self.root))
        self.assertEqual({e['path'] for e in result['entries']},{'same name.md','docs/same name.md'})
        # gitignore works even outside git with --no-require-git.
        self.assertEqual(workspace_files('search',{'root':str(self.root),'query':'ignored'},str(self.root))['entries'],[])
        (self.root/'escape').symlink_to('/etc/passwd')
        self.assertEqual(workspace_files('search',{'root':str(self.root),'query':'escape'},str(self.root))['entries'],[])
        with self.assertRaises(CoordinationError):workspace_files('search',{'root':'/etc','query':''},str(self.root))
    def test_fallback_search_is_bounded_and_skips_dependency_folders(self):
        (self.root/'node_modules').mkdir();(self.root/'node_modules'/'same.md').write_text('skip')
        with patch('agent_coord.workspace_files.subprocess.run',side_effect=FileNotFoundError):
            result=workspace_files('search',{'root':str(self.root),'query':'same'},str(self.root))
        self.assertEqual([e['path'] for e in result['entries']],['docs/same name.md'])
    def test_removed_file_rejected_at_dispatch_validation(self):
        (self.root/'docs'/'same name.md').unlink()
        with self.assertRaises(CoordinationError):thread_mentions(self.text,{'mentions':self.refs},self.sessions)
