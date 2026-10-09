'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const grouping=require('../plugins/agent-coord/scripts/agent_coord/web/thread-groups.js');
const source=fs.readFileSync(require.resolve('../plugins/agent-coord/scripts/agent_coord/web/app.js'),'utf8');
function render(extra={},compact=false,attention=true){
 class Element{
  constructor(tag,text=null,className=''){Object.assign(this,{tag,textContent:text,className,children:[],dataset:{},attrs:{}});}
  append(...nodes){this.children.push(...nodes);}setAttribute(k,v){this.attrs[k]=v;}
  get lastChild(){return this.children.at(-1);}insertBefore(node,before){this.children.splice(this.children.indexOf(before),0,node);}
  replaceChildren(...nodes){this.children=nodes;}
 }
 const thread={thread_id:'one',title:'Blocked work',attention:'now',response_state:'input',status:'idle',links:[],
  turn_completion:{id:8,status:'completed'},unread_result:true,unread:true,unhandled_response:true,
  checkpoint:{summary:'Need an answer',next_actor:'user',next_action:'Choose a target'},...extra};
 const before=JSON.stringify(thread);
 const context={threadGrouping:grouping, state:{selected:null,closing:new Set(),pinning:new Set(),updatingThreads:new Set()},
  node:(...args)=>new Element(...args),document:{createElementNS:(_,tag)=>new Element(tag),createTextNode:text=>new Element('text',text)},
  associationLabel:()=> 'Workspace',relativeTime:()=> 'now',action:f=>f(),select(){},snoozeLabel:()=> '',isDueSnooze:()=> false};
 vm.createContext(context);vm.runInContext(source.slice(source.indexOf('function threadCard('),source.indexOf('function groupHeading(')),context);
 const card=context.threadCard(thread,compact,attention);
 assert.equal(JSON.stringify(thread),before,'rendering does not mutate read/action state');
 const walk=node=>[node,...node.children.flatMap(walk)];
 return {nodes:walk(card),button:walk(card).find(n=>n.dataset.thread==='one')};
}
test('overview and Attention cards use styling and accessible descriptions for response read status',()=>{
 for(const attention of [false,true])for(const compact of [false,true]){
  const unread=render({},compact,attention),read=render({unread_result:false,unread:false},compact,attention);
  assert.match(unread.button.className,/response-unread/);assert.match(read.button.className,/response-read\b/);
  assert.equal(unread.button.attrs['aria-description'],'Unread response');assert.equal(read.button.attrs['aria-description'],'Read response');
  for(const result of [unread,read])assert.equal(result.nodes.some(n=>/read response/i.test(n.textContent||'')),false);
 }
});
test('a read unresolved blocker stays classified and a new response becomes unread again',()=>{
 const read=render({unread_result:false});assert.match(read.button.className,/needs-attention/);
 assert.equal(read.nodes.some(n=>n.className.startsWith('attention-reason')),false);
 const unread=render({turn_completion:{id:9,status:'completed'},unread_result:true});assert.match(unread.button.className,/response-unread/);
 const dismissed=render({unhandled_response:false,response_state:'available'});assert.match(dismissed.button.className,/response-unread/);
});
test('checkpoint-only updates, interrupted turns and new threads do not pretend to be read responses',()=>{
 for(const turn_completion of [null,{status:'interrupted'}]){
  const card=render({turn_completion,unread_result:false,unread:true});assert.equal(card.button.attrs['aria-description'],undefined);
  assert.doesNotMatch(card.button.className,/response-(unread|read)\b/);
 }
});
test('overview link counts are omitted without changing the saved links',()=>{
 for(const attention of [false,true]){
  const card=render({links:[{target:'docs/design.md'}]},false,attention);
  assert.equal(card.nodes.some(n=>/^\d+ links?$/.test(n.textContent||'')),false);
 }
});

test('overview cards omit phase and repository eyebrows while retaining response state and activity time',()=>{
 for(const attention of [false,true])for(const compact of [false,true]){
  const card=render({},compact,attention);
  assert.equal(card.nodes.some(n=>['card-top','project-name','phase-label'].includes(n.className)),false);
  assert.match(card.button.className,/response-unread/);
  if(!compact)assert.ok(card.nodes.some(n=>n.className==='card-time'));
 }
});
