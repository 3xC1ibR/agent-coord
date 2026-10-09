'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const grouping=require('../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js');
const source=fs.readFileSync(require.resolve('../plugins/agent-coord/scripts/agent_coord/web/app.js'),'utf8');
function card(extra={}) {
 class Element {
  constructor(tag,text=null,className=''){Object.assign(this,{tag,textContent:text,className,children:[],dataset:{},attrs:{}});}
  append(...nodes){this.children.push(...nodes);}setAttribute(k,v){this.attrs[k]=v;}
  get lastChild(){return this.children.at(-1);}insertBefore(node,before){this.children.splice(this.children.indexOf(before),0,node);}
  replaceChildren(...nodes){this.children=nodes;}
 }
 const context={threadGrouping:grouping,node:(...args)=>new Element(...args),document:{createElementNS:(_,tag)=>new Element(tag),createTextNode:text=>new Element('text',text)},
  state:{selected:null,closing:new Set(),pinning:new Set(),updatingThreads:new Set()},associationLabel:()=>'',relativeTime:()=>'',action:f=>f(),select(){},snoozeLabel:()=>'',isDueSnooze:()=>false};
 vm.createContext(context);vm.runInContext(source.slice(source.indexOf('function threadCard('),source.indexOf('function groupHeading(')),context);
 const thread={thread_id:'one',title:'Chat interface',status:'idle',attention:'now',response_state:'reply',links:[],checkpoint:{summary:'Adding thread mentions.'},...extra};
 const before=JSON.stringify(thread);const card=context.threadCard(thread,false,true);assert.equal(JSON.stringify(thread),before);
 const walk=n=>[n,...n.children.flatMap(walk)];const nodes=walk(card);
 return {root:card,nodes,summary:nodes.find(n=>n.className.includes('work-summary')),title:nodes.find(n=>n.className==='session-title')};
}
test('changing current work is separate from a recognizable stable conversation title',()=>{
 const first=card(),second=card({checkpoint:{summary:'Making unread responses clearer. Details remain available.'}});
 assert.equal(first.title.children[0].textContent,'Chat interface');assert.equal(second.title.children[0].textContent,'Chat interface');
 assert.equal(first.summary.textContent,'Adding thread mentions.');assert.equal(second.summary.textContent,'Making unread responses clearer. Details remain available.');
 assert.equal(second.summary.title,second.summary.textContent);
});
test('completed, failed and interrupted results never imply ongoing execution',()=>{
 for(const [extra,prefix] of [[{work_phase:'finished',checkpoint:{summary:'Implemented thread mentions.'}},'Completed: '],
  [{turn_completion:{status:'failed'}},'Failed: '],[{turn_completion:{status:'interrupted'}},'Stopped: ']])assert.ok(card(extra).summary.textContent.startsWith(prefix));
 assert.equal(card({status:'running',turn_completion:{status:'failed'}}).summary.textContent,'Adding thread mentions.');
});
test('old conversations without checkpoints retain a useful original-request fallback',()=>{
 assert.equal(card({checkpoint:null,original_request:'Help with the chat interface'}).summary.textContent,'Help with the chat interface');
});
test('Attention preserves the required next action and checkpoint freshness warning',()=>{
 const result=card({response_state:'action',attention_reason:'review',checkpoint_stale:true,
  checkpoint:{summary:'Installer is staged.',next_actor:'user',next_action:'Approve the staged installer.'}});
 assert.equal(result.nodes.find(n=>n.className==='next-label').textContent,'Your next step · ');
 assert.equal(result.nodes.find(n=>n.className==='next-action').title,'Approve the staged installer.');
 assert.equal(result.nodes.find(n=>n.className==='stale-checkpoint').textContent,'New activity since this checkpoint');
});
test('Attention explains why a response is present, including failures and reminders',()=>{
 for(const [extra,label] of [
  [{response_state:'input',attention_reason:'blocked'},'Blocked'],
  [{response_state:'action',attention_reason:'review'},'Review requested'],
  [{response_state:'failed',attention_reason:'blocked'},'Failed'],
  [{attention_reason:'done',work_phase:'finished'},'Done'],
  [{snooze_due:true,attention_reason:'snooze'},'Reminder'],
  [{attention_reason:'findings'},'Findings ready'],
 ]) {
  const result=card(extra),status=result.nodes.find(n=>n.className.split(' ').includes('card-state'));
  assert.equal(status?.textContent,label);
  assert.ok(!status.className.includes('sr-only'));
 }
});
test('card actions share one group and remain outside the navigation button',()=>{
 const result=card({can_handle_response:true});
 const controls=result.root.children.find(n=>n.className==='card-actions');
 assert.ok(controls.children.some(n=>n.className==='card-pin'));
 assert.ok(controls.children.some(n=>n.className==='card-close'));
 assert.ok(controls.children.some(n=>n.dataset.action==='more'));
 const walk=n=>[n,...n.children.flatMap(walk)];
 for(const button of result.nodes.filter(n=>n.tag==='button'))
  assert.ok(walk(button).slice(1).every(n=>n.tag!=='button'));
});
