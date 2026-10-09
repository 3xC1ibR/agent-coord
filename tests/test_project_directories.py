"""Directory projects retain origin while execution and presentation can change."""
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/agent-coord/scripts'))
from agent_coord.codex_app_server import BrowserSessions
from agent_coord.hook import _ensure_registered
from agent_coord.store import CoordinationStore
from agent_coord.ui import make_ui_server
from agent_coord.views import ViewStore
from test_codex_app_server import FakeCodex


class DirectoryProjectTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.child = self.root / 'website'
        self.child.mkdir()
        self.store = CoordinationStore(self.root / 'state.sqlite3')
        self.sessions = BrowserSessions(self.store, rpc_factory=FakeCodex)
        self.addCleanup(self.sessions.close)

    def create(self, cwd):
        return self.sessions.create({'cwd': str(cwd)})['session']['thread_id']

    def test_exact_origin_survives_cd_reregistration_restart_and_fork(self):
        parent, child = self.create(self.root), self.create(self.child)
        self.sessions.send(parent, {'message': '/cd ' + str(self.child)})
        self.store.register(session_id=parent, client='codex', cwd=str(self.child))
        self.assertEqual(self.sessions.work_thread(parent)['cwd'], str(self.child))
        self.assertEqual(self.sessions.work_thread(parent)['project_cwd'], str(self.root))
        self.assertEqual(self.sessions.work_thread(child)['project_cwd'], str(self.child))
        reopened = CoordinationStore(self.store.database_path)
        self.assertEqual(reopened.threads.get(parent)['project_cwd'], str(self.root))
        self.assertEqual(reopened.threads.get(child)['project_cwd'], str(self.child))
        fork = self.sessions.fork_work_thread(parent)
        self.assertEqual(self.store.threads.get(fork['thread_id'])['project_cwd'], str(self.root))

    def test_hook_keeps_exact_launch_directory_and_repository_coordination_scope(self):
        (self.root / '.git').mkdir()
        with patch('agent_coord.hook._client', return_value='codex'):
            _ensure_registered(self.store, {'session_id': 'terminal', 'cwd': str(self.child)})
            _ensure_registered(self.store, {'session_id': 'terminal', 'cwd': str(self.root)})
        self.assertEqual(self.store.get_session('terminal')['cwd'], str(self.root))
        self.assertEqual(self.store.threads.get('terminal')['project_cwd'], str(self.child))

    def test_worktrees_and_subdirectories_are_distinct_projects(self):
        (self.root / '.git/worktrees/feature').mkdir(parents=True)
        worktree = self.root / 'worktree'
        worktree.mkdir()
        gitdir = self.root / '.git/worktrees/feature'
        (gitdir / 'commondir').write_text('../..')
        (worktree / '.git').write_text('gitdir: ' + str(gitdir))
        ids = [self.create(path) for path in (self.root, self.child, worktree)]
        self.assertEqual([self.store.threads.get(id)['project_cwd'] for id in ids],
                         [str(self.root), str(self.child), str(worktree)])
        self.assertEqual(len({self.store.threads.get(id)['repository_id'] for id in ids}), 1)

    def test_legacy_rows_are_migrated_once_without_moving_artifact_links(self):
        id = self.create(self.child)
        before = self.store.threads.get(id)
        # Simulate the schema immediately before project-directory migration.
        with sqlite3.connect(self.store.database_path) as db:
            db.execute('ALTER TABLE work_threads DROP COLUMN project_cwd')
        reopened = CoordinationStore(self.store.database_path)
        after = reopened.threads.get(id)
        self.assertEqual(after['project_cwd'], str(self.child))
        self.assertEqual(after['workspace_id'], before['workspace_id'])
        reopened.register(session_id=id, client='codex', cwd=str(self.root))
        self.assertEqual(CoordinationStore(self.store.database_path).threads.get(id)['project_cwd'], str(self.child))

    def test_view_and_group_names_are_local_to_the_project(self):
        views = ViewStore(self.store)
        a = views.create({'name': 'Review', 'cwd': str(self.root)})
        b = views.create({'name': 'Review', 'cwd': str(self.child)})
        updated = views.update(a['id'], {'name': 'Ready', 'version': a['version']})
        self.assertEqual(updated['cwd'], str(self.root))
        self.assertEqual(next(v for v in views.list() if v['id'] == b['id'])['name'], 'Review')
        groups = self.store.threads.organization
        first = groups.create_project('Launch', str(self.root))
        second = groups.create_project('Launch', str(self.child))
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(groups.create_project('Launch', str(self.root)), first)
        later = views.create({'name': 'Later', 'cwd': str(self.root)})
        views.move(a['id'], {'direction': 'right'})
        self.assertEqual([v['id'] for v in views.list() if v['cwd'] == str(self.root)], [later['id'], a['id']])
        self.assertEqual(next(v for v in views.list() if v['id'] == b['id'])['position'], b['position'])

    def test_search_all_and_current_stay_inside_exact_project_after_cd(self):
        parent, child = self.create(self.root), self.create(self.child)
        self.store.threads.update(parent, title='Shared keyword parent')
        self.store.threads.update(child, title='Shared keyword child')
        self.sessions.send(parent, {'message': '/cd ' + str(self.child)})
        server = make_ui_server(self.store, port=0, browser_sessions=self.sessions)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        for scope in ('all', 'current'):
            for directory, expected in ((self.root, [parent]), (self.child, [child]), ('', [])):
                query = urllib.parse.urlencode({'q': 'keyword', 'scope': scope, 'project_cwd': str(directory)})
                with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/api/browser/search?{query}') as response:
                    result = json.load(response)
                self.assertEqual([item['thread_id'] for item in result['items']], expected)
