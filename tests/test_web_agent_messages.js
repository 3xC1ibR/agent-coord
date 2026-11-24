'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const AgentMessages = require('../plugins/agent-coord/scripts/agent_coord/web/agent-messages.js');
const source = fs.readFileSync(require.resolve('../plugins/agent-coord/scripts/agent_coord/web/app.js'), 'utf8');
const sample = values => ({id: 7, direction: 'received', created_at: 1234567890, body: '<script>alert(1)</script>',
  counterpart: {session_id: 'peer id', title: '<b>Validator</b>', available: true}, status: 'Queued for agent', ...values});
function element(tag) {
  return {tag, children: [], dataset: {}, style: {}, listeners: {}, append(...nodes) { this.children.push(...nodes); },
    setAttribute(key, value) { this[key] = value; }, addEventListener(type, listener) { this.listeners[type] = listener; }};
}
const document = {createElement: element};
// Element.append also accepts strings.
const text = node => typeof node === 'string' ? node : [node.textContent || '', ...(node.children || []).map(text)].join(' ');
const descendants = node => typeof node === 'string' ? [] : [node, ...node.children.flatMap(descendants)];
const withClass = (node, name) => descendants(node).find(n => n.className === name);

test('watchdog preview shows outstanding work and queue age while disclosure preserves the receipt', () => {
  const body = JSON.stringify({source: 'runtime', stage: 'watchdog', classification: 'waiting_for_approval',
    detail: 'Claude is waiting for tool approval.', actionable_inbox_count: 4, oldest_actionable_age_seconds: 1340});
  const card = AgentMessages.card(document, sample({thread_id: 'app-agent-stall:12:2', body}));
  const preview = withClass(card, 'agent-message-preview').textContent;
  assert.match(preview, /waiting for tool approval/);
  assert.match(preview, /4 queued.*oldest 22 min/);
  assert.equal(withClass(card, 'agent-message-body').textContent, body);
  for (const [thread_id, value] of [['other', body], ['app-agent-stall:12:3', '{unparsed']]) {
    const ordinary = AgentMessages.card(document, sample({thread_id, body: value}));
    assert.ok(withClass(ordinary, 'agent-message-preview').textContent.startsWith(value.slice(0, 40)));
  }
});

test('both sides keep identity and link to the same message with safe literal text', () => {
  const visits = [];
  for (const direction of ['sent', 'received']) {
    const card = AgentMessages.card(document, sample({direction}), {open: (...args) => visits.push(args)});
    assert.equal(card.id, 'agent-message-7');
    assert.equal(withClass(card, 'agent-message-body').textContent, '<script>alert(1)</script>');
    assert.equal(withClass(card, 'agent-message-body').innerHTML, undefined);
    const link = withClass(card, 'agent-message-peer');
    assert.equal(link.textContent, '<b>Validator</b>');
    assert.equal(link.href, '/?message=7#peer%20id');
    let prevented = false;
    link.onclick({button: 0, preventDefault() { prevented = true; }});
    assert.ok(prevented);
    link.onclick({button: 0, metaKey: true, preventDefault() { throw Error('modifier click intercepted'); }});
  }
  assert.deepEqual(visits, [['peer id', 7], ['peer id', 7]]);
});

