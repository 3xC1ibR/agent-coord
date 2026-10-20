"use strict";
const test = require('node:test'), assert = require('node:assert/strict');
const {fieldFromThreads, create} = require('../plugins/agent-coord/scripts/agent_coord/web/atlas.js');
const now = 1800000000000;
const thread = (id, extra = {}) => ({thread_id:id, title:'Thread '+id, project_cwd:'/work', cwd:'/work', attention:'now', status:'idle', response_state:'available', work_phase:'validation', domain:'web', created_at:now/1000-10000, updated_at:now/1000-100, ...extra});
function setup() {
  const doc = {activeElement:null, events:{}, addEventListener(type, fn) {this.events[type]=fn;}, removeEventListener(type) {delete this.events[type];}};
  class Element {
    constructor(tag) { this.tagName=tag; this.children=[]; this.attrs={}; this.dataset={}; this.style={}; this.events={}; this.isConnected=true; this.clientWidth=1000; this.clientHeight=700; this.offsetHeight=30; }
    get className() {return this.attrs.class || ''; } set className(value) {this.attrs.class=value;}
    get classList() { const read=()=>new Set(this.className.split(/\s+/).filter(Boolean)); return {
      contains:n=>read().has(n), toggle:(n,on)=>{const s=read(); if(on ?? !s.has(n))s.add(n);else s.delete(n);this.className=[...s].join(' ');},
      add:n=>{const s=read();s.add(n);this.className=[...s].join(' ');},remove:n=>{const s=read();s.delete(n);this.className=[...s].join(' ');}}; }
    append(...children) {for(const child of children){this.children.push(child);if(child instanceof Element){child.parent=this;child.isConnected=true;}}}
    replaceChildren(...children) {for(const c of this.children)if(c instanceof Element)c.isConnected=false;this.children=[];this.append(...children);}
    setAttribute(k,v) {this.attrs[k]=String(v);if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-([a-z])/g,(_,s)=>s.toUpperCase())]=String(v);}
    getAttribute(k) {return this.attrs[k] ?? null;}
    removeAttribute(k) {delete this.attrs[k];}
    addEventListener(type,fn) {(this.events[type] ||= []).push(fn);}
    emit(type,event={}) {for(const fn of this.events[type] || [])fn({preventDefault(){},stopPropagation(){},...event});}
    matches(selector) {if(selector===':hover')return !!this.hovered;return selector.startsWith('.')?selector.slice(1).split('.').every(n=>this.classList.contains(n)):this.tagName===selector;}
    querySelectorAll(selector) {return this.children.filter(c=>c instanceof Element).flatMap(c=>[...(selector.split(',').some(s=>c.matches(s.trim()))?[c]:[]),...c.querySelectorAll(selector)]);}
    querySelector(selector) {return this.querySelectorAll(selector)[0] || null;}
    contains(item) {return this===item || this.children.some(c=>c instanceof Element && c.contains(item));}
    focus() {doc.activeElement=this;}
    remove() {this.isConnected=false;if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);}
    getAnimations() {return [];}
    setPointerCapture() {}
  }
  doc.createElement = tag => new Element(tag); doc.createElementNS = (_,tag) => new Element(tag);
  const observers = [];
  const win = {performance:{now:()=>0},matchMedia:()=>({matches:true}),requestAnimationFrame:fn=>{fn(1000);return 1;},cancelAnimationFrame(){},
    ResizeObserver:class{constructor(fn){this.fn=fn;observers.push(this);}observe(){}disconnect(){this.disconnected=true;}}};
  const data=new Map(), storage={getItem:k=>data.get(k),setItem:(k,v)=>data.set(k,v)};
  const host=new Element('section'), opened=[];
  const ui=create({document:doc,window:win,storage,onOpen:id=>opened.push(id)});
  return {ui,doc,host,opened,data,observers, render:(threads,workspace='/work')=>ui.render(threads,host,{workspace,now})};
}
function sample() {return [thread('working',{status:'running',response_state:'working',work_phase:'implementation'}),
  thread('old',{work_phase:'finished',updated_at:now/1000-4*86400,checkpoint:{phase:'finished',created_at:now/1000-4*86400,summary:'Shipped'}}),
  thread('validator',{checkpoint:{phase:'validation',summary:'Ready for review'}}),
  thread('other',{domain:'backend',project_id:'release',project_name:'Release',work_phase:'finished'})];}
const byAgent=(host,id)=>host.querySelectorAll('.node').filter(n=>n.getAttribute('data-agent-id')===id);

