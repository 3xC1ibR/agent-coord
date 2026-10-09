'use strict';
const test=require('node:test'), assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
const {ThreadMentionDraft,ThreadMentions}=require('../plugins/agent-coord/scripts/agent_coord/web/thread-mentions.js');
const one={thread_id:'one',title:'Chat UI',client:'codex',attention:'now',cwd:'/project/one'};
const two={...one,thread_id:'two',attention:'archived',cwd:'/project/two'};
function draft() {const d=new ThreadMentionDraft('Ask @');d.select(4,5,one);return d;}
test('multiple same-name selections retain independent IDs across rename and UTF-16 offsets',()=>{
 const d=draft();d.edit(d.text.length,d.text.length,'and @');d.select(d.text.length-1,d.text.length,two);
 one.title='Renamed';assert.deepEqual(d.snapshot().map(r=>r.session_id),['one','two']);one.title='Chat UI';
 assert.equal(d.refs[0].text,'@Chat UI');d.edit(0,0,'😀 ');assert.equal(d.refs[0].start,7);
});
test('edits before references shift them; edits inside or removal drop bindings without retargeting duplicates',()=>{
 const d=draft();d.edit(d.text.length,d.text.length,'@');d.select(d.text.length-1,d.text.length,two);
 d.update(d.text.slice(0,4)+d.text.slice(12),{start:4,end:12});assert.deepEqual(d.refs.map(r=>r.session_id),['two']);
 const ref=d.refs[0];d.edit(ref.start+1,ref.start+2,'x');assert.equal(d.refs.length,0);
});
test('ordinary literal text and unknown programmatic replacements never create or infer references',()=>{
 const d=new ThreadMentionDraft('mail@example.com @Chat UI');assert.equal(d.snapshot().length,0);
 const selected=draft();selected.update('Ask @Chat UI renamed');assert.equal(selected.snapshot().length,0);
});
function harness() {
 const doc={activeElement:null,createElement:tag=>element(tag),addEventListener(){}};
 function element(tag){return {tag,ownerDocument:doc,attrs:{},children:[],listeners:{},hidden:true,value:'',selectionStart:0,selectionEnd:0,
 setAttribute(k,v){this.attrs[k]=v;},getAttribute(k){return this.attrs[k];},removeAttribute(k){delete this.attrs[k];},
 addEventListener(k,fn){(this.listeners[k]||=[]).push(fn);},append(...nodes){this.children.push(...nodes);},replaceChildren(...nodes){this.children=nodes;},
 focus(){doc.activeElement=this;},setSelectionRange(a,b){this.selectionStart=a;this.selectionEnd=b;}};}
 const input=element('textarea'),menu=element('div'),references=element('div');menu.id='mentions';input.focus();
 let thread='draft', loads=0;
 const mentions=new ThreadMentions({input,menu,references,getThread:()=>thread,loadThreads:async()=>{loads++;return [one,two];}});
 function edit(start,end,replacement) {input.setSelectionRange(start,end);for(const fn of input.listeners.beforeinput||[])fn({inputType:'insertText'});
 input.value=input.value.slice(0,start)+replacement+input.value.slice(end);input.setSelectionRange(start+replacement.length,start+replacement.length);
 for(const fn of input.listeners.input||[])fn({});}
 return {input,menu,references,mentions,edit,switch:id=>thread=id,loads:()=>loads};
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
test('autocomplete includes closed and duplicate names, keyboard/touch choice, Escape dismissal and email exclusion',async()=>{
 const h=harness();h.edit(0,0,'Ask @Chat');await settle();
 assert.equal(h.menu.hidden,false);assert.equal(h.menu.children.length,2);
 assert.match(h.menu.children[1].children[1].textContent,/Closed.*project\/two.*two/);
 let prevented=0;const event=key=>({key,preventDefault(){prevented++;}});
 assert.equal(h.mentions.keydown(event('ArrowDown')),true);h.mentions.keydown(event('Enter'));
 assert.equal(h.input.value,'Ask @Chat UI ');assert.equal(h.mentions.snapshot()[0].session_id,'two');assert.equal(h.references.children.length,1);
 h.edit(h.input.value.length,h.input.value.length,'and @');await settle();h.mentions.keydown(event('Escape'));
 h.mentions.render();assert.equal(h.menu.hidden,true);
 h.edit(0,h.input.value.length,'me@example.com');assert.equal(h.menu.hidden,true);assert.equal(h.mentions.snapshot().length,0);
 h.edit(0,h.input.value.length,'@Chat');await settle();h.menu.children[0].onclick();assert.equal(h.mentions.snapshot()[0].session_id,'one');
 h.references.children[0].onclick();assert.equal(h.mentions.snapshot().length,0);assert.equal(h.input.value,' ');
 assert.ok(prevented>=3);
});
test('draft switches, pane restore, send failure and edits during send keep exact associations',async()=>{
 const h=harness();h.edit(0,0,'@Chat');await settle();h.mentions.choose(one);
 const text=h.input.value,refs=h.mentions.snapshot();
 h.switch('other');h.input.value='plain';h.mentions.render();assert.equal(h.mentions.snapshot().length,0);
 h.switch('draft');h.input.value=text;h.mentions.render();assert.deepEqual(h.mentions.snapshot(),refs);
 h.mentions.restore('draft',text,refs);assert.deepEqual(h.mentions.snapshot(),refs);
 h.edit(text.length,text.length,'new instruction');h.mentions.sent('draft',text,refs);assert.deepEqual(h.mentions.snapshot(),refs);
 h.mentions.sent('draft',h.input.value,refs);h.input.value='';h.mentions.render();assert.equal(h.mentions.snapshot().length,0);
});
const source=fs.readFileSync(require.resolve('../plugins/agent-coord/scripts/agent_coord/web/app.js'),'utf8');
test('normal send and queue include selected references and failed submission retains them',async()=>{
 for(const mode of ['steer','queue']) {
  const d=draft(),input={value:d.text};const calls=[];
  const state={selected:'receiver',detail:{work_thread:{browser_session:true,client:'codex',attention:'now'},running:false},
    mentions:{snapshot:()=>d.snapshot(),sent:()=>{d.refs=[];}},drafts:new Map(),commandFeedback:new Map()};
  const ctx=vm.createContext({state,$:()=>input,sessionPath:id=>'sessions/'+id,isSessionDraft:()=>false,
    renderStatus(){},refreshDetail:async()=>{},refreshList:async()=>{},api:async(path,body)=>{calls.push({path,body});return{};}});
  vm.runInContext(source.slice(source.indexOf('async function sendMessage('),source.indexOf('$("composer").onsubmit')),ctx);
  const refs=d.snapshot();await ctx.sendMessage(mode);assert.deepEqual(calls[0].body.mentions,refs);
  assert.equal(calls[0].path,mode==='queue'?'sessions/receiver/queue':'sessions/receiver/messages');
  input.value='@Chat UI';d.text=input.value;d.refs=[{start:0,end:8,text:input.value,session_id:'one'}];
  ctx.api=async()=>{throw Error('Send failed');};await assert.rejects(ctx.sendMessage(),/Send failed/);assert.equal(d.refs.length,1);
 }
});
test('appending letters to a selected name clears its identity while punctuation and surrounding instructions preserve it',()=>{
 const d=draft();d.edit(d.refs[0].end,d.refs[0].end,'Changed');assert.equal(d.refs.length,0);
 const safe=draft();safe.edit(safe.refs[0].end,safe.refs[0].end,', please');assert.equal(safe.refs.length,1);
});
test('actual conversation navigation restores textarea before rendering mention state',async()=>{
 const h=harness();h.switch('first');h.mentions.restore('first','first text');h.input.value='first text';
 const d=draft();h.mentions.restore('second',d.text,d.snapshot());
 const controls=new Map([['message',h.input]]);
 const $=id=>{if(!controls.has(id))controls.set(id,{value:'',dataset:{},classList:{remove(){},add(){}},replaceChildren(){}});return controls.get(id);};
 const state={selected:'first',detail:{},drafts:new Map([['second',d.text]]),closing:new Set(),conversations:new Map()};
 const ctx=vm.createContext({state,$,savedViews:null,navigation:null,notifications:null,history:{replaceState(){}},
  retainConversation(){},setNavigation(){},conversationEntry:()=>({detail:null}),
  renderStatus(){h.switch(state.selected);h.mentions.render();},renderTitle(){},renderList(){},node:()=>({}),refreshDetail:async()=>false});
 vm.runInContext(source.slice(source.indexOf('async function select('),source.indexOf('async function refreshDetail(')),ctx);
 await ctx.select('second');assert.deepEqual(h.mentions.snapshot('second',d.text),d.snapshot());
});
test('chat history displays readable references while literal or malformed mapping text stays intact',()=>{
 const {displayMessage}=require('../plugins/agent-coord/scripts/agent_coord/web/thread-mentions.js');
 const d=draft(),marker='\n\n[Selected thread references; positions are UTF-16 offsets in the user text above]\n';
 assert.deepEqual(displayMessage(d.text+marker+JSON.stringify(d.refs)),{text:d.text,refs:d.refs});
 for(const suffix of ['ordinary text','[]',JSON.stringify([{...d.refs[0],text:'@Different'}])]) {
  const text=d.text+marker+suffix;assert.deepEqual(displayMessage(text),{text,refs:[]});
 }
});
test('schedule editor restores selected identities and sends its edited snapshot through the existing schedule path',async()=>{
 const h=harness(),doc=h.input.ownerDocument,controls=new Map(),calls=[];
 doc.getElementById=id=>{if(!controls.has(id)){
  const el=doc.createElement('input');el.id=id;el.open=false;
  el.showModal=()=>el.open=true;el.close=()=>el.open=false;
  Object.defineProperty(el,'options',{get:()=>el.children.flatMap(n=>n.tag==='optgroup'?n.children:[n])});
  controls.set(id,el);
 }return controls.get(id);};
 const module={exports:{}};
 const ctx=vm.createContext({module,ThreadMentions,crypto:require('node:crypto').webcrypto,Date,Intl,setInterval,clearInterval});
 vm.runInContext(fs.readFileSync(require.resolve('../plugins/agent-coord/scripts/agent_coord/web/schedules.js'),'utf8'),ctx);
 const d=draft();
 const ui=new module.exports.ScheduledPromptsUI({document:doc,getDraft:()=>({message:d.text,mentions:d.refs,settings:{cwd:'/repo',model:'available-model',client:'codex'}}),onError(){},
  api:async(path,body)=>{calls.push({path,body});return {data:path.startsWith('models?')?[{model:'available-model',supportedReasoningEfforts:[{reasoningEffort:'medium'}]}]:[]};}});
 ui.showList=async()=>{};await ui.open();assert.deepEqual(ui.mentions.snapshot(),d.refs);
 await ui.save();const post=calls.find(c=>c.path==='schedules');assert.deepEqual(post.body.mentions,d.refs);
 const item={id:'saved',version:1,message:d.text,mentions:d.refs,settings:{cwd:'/repo',model:'available-model'},run_at:Date.now()/1000+86400};
 await ui.open(item);doc.getElementById('schedule-message').value='Edited literal @Chat UI';await ui.save();
 assert.equal(calls.find(c=>c.path==='schedules/saved').body.mentions,undefined);
});
test('first message in a new conversation carries resolved mentions and clears the submitted draft',async()=>{
 const h=harness(),d=draft(),calls=[];h.switch('new-session');h.input.value=d.text;h.mentions.restore('new-session',d.text,d.refs);
 const state={selected:'new-session',newSessionDraft:{session:{client:'codex'}},drafts:new Map(),commandFeedback:new Map(),mentions:h.mentions};
 const ctx=vm.createContext({state,$:()=>h.input,sessionPath:id=>'sessions/'+id,isSessionDraft:()=>state.selected==='new-session',
  api:async(path,body)=>{calls.push({path,body});return path==='sessions'?{session:{thread_id:'created'}}:{};},
  select:async id=>{state.selected=id;h.switch(id);h.input.value=state.drafts.get(id)||'';},refreshList:async()=>{}});
 vm.runInContext(source.slice(source.indexOf('async function sendSessionDraft('),source.indexOf('$("new-session").onclick',source.indexOf('async function sendSessionDraft('))),ctx);
 await ctx.sendSessionDraft(d.text,[],d.refs);
 assert.deepEqual(calls[1].body.mentions,d.refs);assert.equal(calls[1].path,'sessions/created/messages');
 assert.equal(h.mentions.snapshot('created','').length,0);assert.equal(state.newSessionDraft,null);
});