test('agent names replace message eyebrows while accessible direction and creation identity remain', () => {
  const created = sample({direction:'sent', creation_request_id:'creation-key', body:'Initial prompt'});
  const card = AgentMessages.card(document, created);
  assert.equal(card['aria-label'], 'Created agent <b>Validator</b>');
  assert.equal(text(withClass(card, 'agent-message-label')).trim(), '<b>Validator</b>');
  assert.equal(withClass(card, 'agent-message-peer').href, '/?message=7#peer%20id');
  assert.equal(withClass(card, 'agent-message-body').textContent, 'Initial prompt');
  assert.equal(card.id, 'agent-message-7');
  assert.equal(AgentMessages.card(document, {...created, direction:'received'})['aria-label'], 'Message from <b>Validator</b>');
  assert.equal(AgentMessages.card(document, {...created, creation_request_id:null})['aria-label'], 'Message to <b>Validator</b>');
  for (const direction of ['sent', 'received']) {
    const heading = withClass(AgentMessages.card(document, sample({direction})), 'agent-message-heading');
    assert.doesNotMatch(text(heading), /Message (to|from)|Created agent/);
    assert.match(text(heading), /Validator/);
    const icon = withClass(heading, 'agent-message-icon');
    assert.equal(icon.textContent, direction === 'sent' ? '↗' : '↙');
    assert.equal(icon.title, direction === 'sent' ? 'Sent' : 'Received');
  }
  const turns=[{id:'turn',startedAt:1234567900,items:[{id:'later',type:'userMessage'}]}];
  const order = messages => AgentMessages.entries(turns,messages).map(e=>e.message?.id || e.item?.id || 'end');
  assert.deepEqual(order([created]), order([{...created, creation_request_id:null}]));
  assert.equal(AgentMessages.entries(turns,[created])[0].message.id, created.id);
  assert.equal(AgentMessages.entries(turns,[created]).filter(e=>e.message).length,1);
});

test('long message expansion is retained and unavailable peers have no dead link', () => {
  const body = 'Full message\n'.repeat(40);
  const card = AgentMessages.card(document, sample({body, counterpart: {title: 'Elsewhere', available: false}}), {expanded: true});
  assert.equal(withClass(card, 'agent-message-peer').tag, 'span');
  const detail = card.children[0];
  assert.equal(detail.tag, 'details');
  assert.equal(detail.open, true);
  assert.equal(detail.children[1].textContent, body);
});

test('closed status stays inside the collapsed content for both short and long exchanges', () => {
  for (const body of ['Finished.', 'Full message\n'.repeat(40)]) {
    const message = sample({body, status: 'Exchange closed', closed_at: 1234567900});
    const card = AgentMessages.card(document, message);
    const detail = card.children[0];
    assert.equal(detail.tag, 'details');
    assert.equal(detail.open, false);
    assert.equal(detail.dataset.id, 'agent-message:7');
    assert.doesNotMatch(text(detail.children[0]), /Exchange closed/);
    assert.equal(detail.children[1].textContent, body);
    assert.equal(text(detail.children[2]).trim(), 'Exchange closed');
    assert.equal(card.children.length, 1, 'closed status has no visible outer footer');
    detail.open = true;
    assert.doesNotMatch(text(card), /Collapse message/);
    const restored = AgentMessages.card(document, message, {expanded: true});
    assert.equal(restored.children[0].open, true);
  }
});

test('card clicks toggle short and long messages while summaries, links and text selection keep their behavior', () => {
  for (const body of ['Short message.', 'Full message\n'.repeat(40)]) {
    let selection = {isCollapsed: true};
    const doc = {...document, defaultView: {getSelection: () => selection}};
    const card = AgentMessages.card(doc, sample({body}));
    const detail = card.children[0];
    const click = (target = null, extra = {}) => card.onclick({button: 0, target: {closest: () => target}, ...extra});
    assert.equal(detail.open, false);
    click(); assert.equal(detail.open, true);
    click(); assert.equal(detail.open, false);
    for (const target of ['summary', 'a', 'button']) {
      click(target); assert.equal(detail.open, false, 'native summary and link clicks are not toggled twice');
    }
    click(null, {defaultPrevented: true}); assert.equal(detail.open, false);
    click(null, {ctrlKey: true}); assert.equal(detail.open, false);
    click(null, {button: 1}); assert.equal(detail.open, false);
    selection = {isCollapsed: false};
    click(); assert.equal(detail.open, false, 'copying message text does not collapse it');
    assert.equal(detail.children[0].tag, 'summary', 'native keyboard disclosure remains available');
    assert.doesNotMatch(text(card), /Collapse message/);
  }
});

const visibleOrder = (turns, messages) => AgentMessages.entries(turns, messages)
  .filter(e => !e.end).map(e => e.message ? 'card-' + e.message.id : e.item.id);

