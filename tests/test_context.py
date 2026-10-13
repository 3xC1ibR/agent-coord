from __future__ import annotations

import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/agent-coord/scripts'))
from agent_coord.context import cli_path, coordination_bootstrap
from agent_coord.cli import _parser
from agent_coord.store import CoordinationStore


class BootstrapTests(unittest.TestCase):
    def test_each_provider_receives_identity_skill_valid_examples_and_current_scope(self):
        with tempfile.TemporaryDirectory(prefix='coord bootstrap ') as directory:
            store = CoordinationStore(Path(directory) / 'state.sqlite3')
            for provider in ('codex', 'claude', 'kimi', 'antigravity'):
                with self.subTest(provider=provider):
                    store.register(session_id=provider, client=provider, cwd=directory)
                    store.begin_work(session_id=provider, scopes=[provider + '/**'])
                    text = coordination_bootstrap(store, provider, command='agent-coord')
                    self.assertIn(str(cli_path().parent.parent / 'skills/agent-coordination/SKILL.md'), text)
                    self.assertIn('registered with agent-coord as ' + provider, text)
                    self.assertIn('never comma-join', text)
                    state = json.loads(text.split('Current coordination state: ')[1].splitlines()[0])
                    self.assertEqual(state['write_scope'], [provider + '/**'])
                    for line in text.splitlines():
                        if line.startswith('agent-coord '):
                            _parser().parse_args(shlex.split(line.replace('<id>', '1'))[1:])
                    store.end_work(provider)

    def test_bootstrap_recomputes_state_and_quotes_fallback_paths(self):
        with tempfile.TemporaryDirectory(prefix='coord bootstrap ') as directory:
            store = CoordinationStore(Path(directory) / 'state.sqlite3')
            store.register(session_id='one', client='antigravity', cwd=directory)
            store.begin_work(session_id='one', scopes=['first'])
            self.assertIn('"write_scope": ["first"]', coordination_bootstrap(store, 'one'))
            store.begin_work(session_id='one', scopes=['second'])
            with patch('agent_coord.context.shutil.which', return_value=None):
                text = coordination_bootstrap(store, 'one')
            self.assertIn('"write_scope": ["second"]', text)
            self.assertIn(shlex.quote(str(store.database_path)), text)
