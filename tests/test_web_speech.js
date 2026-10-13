'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const {SpeechController, voiceChoices} = require('../plugins/agent-coord/scripts/agent_coord/web/speech.js');
const tick = () => new Promise(resolve => setImmediate(resolve));
const turn = (id, status='completed') => ({id, status, items:[{type:'agentMessage',text:'Reply '+id}]});
const detail = (turns=[], id='chat') => ({work_thread:{thread_id:id,browser_session:true,title:'Chat'},thread:{turns}});
test('voice choices preserve available names and keep the saved selection without changing the catalogue',()=>{
  const voices=Object.freeze([{id:'af_heart',name:'Heart'}, {id:'bf_emma',name:'Emma'}]);
  assert.deepEqual(voiceChoices(voices,'bf_emma'),voices);
  assert.deepEqual(voiceChoices(voices,'af_heart+bf_emma'),[
    {id:'af_heart+bf_emma',name:'af_heart+bf_emma (saved)'},...voices]);
  assert.deepEqual(voiceChoices([],'bf_emma'),[{id:'bf_emma',name:'bf_emma (saved)'}]);
  assert.equal(voices.length,2);
});
function fixture(extra={}) {
  const store = new Map(), requests=[], revoked=[];
  const audio={play:async()=>{}, pause(){}, removeAttribute(){}, load(){}};
  const storage={getItem:k=>store.get(k),setItem:(k,v)=>store.set(k,v)};
  const request=async(path, body)=>{requests.push({path,body});return new Blob(['audio']);};
  const controller=new SpeechController({audio,storage,request,url:{createObjectURL:()=> 'blob:audio',revokeObjectURL:u=>revoked.push(u)},...extra});
  return {controller,audio,requests,store,revoked};
}
test('opt in reads only newly completed replies once, without replaying history or commentary',async()=>{
  const f=fixture(), c=f.controller;
  c.update('chat',detail([turn('old')]));c.toggle();
  c.update('chat',detail([turn('old'),turn('new','inProgress')]));assert.equal(f.requests.length,0);
  c.update('chat',detail([turn('old'),turn('new')]));await tick();assert.equal(f.requests.length,1);
  c.update('chat',detail([turn('old'),turn('new')]));await tick();assert.equal(f.requests.length,1);
  assert.equal(f.requests[0].body.turn_id,'new');f.audio.onended();await tick();assert.equal(c.current,null);assert.equal(f.revoked.length,1);
  c.update('chat',detail([turn('old'),turn('new'),turn('failed','failed')]));assert.equal(f.requests.length,1);
});
test('autoplay rejection keeps a playable reply; disabling releases it and clears the queue',async()=>{
  const f=fixture(),c=f.controller; f.audio.play=async()=>{throw Error('NotAllowedError');};
  c.update('chat',detail());c.toggle();c.update('chat',detail([turn('one'),turn('two')]));await tick();
  assert.equal(c.retry,true);assert.match(c.message,/Tap Play/);assert.equal(f.requests.length,1);
  f.audio.play=async()=>{};c.retryCurrent();await tick();assert.equal(c.retry,false);
  c.toggle();await tick();assert.equal(c.current,null);assert.equal(c.queue.length,0);assert.equal(f.requests.length,1);
});
test('cancel while generating never plays a late response',async()=>{
  let resolve, played=0;const f=fixture({request:()=>new Promise(r=>resolve=r)}),c=f.controller;
  f.audio.play=async()=>played++;
  c.update('chat',detail());c.toggle();c.update('chat',detail([turn('one')]));c.stop();
  resolve(new Blob(['late']));await tick();assert.equal(played,0);assert.equal(c.current,null);assert.equal(c.pumping,false);
});
test('completion in another chat is read and replayed notifications are deduplicated',async()=>{
  const f=fixture(), c=f.controller;c.update('chat',detail());c.toggle();c.update('elsewhere',detail([], 'elsewhere'));
  c.request=async(path,body)=>{f.requests.push({path,body});return path.startsWith('sessions/')?detail([turn('new')]):new Blob(['audio']);};
  c.receive([{thread_id:'chat',status:'completed'}]);await tick();assert.equal(c.current.thread,'chat');
  f.audio.onended();await tick();c.receive([{thread_id:'chat',status:'completed'}]);await tick();
  assert.equal(f.requests.filter(r=>r.path==='speech/audio').length,1);
});
test('Kokoro failure has a retry and survives a successful retry',async()=>{
  const f=fixture(), c=f.controller;let count=0;
  c.request=async()=>{if(!count++)throw Error('Start Kokoro');return new Blob(['audio']);};
  c.update('chat',detail());c.toggle();c.update('chat',detail([turn('new')]));await tick();
  assert.equal(c.retry,true);assert.match(c.message,/Start Kokoro/);
  c.retryCurrent();await tick();assert.equal(count,2);assert.match(c.message,/Reading aloud/);c.stop();await tick();
});
test('two tabs share playback claims under a browser lock',async()=>{
  let tail=Promise.resolve();const locks={request:(_,fn)=>{const next=tail.then(fn);tail=next;return next;}};
  const a=fixture({locks}),b=fixture({locks,storage:{getItem:k=>a.store.get(k),setItem:(k,v)=>a.store.set(k,v)}});
  for(const f of [a,b])f.controller.update('chat',detail());
  a.controller.toggle();
  a.controller.update('chat',detail([turn('new')]));b.controller.update('chat',detail([turn('new')]));await tick();
  assert.equal(a.requests.length+b.requests.length,1);a.audio.onended();await tick();assert.equal(a.requests.length+b.requests.length,1);
});
test('stopping while waiting for another tab never starts the cancelled audio',async()=>{
  let release;const locks={request:async(_,fn)=>{await new Promise(r=>release=r);return fn();}};
  const f=fixture({locks}),c=f.controller;
  c.update('chat',detail());c.toggle();c.update('chat',detail([turn('new')]));c.stop();release();await tick();
  assert.equal(f.requests.length,0);assert.equal(c.pumping,false);
});