test('messages stay before a later user turn, including legacy messages without anchors', () => {
  const turns = [{id:'a', startedAt:100, completedAt:140, items:[
    {id:'first-user',type:'userMessage',timelineAt:100}, {id:'first-answer',type:'agentMessage',timelineAt:120}]}];
  const messages = [sample({id:1,created_at:160}), sample({id:2,created_at:170})];
  assert.deepEqual(visibleOrder(turns,messages), ['first-user','first-answer','card-1','card-2']);
  turns.push({id:'b',startedAt:200,items:[{id:'later-user',type:'userMessage',timelineAt:200}]});
  assert.deepEqual(visibleOrder(turns,messages), ['first-user','first-answer','card-1','card-2','later-user']);
});

test('cards interleave with streamed items and keep creation order across turn anchors', () => {
  const turns=[{id:'a',startedAt:100,items:[{id:'user',timelineAt:100},
    {id:'tool',timelineAt:110},{id:'answer',timelineAt:150}]}];
  const messages=[sample({id:1,created_at:90,turn_id:'a'}),
    sample({id:2,created_at:120,direction:'sent',turn_id:'a'}),
    sample({id:3,created_at:130,turn_id:'a'})];
  assert.deepEqual(visibleOrder(turns,messages),['card-1','user','tool','card-2','card-3','answer']);
});

test('late inbox delivery never moves an old message under a newer user prompt', () => {
  const turns=[{id:'a',startedAt:100,items:[{id:'first-user',timelineAt:100}]},
    {id:'b',startedAt:200,items:[{id:'later-user',timelineAt:200}]}];
  const message=sample({id:1,created_at:90,turn_id:'b',direction:'received'});
  assert.deepEqual(visibleOrder(turns,[message]),['card-1','first-user','later-user']);
});

test('partially recoverable history still leaves older cards above a new dated turn', () => {
  const turns=[{id:'legacy',items:[{id:'legacy-user'}]},
    {id:'new',startedAt:'2026-10-08T21:35:00Z',items:[{id:'new-user',type:'userMessage'}]}];
  const messages=[sample({id:1,created_at:Date.parse('2026-10-08T19:39:00Z')/1000})];
  assert.deepEqual(visibleOrder(turns,messages),['legacy-user','card-1','new-user']);
  assert.equal(AgentMessages.entries(turns,messages).filter(e=>e.message).length,1);
});

test('only confirmed wake turns replace the generic wake prompt', () => {
  const item = {type: 'userMessage', content: [{text: 'Check and handle your unread agent-coord messages.'}]};
  const message = sample({started_turn: true, wake_turn_id: 'wake'});
  assert.ok(AgentMessages.isWakePrompt(item, {id: 'wake'}, [message]));
  assert.equal(AgentMessages.isWakePrompt(item, {id: 'other'}, [message]), false);
  assert.equal(AgentMessages.isWakePrompt(item, {id: 'wake'}, []), false);
  assert.equal(AgentMessages.isWakePrompt({...item, content: [{text: 'User task'}]}, {id: 'wake'}, [message]), false);
  const automatic = {...item, content: [{text: '[Automatic Agent Coord inbox wake — not a human message]\n' +
    'Check and handle your unread agent-coord messages. This prompt was generated by the runtime because agent messages are pending.'}]};
  assert.ok(AgentMessages.isWakePrompt(automatic, {id: 'wake'}, [message]));
  assert.equal(AgentMessages.isWakePrompt(automatic, {id: 'other'}, [message]), false);
  assert.equal(AgentMessages.isWakePrompt(automatic, {id: 'wake'}, []), false);
  assert.equal(AgentMessages.isWakePrompt({...automatic, content: [{text: automatic.content[0].text + ' My task.'}]},
    {id: 'wake'}, [message]), false, 'a human message with the same prefix remains visible');
  assert.match(text(AgentMessages.card(document, message)), /Started this turn/);
  assert.doesNotMatch(text(AgentMessages.card(document, sample({}))), /Started this turn/);
});

