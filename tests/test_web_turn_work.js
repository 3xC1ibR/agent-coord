'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const AgentMessages = require('../plugins/agent-coord/scripts/agent_coord/web/agent-messages.js');
const source = fs.readFileSync(require.resolve('../plugins/agent-coord/scripts/agent_coord/web/app.js'), 'utf8');
function element(tag, textContent = '', className = '') {
  return {tag, textContent, className, dataset: {}, children: [], listeners: {}, open:false,
    append(...nodes) { for (const n of nodes) this.children.push(...(n.tag === 'fragment' ? n.children : [n])); },
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); },
    get childNodes() { return this.children; },
    setAttribute(key, value) { this[key] = value; },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    querySelectorAll() { return descendants(this).filter(n => n.tag === 'details' && n.dataset.id); }};
}
function descendants(el) { return el.children.flatMap(n => [n, ...descendants(n)]); }
function harness(turns, messages = [], storage = new Map()) {
  const timeline = element('main');
  const entries = new Map();
  const state = {selected:'test', detail:{work_thread:{client:'claude'},thread:{turns},coordinationMessages:messages}};
  const ctx = vm.createContext({state, AgentMessages, node:element, $:()=>timeline,
    document:{createElement:element,createDocumentFragment:()=>element('fragment')},
    conversationEntry:id=>{if(!entries.has(id))entries.set(id,{});return entries.get(id);}, sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
    messageMarkdown:{render:t=>t}, ChatImageAttachments:{appendPreviews(){}},openAgentMessage(){},focusAgentMessage(){}});
  vm.runInContext(source.slice(source.indexOf('function itemText('),source.indexOf('function focusAgentMessage(')),ctx);
  return {timeline,state,render:()=>vm.runInContext('renderTimeline()',ctx),storage};
}
const user = {id:'user',type:'userMessage',content:[{type:'text',text:'Do the work'}],timelineAt:100};
const thinking = {id:'thinking',type:'reasoning',summary:['Reasoning details'],timelineAt:110};
const command = {id:'command',type:'commandExecution',command:'run test',aggregatedOutput:'all output',exitCode:1,status:'failed',timelineAt:130};
const answer = {id:'answer',type:'agentMessage',text:'Result',timelineAt:150};
const turn = (status='completed',id='first') => ({id,status,startedAt:100,completedAt:160,items:[user,thinking,command,answer]});
const groups = h => h.timeline.children.filter(n=>n.className==='turn-work');
test('live work and commentary stay visible until the actual turn ends; accessible speaker names remain',()=>{
  const h=harness([turn('inProgress')]); h.render();
  assert.equal(groups(h).length,0);
  assert.equal(h.timeline.children.filter(n=>n.className==='tool').length,2);
  const articles=h.timeline.children.filter(n=>n.tag==='article');
  assert.deepEqual(articles.map(n=>n['aria-label']),['You','Claude']);
  assert.ok(articles.every(n=>n.children.every(c=>c.className!=='speaker')));
  h.state.detail.thread.turns[0].status='completed';h.render();
  assert.equal(groups(h).length,1);assert.equal(groups(h)[0].open,false);
  assert.deepEqual(groups(h)[0].children.slice(1).map(n=>n.dataset.id),['thinking','command']);
  assert.match(groups(h)[0].children[2].children[1].textContent,/all output\n\nExit code: 1/);
  assert.equal(h.timeline.children.at(-1)['aria-label'],'Claude');
});
test('each terminal turn has its own group and failed/stopped outcomes and errors stay truthful',()=>{
  const failed={...turn('failed'),error:{message:'Provider failure'}};
  const stopped=turn('interrupted','second');
  const h=harness([failed,stopped]);h.render();
  assert.deepEqual(groups(h).map(g=>g.children[0].textContent),['Work · Failed','Work · Stopped']);
  assert.ok(h.timeline.children.some(n=>n.textContent==='Provider failure'));
  assert.ok(h.timeline.children.some(n=>n.textContent==='Turn stopped.'));
});
test('sent, received and created cards remain outside Work in their timeline order with links intact',()=>{
  const messages=['sent','received','sent'].map((direction,i)=>({id:i+1,direction,created_at:115+i,body:'Coordination text',
    creation_request_id:i===2?'created':null,counterpart:{session_id:'peer',title:'Specialist',available:true},status:'Sent'}));
  const h=harness([turn()],messages);h.render();
  const cards=h.timeline.children.filter(n=>n.id?.startsWith('agent-message-'));
  assert.equal(cards.length,3);
  assert.deepEqual(cards.map(c=>c['aria-label']),['Message to Specialist','Message from Specialist','Created agent Specialist']);
  assert.equal(cards[2].children[0].children[1].children[0].href,'/?message=3#peer');
  assert.equal(descendants(groups(h)[0]).filter(n=>n.id?.startsWith('agent-message-')).length,0);
  assert.equal(h.timeline.children.at(-1)['aria-label'],'Claude');
});
test('nested and outer expansion survive streaming, refreshed history, conversation eviction and page reload',()=>{
  const h=harness([turn('inProgress')]);h.render();
  h.timeline.children.find(n=>n.dataset.id==='command').open=true;
  h.state.detail.thread.turns[0].status='completed';h.render();
  const group=groups(h)[0];assert.equal(group.open,false);
  assert.equal(group.children[2].open,true);
  group.open=true;group.listeners.toggle();h.render();assert.equal(groups(h)[0].open,true);
  h.state.detail.thread.turns=JSON.parse(JSON.stringify([turn()]));h.render();
  assert.equal(groups(h)[0].children[2].open,true);
  const reload=harness([turn()],[],h.storage);reload.render();
  assert.equal(groups(reload)[0].open,true);assert.equal(groups(reload)[0].children[2].open,true);
  groups(reload)[0].open=false;groups(reload)[0].listeners.toggle();reload.render();assert.equal(groups(reload)[0].open,false);
});
test('unknown or missing turn status does not prematurely hide activity',()=>{
  for(const status of [undefined,'queued','inProgress']) {const h=harness([turn(status)]);h.state.detail.thread.turns[0].status=status;h.render();assert.equal(groups(h).length,0);}
});
test('completion event collapses immediately and retains streamed content when the event has no items',()=>{
  const h=harness([turn('inProgress')]);h.render();
  let scheduled=0;
  const ctx=vm.createContext({state:h.state,renderTimeline:h.render,scheduleDetail(){scheduled++;},renderStatus(){}});
  vm.runInContext(source.slice(source.indexOf('function applyEvent('),source.indexOf('let listTimer, detailTimer;')),ctx);
  vm.runInContext('applyEvent({method:"turn/completed",params:{threadId:"test",turn:{id:"first",status:"failed",error:{message:"Failed completion"}}}})',ctx);
  assert.equal(groups(h)[0].children[0].textContent,'Work · Failed');
  assert.equal(groups(h)[0].children.length,3);
  assert.equal(h.state.detail.running,false);assert.equal(scheduled,1);
});

test('a queued toggle from a background transcript stores expansion under its owning conversation',()=>{
  const h=harness([turn()]);h.render();
  const original=groups(h)[0];h.state.selected='other';h.state.timelineThread=null;h.render();
  original.open=true;original.listeners.toggle();
  assert.ok(JSON.parse(h.storage.get('timeline-disclosures:test')).includes('turn-work:first'));
  assert.equal(h.storage.has('timeline-disclosures:other'),false);
});
