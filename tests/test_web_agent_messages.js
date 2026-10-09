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
  return {tag, children: [], dataset: {}, append(...nodes) { this.children.push(...nodes); },
    setAttribute(key, value) { this[key] = value; }};
}
const document = {createElement: element};
const text = node => [node.textContent || '', ...(node.children || []).map(text)].join(' ');

test('both sides keep identity and link to the same message with safe literal text', () => {
  const visits = [];
  for (const direction of ['sent', 'received']) {
    const card = AgentMessages.card(document, sample({direction}), {open: (...args) => visits.push(args)});
    assert.equal(card.id, 'agent-message-7');
    assert.equal(card.children[1].textContent, '<script>alert(1)</script>');
    assert.equal(card.children[1].innerHTML, undefined);
    const link = card.children[0].children[1].children[0];
    assert.equal(link.textContent, '<b>Validator</b>');
    assert.equal(link.href, '/?message=7#peer%20id');
    let prevented = false;
    link.onclick({button: 0, preventDefault() { prevented = true; }});
    assert.ok(prevented);
    link.onclick({button: 0, metaKey: true, preventDefault() { throw Error('modifier click intercepted'); }});
  }
  assert.deepEqual(visits, [['peer id', 7], ['peer id', 7]]);
});

test('confirmed creation labels only the initial outgoing card and preserves its link and position', () => {
  const created = sample({direction:'sent', creation_request_id:'creation-key', body:'Initial prompt'});
  const card = AgentMessages.card(document, created);
  assert.equal(card['aria-label'], 'Created agent <b>Validator</b>');
  assert.equal(card.children[0].children[1].textContent, 'Created agent ');
  assert.equal(card.children[0].children[1].children[0].href, '/?message=7#peer%20id');
  assert.equal(card.children[1].textContent, 'Initial prompt');
  assert.equal(card.id, 'agent-message-7');
  assert.equal(AgentMessages.card(document, {...created, direction:'received'})['aria-label'], 'Message from <b>Validator</b>');
  assert.equal(AgentMessages.card(document, {...created, creation_request_id:null})['aria-label'], 'Message to <b>Validator</b>');
  const turns=[{id:'turn',startedAt:1234567900,items:[{id:'later',type:'userMessage'}]}];
  const order = messages => AgentMessages.entries(turns,messages).map(e=>e.message?.id || e.item?.id || 'end');
  assert.deepEqual(order([created]), order([{...created, creation_request_id:null}]));
  assert.equal(AgentMessages.entries(turns,[created])[0].message.id, created.id);
  assert.equal(AgentMessages.entries(turns,[created]).filter(e=>e.message).length,1);
});

test('long message expansion is retained and unavailable peers have no dead link', () => {
  const body = 'Full message\n'.repeat(40);
  const card = AgentMessages.card(document, sample({body, counterpart: {title: 'Elsewhere', available: false}}), {expanded: true});
  assert.equal(card.children[0].children[1].children[0].tag, 'span');
  const detail = card.children[1];
  assert.equal(detail.tag, 'details');
  assert.equal(detail.open, true);
  assert.equal(detail.children[1].textContent, body);
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
  const context={state,renderTimeline:()=>{},renderStatus:()=>{}};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('function applyEvent('),source.indexOf('let listTimer')),context);
  context.applyEvent({method:'turn/started',params:{threadId:'thread',turn:{id:'new',startedAt:200,items:[]}}});
  context.applyEvent({method:'item/completed',params:{threadId:'thread',turnId:'new',turnStartedAt:200,
    item:{id:'new-user',type:'userMessage',timelineAt:200}}});
  assert.deepEqual(visibleOrder(state.detail.thread.turns,state.detail.coordinationMessages),['old-user','card-1','new-user']);
});