test('message refresh discards stale responses after switching conversations', async () => {
  let resolve;
  const entry = {}, state = {selected: 'a', detail: {}, conversations: new Map([['a', entry]])};
  const context = {state, isSessionDraft: () => false, conversationEntry: () => entry,
    threadPath: id => 'threads/' + id, api: () => new Promise(done => { resolve = done; }),
    renderTimeline: () => { throw Error('rendered stale conversation'); }};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('async function refreshAgentMessages('), source.indexOf('function requestButton(')), context);
  const pending = context.refreshAgentMessages();
  state.selected = 'b';
  resolve({data: [sample({})]});
  await pending;
  assert.equal(state.detail.coordinationMessages, undefined);
});

test('matching message navigation preserves drafts by using existing session navigation', async () => {
  const calls = [], card = {scrollIntoView: () => calls.push('scroll'), focus: () => calls.push('focus')};
  const state = {selected: 'sender'};
  const context = {state, window: {}, document: {getElementById: id => id === 'agent-message-7' ? card : null},
    $: () => ({scrollTop: 42}), select: async id => { calls.push(id); state.selected = id; }, showError: error => { throw error; }};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('function focusAgentMessage('), source.indexOf('async function refreshAgentMessages(')), context);
  await context.openAgentMessage('recipient', 7);
  assert.deepEqual(calls, ['recipient', 'scroll', 'focus']);
  assert.equal(state.messageTarget, null);
});

test('an anchored outgoing card cannot jump over an earlier unanchored incoming card', () => {
  const turns=[{id:'a',startedAt:100,items:[{id:'user',timelineAt:100}]}];
  const messages=[sample({id:1,created_at:120}),sample({id:2,created_at:130,turn_id:'a',direction:'sent'})];
  assert.deepEqual(visibleOrder(turns,messages),['user','card-1','card-2']);
});

test('live turn-start events retain timing before the first streamed user item', () => {
  const state={selected:'thread',detail:{thread:{turns:[{id:'old',items:[{id:'old-user'}]}]},
    coordinationMessages:[sample({id:1,created_at:150})]}};
  const context={state,renderTimeline:()=>{},scheduleTimelineRender:()=>{},renderStatus:()=>{}};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('function applyEvent('),source.indexOf('let listTimer')),context);
  context.applyEvent({method:'turn/started',params:{threadId:'thread',turn:{id:'new',startedAt:200,items:[]}}});
  context.applyEvent({method:'item/completed',params:{threadId:'thread',turnId:'new',turnStartedAt:200,
    item:{id:'new-user',type:'userMessage',timelineAt:200}}});
  assert.deepEqual(visibleOrder(state.detail.thread.turns,state.detail.coordinationMessages),['old-user','card-1','new-user']);
});

const DELIVERY = '[Automatic Agent Coord message delivery — not a human message]\nHandle the coordination messages supplied here. ' +
  'If their bodies are missing, retrieve them with agent-coord inbox --unread. Delivery receipts are automatic. Reply only when reply_required=true.';
const userItem = text => ({type: 'userMessage', content: [{type: 'text', text}]});
const delivery = (...entries) => userItem(DELIVERY + '\n' + entries.map(entry => JSON.stringify(entry)).join('\n'));
const request = {id: 36798, sender_session_id: 'adjutant', sender_name: 'Adjutant', classification: 'action_required',
  thread_id: 'app-agent-request:stoic-share', reply_required: true, body: 'Save links to **Stoic** from the share sheet.'};
const eventBody = JSON.stringify({event: 'subscription_matched', subscription_id: 'sub-1', mode: 'all', target: 'all',
  outcomes: [{kind: 'turn', session_id: 'worker', turn_id: 't1', state: 'completed',
    checkpoint: {phase: 'validation', summary: 'Share sheet saves links.'}}],
  continuation: 'Review the result.', meaning: 'Turn completion is not assignment completion.'});
const event = {id: 36860, sender_session_id: 'me', sender_name: 'Me', classification: 'action_required',
  thread_id: 'event:sub-1:all', reply_required: false, body: eventBody};

