const test = require('node:test');
const assert = require('node:assert/strict');
const {VisitRecorder, bind, surfaceOf, SURFACES} = require('../plugins/agent-coord/scripts/agent_coord/web/activity-collector.js');
const {localRange, queryString, reviewFilters, coverageText} = require('../plugins/agent-coord/scripts/agent_coord/web/activity.js');
function fixture(options = {}) {
  let sequence = 0, clock = 1000;
  const sent = [], recorder = new VisitRecorder({windowId:'window-a', uuid:()=>'id-'+ ++sequence,
    now:()=>clock, send:async batch=>sent.push(batch), ...options});
  recorder.setEnabled(true); recorder.setForeground(true);
  return {recorder, sent, advance: () => clock++};
}
test('switching threads ends the previous visit and a no-input return creates a new visit', () => {
  const {recorder:r, advance} = fixture();
  r.select('alpha','navigation'); const first = r.current.visit_id;
  r.select('alpha','navigation'); assert.equal(r.queue.length,1);
  advance(); r.select('beta','navigation'); advance(); r.select('alpha','navigation'); r.select(null);
  assert.deepEqual(r.queue.map(e=>[e.thread_id,e.kind]), [
    ['alpha','visit_started'],['alpha','visit_ended'],['beta','visit_started'],['beta','visit_ended'],['alpha','visit_started'],['alpha','visit_ended']]);
  assert.notEqual(r.queue[4].visit_id,first);
  assert.equal(r.queue[4].visit_id,r.queue[5].visit_id);
  assert.ok(r.queue.every(e=>e.source==='navigation' && e.window_id==='window-a'));
});
test('refresh, restore and programmatic selection do not fabricate human visits', () => {
  const {recorder:r} = fixture();
  r.select('alpha','restore'); r.select('alpha','programmatic'); assert.equal(r.queue.length,1); assert.equal(r.current.source,'restore');
  r.setForeground(false); r.select('beta','navigation'); assert.equal(r.queue.length,2);
  r.setForeground(true); assert.equal(r.queue.length,2);
  r.interact('beta','focus'); assert.equal(r.queue.length,3);
  r.setForeground(false); assert.equal(r.queue.at(-1).kind,'visit_ended');
  const fresh = fixture().recorder; fresh.select('beta','restore'); assert.equal(fresh.queue.length,1); assert.equal(fresh.current.source,'restore');
});
test('trusted navigation intent expires and is consumed once', () => {
  const {recorder:r, advance} = fixture();
  r.markIntent(); assert.equal(r.source(),'navigation'); assert.equal(r.source(),'programmatic');
  r.markIntent(); advance(); assert.equal(r.source(),'programmatic');
});
function inside(...selectors) {
  return {closest: query => query.split(',').some(part => selectors.includes(part.trim())) ? {} : null};
}
test('a deliberate navigation reports the surface where it began', () => {
  const cases = [
    [['#sessions', '#sidebar'], 'sidebar'], [['#overview', '#welcome'], 'cards'], [['#working-set-overview', '#welcome'], 'working-set'],
    [['#conversation-results', '#welcome'], 'search'], [['#conversation-tabs'], 'tabs'], [['#thread-hover'], 'hover-card'],
    [['#roll-up-bar'], 'roll-up'], [['.asks-dialog'], 'decisions'], [['#dispatcher-workspace'], 'dispatcher'], [[], 'other'],
  ];
  for (const [selectors, surface] of cases) assert.equal(surfaceOf({type: 'pointerdown', target: inside(...selectors)}), surface, surface);
  assert.equal(surfaceOf({type: 'keydown', metaKey: true, target: inside('#sessions')}), 'shortcut');
  assert.equal(surfaceOf({type: 'keydown', key: 'Enter', target: inside('#sessions')}), 'sidebar');
  const {recorder: r} = fixture();
  r.markIntent('sidebar'); const source = r.source(); r.source(); r.select('alpha', source);
  assert.equal(r.current.surface, 'sidebar');
  r.select('beta', 'navigation'); r.interact('beta', 'focus'); r.select('gamma', 'programmatic');
  assert.deepEqual(r.queue.map(e => [e.thread_id, e.kind, e.surface ?? null]), [
    ['alpha', 'visit_started', 'sidebar'], ['alpha', 'visit_ended', 'sidebar'],
    ['beta', 'visit_started', null], ['beta', 'visit_ended', null], ['gamma', 'visit_started', null]]);
  // A deliberate selection upgrades the observed system visit and keeps its own surface.
  r.markIntent('notification'); r.select('gamma', r.source());
  assert.deepEqual([r.current.source, r.current.surface], ['navigation', 'notification']);
});
test('every surface the collector reports is accepted by the activity API', () => {
  const source = require('node:fs').readFileSync(require('node:path').join(__dirname, '../plugins/agent-coord/scripts/agent_coord/activity_api.py'), 'utf8');
  const accepted = new Set([...source.match(/NAVIGATION_SURFACES = frozenset\(\{([^}]*)\}\)/)[1].matchAll(/"([^"]+)"/g)].map(match => match[1]));
  for (const surface of [...SURFACES.map(([, name]) => name), 'notification', 'shortcut', 'other']) assert.ok(accepted.has(surface), surface);
});
test('pause and clear do not revive visits until deliberate interaction', () => {
  const {recorder:r} = fixture(); r.select('alpha','navigation'); r.setEnabled(false);
  assert.equal(r.queue.at(-1).kind,'visit_ended'); r.interact('alpha'); assert.equal(r.current,null);
  r.setEnabled(true); assert.equal(r.current.source,'programmatic'); r.interact('alpha'); assert.equal(r.current.source,'focus');
  r.clear(); assert.deepEqual(r.queue,[]); assert.equal(r.current,null);
});
test('submission metadata is stable per body, has no input copy, and cannot attribute another thread', () => {
  const {recorder:r} = fixture(); r.select('alpha','navigation');
  const body={message:'private text'}, first=r.submission('alpha',body).activity;
  assert.deepEqual(r.submission('alpha',body).activity,first);
  assert.deepEqual(Object.keys(first).sort(),['event_id','visit_id','window_id']);
  assert.equal(first.visit_id,r.current.visit_id);
  assert.equal(r.submission('beta',{message:'elsewhere'}).activity.visit_id,null);
  assert.notEqual(r.submission('alpha',{message:'next'}).activity.event_id,first.event_id);
  assert.ok(!JSON.stringify(r.queue).includes('private text'));
});
test('bounded retries retain event IDs and do not remove newly queued events', async () => {
  const batches=[]; let fail=true, resolve;
  const {recorder:r}=fixture({limit:4,send:async batch=>{batches.push(batch);if(fail)throw Error('offline');await new Promise(done=>resolve=done);}});
  r.select('alpha','navigation'); await r.flush(); const id=r.queue[0].event_id;
  fail=false; const pending=r.flush(); await Promise.resolve();
  r.select('beta','navigation'); resolve(); await pending;
  assert.equal(batches[1][0].event_id,id); assert.equal(r.queue.length,2);
  r.select('gamma','navigation'); r.select('delta','navigation'); assert.equal(r.queue.length,4);
});
test('pane bind borrows the shell owner and never creates its own timer or sender', () => {
  const recorder=fixture().recorder;
  const child={parent:{agentCoordActivity:recorder}};
  assert.equal(bind({window:child,document:{},state:{paneMode:true},api:()=>assert.fail('pane sent telemetry')}),recorder);
});
test('local inclusive dates honor daylight saving boundaries', () => {
  const previous=process.env.TZ; process.env.TZ='America/Chicago';
  try {
    const spring=localRange('2026-03-08','2026-03-08'); assert.equal(spring.end-spring.start,23*3600);
    const fall=localRange('2026-11-01','2026-11-01'); assert.equal(fall.end-fall.start,25*3600);
    assert.throws(()=>localRange('2026-02-30','2026-03-01'),/valid/);
    assert.throws(()=>localRange('2026-03-09','2026-03-08'),/end date/);
  } finally { if(previous===undefined)delete process.env.TZ;else process.env.TZ=previous; }
});
test('query and review preserve unassigned filters and a fixed cutoff', () => {
  const query={start:100,end:1000,as_of:900,group_by:'domain',filters:{project:'/work/a',domain:'',group:null,allowed_cwd:'/work'}};
  const filters=reviewFilters(query); assert.deepEqual(filters,{project:'/work/a',domain:'',start:100,end:900,group_by:'domain'});
  const params=new URLSearchParams(queryString(filters,'page/2'));
  assert.equal(params.get('domain'),''); assert.equal(params.has('group'),false);
  assert.equal(params.get('cursor'),'page/2'); assert.equal(params.get('end'),'900');
});
test('coverage communicates pauses, cleared history and incomplete visit limitations', () => {
  const text=coverageText({enabled:false,history_cleared_at:100,paused_intervals:[{start:1,end:2}]},{incomplete_visits:3});
  assert.match(text,/paused/); assert.match(text,/cleared/); assert.match(text,/3 incomplete visits/);
  assert.match(coverageText({complete:false},{}),/partial/);
});