test('live adapter uses runtime phase and attention, preserves group IDs, ignores stale next steps',()=>{
  const source=[thread('parent',{work_phase:'implementation',checkpoint:{phase:'finished',next_actor:'user',next_action:'Old ask'},checkpoint_stale:true}),
    thread('child',{creation_mode:'orchestration',created_by_thread_id:'parent',project_id:'g',project_name:'Launch'}),
    thread('fork',{creation_mode:'fork',created_by_thread_id:'parent'}), thread('hidden',{creation_mode:'orchestration',created_by_thread_id:'absent'})];
  const field=fieldFromThreads(source,{now});
  assert.equal(field.byId('parent').phase,'implementation'); assert.equal(field.needsUser(field.byId('parent')),false);
  assert.equal(field.byId('child').groupId,'g'); assert.equal(field.parent('child').id,'parent');assert.equal(field.parent('fork'),null);assert.equal(field.parent('hidden'),null);
  assert.deepEqual(field.roots().map(a=>a.id),['parent','fork','hidden']);
  const cycle=fieldFromThreads([thread('a',{creation_mode:'orchestration',created_by_thread_id:'b'}),thread('b',{creation_mode:'orchestration',created_by_thread_id:'a'})]);
  assert.equal(cycle.roots().flatMap(a=>[a,...cycle.descendants(a.id)]).length,2);
});
test('rendering uses every filtered record exactly once per visible representation and keeps SVG on updates',()=>{
  const c=setup(), source=sample(), before=JSON.stringify(source);c.render(source);
  assert.equal(c.host.querySelectorAll('.overview-threads').flatMap(g=>g.querySelectorAll('.node')).length,4);
  assert.equal(c.host.querySelectorAll('.cluster').reduce((n,g)=>n+Number(g.getAttribute('data-threads')),0),4);
  const svg=c.host.querySelector('svg');c.render(source.map(t=>({...t,title:t.title+' updated'})));
  assert.equal(c.host.querySelector('svg'),svg);assert.equal(JSON.stringify(source),before);assert.equal(c.observers.length,1);
  c.render([source[0]]);assert.equal(c.host.querySelectorAll('.overview-threads').flatMap(g=>g.querySelectorAll('.node')).length,1);
});
test('phase ruler hover and focus highlight only that cluster and temporarily reveal folded finished agents',()=>{
  const c=setup();c.render(sample());
  c.host.querySelectorAll('.overview-hull').find(n=>n.getAttribute('aria-label').startsWith('Web')).emit('keydown',{key:'Enter'});
  const finished=c.host.querySelectorAll('.phase-entry').find(n=>n.getAttribute('data-band')==='finished');assert(finished);
  const old=byAgent(c.host,'old').at(-1);assert(old.classList.contains('folded-finished'));
  const before=c.host.querySelector('svg').getAttribute('viewBox');finished.emit('pointerenter');
  assert(old.classList.contains('phase-match'));assert(byAgent(c.host,'validator').at(-1).classList.contains('phase-muted'));
  assert(byAgent(c.host,'other').every(n=>!n.classList.contains('phase-match')&&!n.classList.contains('phase-muted')));
  finished.emit('pointerleave');assert(!old.classList.contains('phase-match'));assert(old.classList.contains('folded-finished'));
  c.doc.activeElement=finished;finished.emit('focus');finished.emit('pointerleave');assert(old.classList.contains('phase-match'));
  c.doc.activeElement=null;finished.emit('blur');assert(!old.classList.contains('phase-match'));
  assert.equal(c.host.querySelector('svg').getAttribute('viewBox'),before);
});
test('group switching preserves explicit child groups and restores independent layouts/camera',()=>{
  const c=setup();const source=[thread('parent',{project_id:'one',project_name:'Same'}),thread('child',{creation_mode:'orchestration',created_by_thread_id:'parent',project_id:'two',project_name:'Same'}),thread('unassigned')];c.render(source);
  c.host.querySelector('.overview-hull').emit('keydown',{key:'Enter'});const view=c.host.querySelector('svg').getAttribute('viewBox');
  let select=c.host.querySelector('.clustering').querySelector('select');select.value='group';select.onchange();
  assert.equal(c.host.querySelectorAll('.cluster').length,3);assert.equal(c.host.querySelectorAll('.cluster').reduce((n,g)=>n+Number(g.getAttribute('data-threads')),0),3);
  const groupPositions=[...c.data.entries()].find(([key])=>key.includes('atlas:v1:')&&key.includes('group'));assert(groupPositions);
  select=c.host.querySelector('.clustering').querySelector('select');select.value='domain';select.onchange();
  assert.equal(c.host.querySelector('svg').getAttribute('viewBox'),view);assert.equal(c.host.querySelectorAll('.cluster').length,1);
});
test('selected thread previews use real summaries, and Open conversation uses the existing navigation callback',()=>{
  const c=setup();c.render(sample());c.ui.focusThread('validator');byAgent(c.host,'validator').at(-1).emit('keydown',{key:'Enter'});
  const detail=c.host.querySelector('.atlas-inspector');assert(detail);assert(detail.children.some(n=>n.textContent==='Ready for review'));
  detail.querySelectorAll('button').find(n=>n.textContent==='Open conversation').onclick();assert.deepEqual(c.opened,['validator']);
  c.render([]);assert.equal(c.host.querySelectorAll('.cluster').length,0);assert.equal(c.host.querySelector('.canvas-empty').hidden,false);
  c.ui.destroy();assert.equal(c.host.children.length,0);assert.equal(c.observers[0].disconnected,true);
});