test('runtime-generated prompt families are recognized and human text is not', () => {
  const message = AgentMessages.automaticPrompt(delivery(request));
  assert.equal(message.kind, 'delivery');
  assert.deepEqual(message.entries.map(e => e.id), [36798]);
  assert.equal(AgentMessages.automaticPrompt(delivery(event)).kind, 'event');
  assert.equal(AgentMessages.automaticPrompt(delivery(request, event)).kind, 'delivery');
  assert.equal(AgentMessages.automaticPrompt(userItem(DELIVERY)).kind, 'wake', 'body-free browser delivery');
  assert.equal(AgentMessages.automaticPrompt(userItem('Check and handle your unread agent-coord messages.')).kind, 'wake');
  assert.equal(AgentMessages.automaticPrompt(userItem('Automatic agent-message delivery. Handle the coordination messages supplied with this turn. ' +
    'If their bodies are missing, retrieve them with agent-coord inbox --unread.')).kind, 'wake');
  const continuation = AgentMessages.automaticPrompt(userItem('[Automatic continuation after a Ribbon Field backend update]\n' +
    'Continue the unfinished assignment from the saved conversation.'));
  assert.equal(continuation.kind, 'automatic');
  assert.equal(continuation.title, 'Automatic continuation after a Ribbon Field backend update');
  for (const text of [DELIVERY + '\n' + JSON.stringify(request) + '\nAnd also fix the build.', 'Please fix the build.',
    '[Automatic continuation after a Ribbon Field backend update]', DELIVERY.split('\n')[0] + '\nMy own words',
    DELIVERY + '\n{"id": "not a number"}'])
    assert.equal(AgentMessages.automaticPrompt(userItem(text)), null, text);
  assert.equal(AgentMessages.automaticPrompt({type: 'agentMessage', text: DELIVERY}), null);
});

test('a prompt whose messages all have cards is folded into those cards', () => {
  const prompt = AgentMessages.automaticPrompt(delivery(request, event));
  assert.ok(AgentMessages.represented(prompt, {id: 't'}, [sample({id: 36798}), sample({id: 36860})]));
  assert.equal(AgentMessages.represented(prompt, {id: 't'}, [sample({id: 36798})]), false, 'a missing card keeps the row');
  assert.equal(AgentMessages.represented(prompt, {id: 't'}, []), false);
  const wake = AgentMessages.automaticPrompt(userItem('Check and handle your unread agent-coord messages.'));
  assert.ok(AgentMessages.represented(wake, {id: 'wake'}, [sample({started_turn: true, wake_turn_id: 'wake'})]));
  assert.equal(AgentMessages.represented(wake, {id: 'other'}, [sample({started_turn: true, wake_turn_id: 'wake'})]), false);
  const item = delivery(request);
  const prompts = AgentMessages.deliveredPrompts([{id: 't', items: [userItem('Human words'), item]}]);
  assert.deepEqual([...prompts.keys()], [36798]);
  const card = AgentMessages.card(document, sample({id: 36798, body: request.body}), {prompt: prompts.get(36798)});
  const raw = withClass(card, 'agent-message-raw');
  assert.equal(raw.tag, 'details');
  assert.equal(raw.open, undefined, 'the exact prompt is never the default view');
  assert.equal(raw.children[0].textContent, 'Delivered prompt');
  assert.equal(raw.children[1].textContent, item.content[0].text);
  assert.equal(withClass(AgentMessages.card(document, sample({})), 'agent-message-raw'), undefined);
});

test('subscription event cards read as outcomes with the JSON folded away', () => {
  const card = AgentMessages.card(document, sample({...event, direction: 'sent'}), {titleOf: id => id === 'worker' ? 'Share sheet worker' : null});
  const preview = withClass(card, 'agent-message-preview').textContent;
  assert.equal(preview, 'Subscription matched · Share sheet worker · completed. Share sheet saves links.');
  const body = withClass(card, 'agent-message-body');
  assert.match(text(body), /Share sheet worker.*completed · validation.*Share sheet saves links.*Review the result/s);
  const raw = withClass(body, 'agent-message-raw');
  assert.equal(raw.children[0].textContent, 'Event JSON');
  assert.equal(raw.children[1].textContent, eventBody);
  const timeout = JSON.stringify({event: 'subscription_timeout', outcomes: []});
  assert.equal(withClass(AgentMessages.card(document, sample({...event, body: timeout})), 'agent-message-preview').textContent, 'Subscription timed out');
  const unparsed = withClass(AgentMessages.card(document, sample({...event, body: '{broken'})), 'agent-message-preview').textContent;
  assert.equal(unparsed, '{broken', 'unrecognized event text is shown as written');
});

