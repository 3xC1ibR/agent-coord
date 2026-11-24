"""Finite normalized stream mutations and their in-memory undo records.

The ledger retains old object references and scalar fields, never transcript
copies. It covers only the active item proven by the native frame classifier;
item creation, replacement, requests and terminal events use the boundary path.
"""
from collections import deque

DELTA_METHODS = frozenset(('item/agentMessage/delta', 'item/commandExecution/outputDelta',
    'item/reasoning/summaryTextDelta', 'item/mcpToolCall/outputDelta'))


class MutationLedger:
    def __init__(self):
        self.objects = []
        self.seen = set()
        self.entries = []
        self.members = []

    def capture(self, value):
        if id(value) in self.seen or not isinstance(value, (dict, list, set, deque)):
            return
        self.seen.add(id(value))
        self.objects.append((value, value.copy() if isinstance(value, (dict, set)) else tuple(value)))

    def entry(self, mapping, key):
        self.entries.append((mapping, key, key in mapping, mapping.get(key)))

    def member(self, values, identity):
        self.members.append((values, identity, identity in values))

    def item(self, item):
        if isinstance(item, dict):
            self.capture(item)
            self.capture(item.get('summary'))

    def restore(self):
        for values, identity, existed in reversed(self.members):
            values.add(identity) if existed else values.discard(identity)
        for value, saved in reversed(self.objects):
            if isinstance(value, dict):
                value.clear()
                value.update(saved)
            elif isinstance(value, list):
                value[:] = saved
            else:
                value.clear()
                value.update(saved) if isinstance(value, set) else value.extend(saved)
        for mapping, key, existed, value in reversed(self.entries):
            if existed:
                mapping[key] = value
            else:
                mapping.pop(key, None)


def identify(projection, event, message):
    """Return (thread, turn, item ID, native item) only for an existing pure item.

    Called with BrowserSessions.lock. The source frame remains the recovery
    evidence; classification neither changes adapter state nor reads storage.
    """
    if event['stream'] != 'stdout' or not isinstance(message, dict) or 'id' in message:
        return None
    entry = projection.channels.get(event['channel'])
    if not entry or entry.get('closed'):
        return None
    sessions = projection.sessions
    provider = entry['provider']
    params = message.get('params') or {}
    if not isinstance(params, dict):
        return None
    identity = params.get('threadId') if provider == 'codex' else entry.get('thread_id')
    native = None
    if provider == 'codex':
        if message.get('method') not in DELTA_METHODS or not isinstance(params.get('delta'), str):
            return None
        if message.get('method') == 'item/reasoning/summaryTextDelta':
            index = params.get('summaryIndex', 0)
            if type(index) is not int or not 0 <= index < 1024:
                return None
        turn_id, item_id = params.get('turnId'), params.get('itemId')
    elif provider == 'claude':
        frame = message.get('event') or {}
        delta = frame.get('delta') or {}
        if (message.get('type') != 'stream_event' or frame.get('type') != 'content_block_delta'
                or delta.get('type') not in {'text_delta', 'thinking_delta'}
                or not isinstance(delta.get('text' if delta.get('type') == 'text_delta' else 'thinking', ''), str)):
            return None
        native = sessions.claude.blocks.get(identity, {}).get(frame.get('index', 0))
        turn_id = sessions.claude.turns.get(identity, {}).get('id')
        item_id = native.get('id') if native else None
    elif provider in {'kimi', 'antigravity'}:
        adapter = getattr(sessions, provider)
        if identity in getattr(adapter, 'native_replays', {}):
            return None
        if message.get('method') != 'session/update':
            return None
        update = params.get('update') or {}
        turn = adapter.turns.get(identity) or {}
        kind = update.get('sessionUpdate')
        turn_id = turn.get('id')
        if kind in {'agent_message_chunk', 'agent_thought_chunk'}:
            content = update.get('content') or {}
            if content.get('type') != 'text' or not isinstance(content.get('text', ''), str):
                return None
            items = turn.get('items') or []
            native = items[-1] if items else None
            item_type = 'agentMessage' if kind == 'agent_message_chunk' else 'reasoning'
            if not native or native.get('type') != item_type:
                return None
        elif kind == 'tool_call_update':
            if set(update) - {'sessionUpdate', 'toolCallId', 'rawOutput'}:
                return None
            native = next((item for item in turn.get('items', []) if item['id'] == update.get('toolCallId')), None)
            previous = native.get('result') if native else None
            current = update.get('rawOutput')
            if (not isinstance(previous, str) or not isinstance(current, str)
                    or len(current) > 64000 or not current.startswith(previous)):
                return None
        else:
            return None
        item_id = native['id']
    elif provider == 'muse':
        if message.get('method') != 'item/delta' or not isinstance(params.get('delta'), str):
            return None
        field = params.get('field', 'text')
        if field not in {'text', 'visibleOutput', 'output'}:
            if not (field.startswith('summary.') and field[8:].isdigit() and int(field[8:]) <= 100):
                return None
        adapter = sessions.muse
        native = adapter.items.get(identity, {}).get(params.get('itemId'))
        turn_id = (native or {}).get('turnId') or params.get('turnId') or adapter.turns.get(identity, {}).get('id')
        item_id = params.get('itemId')
    else:
        return None
    history = sessions.histories.get(identity)
    turn = next((turn for turn in (history or {}).get('turns', []) if turn.get('id') == turn_id), None)
    item = next((item for item in (turn or {}).get('items', []) if item.get('id') == item_id), None)
    if not identity or not turn or not item:
        return None
    return identity, turn, item, native


def ledger(sessions, target, entry, message):
    identity, turn, item, native = target
    undo = MutationLedger()
    undo.item(item)
    undo.capture(turn)
    undo.item(native)
    undo.entry(sessions.history_revisions, identity)
    undo.member(sessions.dirty_histories, identity)
    watchdog = sessions.watchdog.observed.get(identity)
    undo.capture(watchdog)
    if watchdog:
        undo.capture(watchdog.get('tools'))
    delivery = getattr(sessions, 'delivery_inflight', {}).get(identity)
    undo.capture(delivery)
    if entry['provider'] == 'muse':
        adapter = sessions.muse
        rendered_turn = adapter.turns.get(identity, {})
        if rendered_turn.get('id') != turn.get('id'):
            rendered_turn = adapter.background_turns.get(identity, {}).get(turn.get('id'), {})
        undo.item(next((value for value in rendered_turn.get('items', []) if value.get('id') == item.get('id')), None))
        params = message.get('params') or {}
        cursor = params.get('viewCursor')
        if cursor:
            key = (message.get('method'), cursor,
                   params.get('approvalId') or params.get('userInputId') or params.get('itemId') or params.get('item', {}).get('itemId'),
                   params.get('currentRequirementId', {}).get('sourceIndex'),
                   params.get('change', {}).get('kind'),
                   params.get('change', {}).get('requirementId', {}).get('sourceIndex'))
            undo.member(sessions.muse.seen[identity], key)
    if entry['provider'] == 'antigravity':
        adapter = sessions.antigravity
        undo.capture(adapter.delivery_turns.get(identity))
        undo.entry(adapter.last_activity, identity)
    return undo
