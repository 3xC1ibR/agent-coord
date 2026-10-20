"""Tool outcomes must survive turn termination without invented success/failure."""
import json
import unittest
from pathlib import Path

import test_antigravity as support


class ToolHistoryTests(unittest.TestCase):
    setUp = support.AntigravityTests.setUp
    make_sessions = support.AntigravityTests.make_sessions
    create = support.AntigravityTests.create
    start = support.AntigravityTests.start
    update = support.AntigravityTests.update

    def tools(self, thread):
        return {i['id']: i for t in self.sessions.read(thread)['thread']['turns']
                for i in t['items'] if i['type'] == 'mcpToolCall'}

    def reload(self, thread):
        before = self.tools(thread)
        self.sessions.close()
        self.sessions = self.make_sessions()
        self.assertEqual(self.tools(thread), before)
        return before

    def test_live_completed_without_results_is_unknown_for_both_native_outcomes(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/antigravity_tool_history.json').read_text())
        self.assertIn('violet-731', fixture['native_tool_responses'][0])
        self.assertIn('No such file', fixture['native_tool_responses'][1])
        thread = self.create()
        connection = self.start(thread)
        for update in fixture['updates']:
            self.update(thread, **update)
        connection.future.set_result({'stopReason': 'end_turn'})
        for item in self.reload(thread).values():
            self.assertEqual(item['status'], 'unknown')
            self.assertEqual(item['providerStatus'], 'completed')
            self.assertIn('without a tool result', item['statusReason'])
            self.assertNotIn('result', item)

    def test_unconfirmed_outcomes_survive_end_stop_failure_and_native_resume(self):
        for ending in ('end_turn', 'stop', 'disconnect'):
            with self.subTest(ending=ending):
                thread = self.create()
                connection = self.start(thread)
                native = self.sessions.antigravity.native[thread]
                for tool in ('success', 'failure', 'unconfirmed'):
                    self.update(thread, sessionUpdate='tool_call', toolCallId=tool, title='Read file', status='in_progress')
                self.update(thread, sessionUpdate='tool_call_update', toolCallId='success', status='completed', rawOutput='contents')
                self.update(thread, sessionUpdate='tool_call_update', toolCallId='failure', status='failed', rawOutput='Permission denied')
                # Partial output alone is not a completion acknowledgment.
                self.update(thread, sessionUpdate='tool_call_update', toolCallId='unconfirmed', rawOutput='partial')
                if ending == 'stop':
                    self.sessions.interrupt(thread)
                elif ending == 'disconnect':
                    connection.callback({'method': 'disconnected', 'params': {'error': 'Lost connection'}})
                else:
                    connection.future.set_result({'stopReason': ending})
                tools = self.reload(thread)
                self.assertEqual(tools['success']['status'], 'completed')
                self.assertEqual(tools['success']['result'], 'contents')
                self.assertEqual(tools['failure']['status'], 'failed')
                self.assertEqual(tools['failure']['result'], 'Permission denied')
                self.assertEqual(tools['unconfirmed']['status'], 'unknown')
                self.assertEqual(tools['unconfirmed']['result'], 'partial')
                resumed = self.start(thread)
                self.assertIn(('session/resume', {'cwd': str(self.root), 'mcpServers': [], 'sessionId': native}), resumed.calls)
                self.assertEqual(self.tools(thread), tools)
                resumed.future.set_result({'stopReason': 'end_turn'})

    def test_result_can_follow_completion_and_null_raw_output_uses_content(self):
        thread = self.create()
        connection = self.start(thread)
        self.update(thread, sessionUpdate='tool_call_update', toolCallId='read', status='completed')
        self.assertEqual(self.tools(thread)['read']['status'], 'unknown')
        content = [{'type': 'content', 'content': {'type': 'text', 'text': 'contents'}}]
        self.update(thread, sessionUpdate='tool_call_update', toolCallId='read', rawOutput=None, content=content)
        self.assertEqual(self.tools(thread)['read']['status'], 'completed')
        self.assertEqual(self.tools(thread)['read']['result'], content)
        self.assertIsNone(self.tools(thread)['read']['statusReason'])
        # An empty string is a valid result (for example a silent command).
        self.update(thread, sessionUpdate='tool_call_update', toolCallId='silent', status='completed', rawOutput='')
        self.assertEqual(self.tools(thread)['silent']['status'], 'completed')
        connection.future.set_result({'stopReason': 'end_turn'})
        self.assertIsNone(self.reload(thread)['read']['statusReason'])

    def test_crash_recovery_does_not_leave_tools_running_or_rewrite_confirmed_history(self):
        thread = self.create()
        self.start(thread)
        self.update(thread, sessionUpdate='tool_call', toolCallId='pending', status='in_progress')
        self.update(thread, sessionUpdate='tool_call_update', toolCallId='confirmed', status='completed', rawOutput='contents')
        # Snapshot the on-disk history as it would be left by a process crash.
        with self.store._connection() as db:
            saved = db.execute('SELECT history_json FROM browser_history WHERE thread_id = ?', (thread,)).fetchone()[0]
        self.sessions.close()
        with self.store._connection() as db:
            db.execute('UPDATE browser_history SET history_json = ? WHERE thread_id = ?', (saved, thread))
        self.sessions = self.make_sessions()
        self.assertEqual(self.tools(thread)['pending']['status'], 'unknown')
        self.assertIn('session ended', self.tools(thread)['pending']['statusReason'])
        self.assertEqual(self.tools(thread)['confirmed']['status'], 'completed')
        self.assertEqual(self.sessions.read(thread)['thread']['turns'][0]['status'], 'interrupted')


class DeliveryToolHistoryTests(unittest.TestCase):
    setUp = support.AntigravityDeliveryTests.setUp
    make_sessions = support.AntigravityTests.make_sessions
    create = support.AntigravityTests.create
    start = support.AntigravityTests.start
    update = support.AntigravityTests.update
    tools = ToolHistoryTests.tools
    reload = ToolHistoryTests.reload
    send_correction = support.AntigravityDeliveryTests.send_correction
    deliver = support.AntigravityDeliveryTests.deliver

    def test_coordination_reconnect_keeps_confirmed_result_and_persists_unknown(self):
        thread = self.create()
        connection = self.start(thread)
        self.update(thread, sessionUpdate='tool_call_update', toolCallId='confirmed', status='completed', rawOutput='already written')
        self.update(thread, sessionUpdate='tool_call', toolCallId='pending', status='in_progress')
        self.send_correction(thread)
        self.deliver(thread)
        connection.future.set_result({'stopReason': 'cancelled'})
        self.deliver(thread)
        self.assertEqual(self.tools(thread)['confirmed']['status'], 'completed')
        self.assertEqual(self.tools(thread)['pending']['status'], 'unknown')
        with self.store._connection() as db:
            saved = json.loads(db.execute('SELECT history_json FROM browser_history WHERE thread_id = ?', (thread,)).fetchone()[0])
        pending = next(i for i in saved['turns'][0]['items'] if i['id'] == 'pending')
        self.assertEqual(pending['status'], 'unknown')
        resumed = self.sessions.antigravity.connections[thread]
        resumed.future.set_result({'stopReason': 'end_turn'})
        self.assertEqual(self.reload(thread)['pending'], pending)