test('uncarded prompts become a marked system row whose popover holds readable details and the exact prompt', () => {
  const item = delivery(request);
  const row = AgentMessages.promptRow(document, AgentMessages.automaticPrompt(item), {time: element('time')});
  assert.equal(row.className, 'agent-coord-row');
  const trigger = withClass(row, 'agent-coord-row-trigger');
  assert.equal(trigger.tag, 'button');
  assert.equal(text(withClass(trigger, 'agent-coord-row-title')).replace(/\s+/g, ' ').trim(), 'Agent Coord message from Adjutant');
  assert.equal(withClass(trigger, 'agent-coord-row-detail').textContent, 'Reply requested');
  assert.match(trigger['aria-label'], /^Agent Coord message from Adjutant · Reply requested/);
  const panel = withClass(row, 'agent-coord-popover');
  assert.equal(panel.hidden, true);
  assert.match(text(panel), /Adjutant.*Action required · Reply requested · Exchange app-agent-request:stoic-share/s);
  assert.equal(withClass(panel, 'agent-coord-body plain').textContent, request.body, 'bodies stay literal text without the renderer');
  assert.equal(withClass(panel, 'agent-message-raw').children[1].textContent, item.content[0].text);
  trigger.onclick({});
  assert.equal(panel.hidden, false); assert.equal(trigger['aria-expanded'], 'true');
  row.listeners.pointerleave({pointerType: 'mouse'});
  assert.equal(panel.hidden, false, 'a tapped or clicked popover stays open when the pointer leaves');
  trigger.onclick({});
  assert.equal(panel.hidden, true); assert.equal(trigger['aria-expanded'], 'false');
  const titles = kind => text(withClass(AgentMessages.promptRow(document, AgentMessages.automaticPrompt(kind)), 'agent-coord-row-title')).trim();
  assert.equal(titles(delivery(event)), 'Agent Coord event');
  assert.equal(titles(userItem('Check and handle your unread agent-coord messages.')), 'Agent Coord inbox wake');
  assert.equal(titles(userItem('[Automatic continuation after a Ribbon Field backend update]\nContinue.')),
    'Automatic continuation after a Ribbon Field backend update');
  const several = AgentMessages.promptRow(document, AgentMessages.automaticPrompt(delivery(request, {...request, id: 2, sender_name: 'Validator', reply_required: false})));
  assert.equal(text(withClass(several, 'agent-coord-row-title')).replace(/\s+/g, ' ').trim(), 'Agent Coord messages from Adjutant and 1 more');
  const large = {...request, body: undefined, body_chars: 24000, retrieve: 'agent-coord message --message-id 36798 --offset 0'};
  delete large.body;
  assert.match(text(AgentMessages.promptRow(document, AgentMessages.automaticPrompt(delivery(large)))), /24,000-character body was not included/);
});

test('the timeline folds carded prompts, renders other automatic prompts as rows, and passes the exact prompt to cards', () => {
  const start = source.indexOf('function renderTimeline(');
  const body = source.slice(start, source.indexOf('\nfunction ', start + 10));
  assert.match(body, /AgentMessages\.deliveredPrompts\(turns\)/);
  assert.match(body, /prompt: deliveredPrompts\.get\(entry\.message\.id\)/);
  assert.match(body, /AgentMessages\.represented\(automatic, turn, messages\)\) continue;/);
  assert.match(body, /AgentMessages\.promptRow\(document, automatic/);
  assert.ok(body.indexOf('AgentMessages?.automaticPrompt(item)') < body.indexOf('item.type === "userMessage" || item.type === "agentMessage"'),
    'automatic prompts are handled before ordinary user messages');
});