function browserFixture({hidden = false} = {}) {
  const listeners = () => ({handlers:new Map(), addEventListener(name,fn) { const rows=this.handlers.get(name)||[];rows.push(fn);this.handlers.set(name,rows); }, fire(name,event={}) {for(const fn of this.handlers.get(name)||[])fn(event);} });
  const doc=Object.assign(listeners(),{hidden,focused:!hidden,hasFocus(){return this.focused;},querySelector(){return null;}});
  const view=Object.assign(listeners(),{setInterval(fn){this.tick=fn;return 1;},clearInterval(){},setTimeout(fn){fn();}});
  const state={selected:'alpha',sourceWindowId:'owner',paneMode:false};const sends=[];
  const recorder=bind({window:view,document:doc,state,api:async(path,body)=>{if(body)sends.push(body);return {enabled:true};}});
  const interact=(trusted=true)=>doc.fire('pointerdown',{isTrusted:trusted,target:{closest:()=>true}});
  return {doc,view,state,recorder,sends,interact};
}
test('browser binding ignores initial background restore and synthetic interaction',async()=>{
  const f=browserFixture({hidden:true});await Promise.resolve();
  f.doc.hidden=false;f.doc.focused=true;f.doc.fire('visibilitychange');
  assert.equal(f.recorder.queue.length,1); assert.equal(f.recorder.current.source,'restore');
  f.interact(false);await Promise.resolve();assert.equal(f.recorder.queue.length,1);
  f.interact();await Promise.resolve();assert.equal(f.recorder.queue.length,3); assert.equal(f.recorder.current.source,'focus');
  f.doc.hidden=true;f.doc.focused=false;f.doc.fire('visibilitychange');
  assert.equal(f.recorder.current,null);
});
test('browser binding names the surface of a trusted sidebar selection',async()=>{
  const f=browserFixture();await Promise.resolve();
  f.doc.fire('pointerdown',{type:'pointerdown',isTrusted:true,target:inside('#sessions','#sidebar')});await Promise.resolve();
  f.recorder.select('beta',f.recorder.source());
  assert.deepEqual([f.recorder.current.thread_id,f.recorder.current.source,f.recorder.current.surface],['beta','navigation','sidebar']);
});
test('moving focus into an owned pane does not split the current visit; window return does',async()=>{
  const f=browserFixture();await Promise.resolve();f.interact();await Promise.resolve();
  const original=f.recorder.current.visit_id;
  f.view.fire('blur');assert.equal(f.recorder.current.visit_id,original);
  f.view.agentCoordPanes={active:true,selected:'beta'};
  f.recorder.select('beta','navigation');const paneVisit=f.recorder.current.visit_id;
  f.view.fire('blur');assert.equal(f.recorder.current.visit_id,paneVisit);
  f.doc.focused=false;f.view.fire('blur');assert.equal(f.recorder.current,null);
  f.doc.focused=true;f.view.fire('focus');assert.equal(f.recorder.current.thread_id,'beta');assert.notEqual(f.recorder.current.visit_id,paneVisit);
});
test('restoring or first tiling a layout identifies programmatic focus separately from selecting a pane',()=>{
  const {ThreadPanes}=require('../plugins/agent-coord/scripts/agent_coord/web/thread-panes.js');
  const focused=[];
  const shell=Object.assign(Object.create(ThreadPanes.prototype),{layouts:new Map(),records:new Map(),active:false,
    container:{hidden:true,getBoundingClientRect:()=>({width:900,height:700})},record(){},render(){},
    onFocus:(id,options)=>focused.push({id,...options})});
  shell.tile('view',[{thread_id:'alpha',attention:'now'},{thread_id:'beta',attention:'now'}]);
  assert.deepEqual(focused,[{id:'alpha',restored:true}]);
  shell.active=false;shell.show('view');assert.deepEqual(focused[1],{id:'alpha',restored:true});
});
test('tiling from the main composer does not count the automatically selected pane',async()=>{
  const f=browserFixture();await Promise.resolve();
  f.interact();f.view.agentCoordPanes={active:true,selected:'beta'};
  await Promise.resolve();assert.equal(f.recorder.queue.length,0);
});
test('a foreground system exposure upgrades to a separate human visit and cannot own message attribution',()=>{
  const {recorder:r}=fixture();r.select('alpha','restore');const exposure=r.current.visit_id;
  assert.equal(r.submission('alpha',{message:'accepted'}).activity.visit_id,null);
  r.interact('alpha','focus');assert.notEqual(r.current.visit_id,exposure);
  assert.deepEqual(r.queue.map(e=>[e.kind,e.source]),[['visit_started','restore'],['visit_ended','restore'],['visit_started','focus']]);
  assert.equal(r.submission('alpha',{message:'accepted next'}).activity.visit_id,r.current.visit_id);
  r.select('alpha','programmatic');assert.equal(r.current.source,'focus');
});
