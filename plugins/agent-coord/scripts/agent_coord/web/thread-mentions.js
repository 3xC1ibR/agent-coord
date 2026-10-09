"use strict";
(() => {
  class ThreadMentionDraft {
    constructor(text = "", refs = []) { this.text = text; this.refs = refs.map(r => ({...r})); this.validate(); }
    validate() { this.refs = this.refs.filter(r => Number.isInteger(r.start) && Number.isInteger(r.end) && r.start >= 0 && r.end > r.start && this.text.slice(r.start, r.end) === r.text); }
    edit(start, end, replacement) {
      const delta = replacement.length - (end - start);
      this.refs = this.refs.filter(r => !(start === end && start === r.end && /^[\p{L}\p{N}_@]/u.test(replacement)) && !(start < r.end && end > r.start) && !(start === end && start > r.start && start < r.end))
        .map(r => r.start >= end ? {...r, start:r.start + delta, end:r.end + delta} : r);
      this.text = this.text.slice(0, start) + replacement + this.text.slice(end); this.validate();
    }
    update(text, hint) {
      if (text === this.text) return;
      if (!hint) { this.text = text; this.refs = []; return; } // Unknown replacements never infer a binding from a name.
      const old = this.text;
      let prefix = 0, suffix = 0;
      const before = Math.min(hint.start, old.length), after = old.length - hint.end;
      if (hint.forward) {
        while (prefix < before && prefix < text.length && old[prefix] === text[prefix]) prefix++;
        while (suffix < after && suffix < text.length-prefix && old[old.length-1-suffix] === text[text.length-1-suffix]) suffix++;
      } else {
        while (suffix < after && suffix < text.length && old[old.length-1-suffix] === text[text.length-1-suffix]) suffix++;
        while (prefix < before && prefix < text.length-suffix && old[prefix] === text[prefix]) prefix++;
      }
      this.edit(prefix, old.length-suffix, text.slice(prefix,text.length-suffix));
    }
    select(start, end, thread) {
      const text = "@" + (thread.title || "Untitled thread").replace(/\s+/g, " ").trim();
      this.edit(start, end, text + " ");
      this.refs.push({start,end:start+text.length,text,session_id:thread.thread_id});
      this.refs.sort((a,b)=>a.start-b.start);
      return start+text.length+1;
    }
    snapshot() { this.validate(); return this.refs.map(r=>({...r})); }
  }
  class ThreadMentions {
    constructor({input, menu, references, getThread, loadThreads, canComplete = () => true, onChange = () => {}}) {
      Object.assign(this,{input,menu,references,getThread,loadThreads,canComplete,onChange});
      this.drafts=new Map(); this.matches=[]; this.index=0;
      input.addEventListener("beforeinput", e=>{this.hint={start:input.selectionStart,end:input.selectionEnd,forward:e.inputType?.includes("Forward")};});
      input.addEventListener("input", e=>{
        this.draft().update(input.value,this.hint);this.hint=null;this.dismissed=null;
        if(e.isComposing)this.close();else this.render();this.onChange();
      });
      input.addEventListener("compositionstart",()=>{this.composing=true;this.close();});
      input.addEventListener("compositionend",()=>{this.composing=false;this.render();});
      input.addEventListener("focus",()=>{this.dismissed=null;this.catalog=null;this.render();});
      input.addEventListener("blur",()=>this.close());
      input.addEventListener("click",()=>this.render());
      input.addEventListener("keyup",()=>this.render());
      menu.addEventListener("pointerdown",e=>e.preventDefault());
      menu.addEventListener("mousedown",e=>e.preventDefault());
    }
    draft(id=this.getThread()) { if(!this.drafts.has(id))this.drafts.set(id,new ThreadMentionDraft());return this.drafts.get(id); }
    snapshot(id=this.getThread(), text = id===this.getThread()?this.input.value:this.draft(id).text) {
      const draft=this.draft(id);draft.update(text);return draft.snapshot();
    }
    restore(id,text,refs=[]) {this.drafts.set(id,new ThreadMentionDraft(text,refs));if(id===this.getThread())this.render();}
    move(from,to) {this.drafts.set(to,this.draft(from));this.drafts.delete(from);}
    sent(id,text,refs) {
      const draft=this.draft(id);
      if(draft.text===text) {draft.text="";draft.refs=[];}
    }
    query() {
      const i=this.input;
      if(this.composing||!this.canComplete()||i.disabled||i.ownerDocument.activeElement!==i||i.selectionStart!==i.selectionEnd)return null;
      const caret=i.selectionStart;
      if(this.draft().refs.some(r=>caret>r.start&&caret<=r.end))return null;
      const match=/(?:^|[\s(\[{])@([^@\n]*)$/.exec(i.value.slice(0,caret));
      if(!match)return null;
      const start=caret-match[1].length-1;
      if(this.draft().refs.some(r=>r.start===start))return null;
      return {start,end:caret,prefix:match[1].toLowerCase()};
    }
    context(t) {return [t.client, t.attention==='archived'?'Closed':t.attention==='later'?'Later':'Now',t.cwd,t.thread_id.slice(-8)].filter(Boolean).join(' · ');}
    close() {
      this.menu.hidden=true;
      if(this.input.getAttribute('aria-activedescendant')?.startsWith(this.menu.id+'-')) {this.input.setAttribute('aria-expanded','false');this.input.removeAttribute('aria-activedescendant');}
    }
    render() {
      const draft=this.draft();draft.update(this.input.value);
      this.references.replaceChildren();
      for(const ref of draft.refs) {
        const chip=this.input.ownerDocument.createElement('button');chip.type='button';chip.className='thread-reference';
        chip.textContent=ref.text+' · '+ref.session_id.slice(-8)+' ×';chip.setAttribute('aria-label','Remove reference '+ref.text+' '+ref.session_id);
        chip.onclick=()=>{draft.edit(ref.start,ref.end,'');this.input.value=draft.text;this.input.focus();this.render();this.onChange();};
        this.references.append(chip);
      }
      const query=this.query(),key=JSON.stringify([this.getThread(),this.input.value,this.input.selectionStart]);
      if(!query||key===this.dismissed){this.close();return;}
      if(!this.catalog) {
        if(!this.loading) {
          this.loading=true;
          Promise.resolve().then(()=>this.loadThreads()).then(threads=>{this.catalog=threads;this.loading=false;this.render();},()=>{this.loading=false;this.close();});
        }
        this.close();return;
      }
      this.matches=this.catalog.filter(t=>(t.title||'Untitled thread').toLowerCase().includes(query.prefix)).slice(0,12);
      if(!this.matches.length){this.close();return;}
      if(this.key!==key)this.index=0;this.key=key;this.index=Math.min(this.index,this.matches.length-1);
      this.menu.replaceChildren();this.menu.hidden=false;
      this.input.setAttribute('aria-controls',this.menu.id);this.input.setAttribute('aria-expanded','true');
      this.input.setAttribute('aria-activedescendant',this.menu.id+'-'+this.index);
      this.matches.forEach((thread,index)=>{
        const option=this.input.ownerDocument.createElement('button');option.type='button';option.id=this.menu.id+'-'+index;
        option.setAttribute('role','option');option.setAttribute('aria-selected',String(index===this.index));option.tabIndex=-1;
        const name=this.input.ownerDocument.createElement('span');name.textContent=thread.title||'Untitled thread';
        const context=this.input.ownerDocument.createElement('small');context.textContent=this.context(thread);option.append(name,context);
        option.onclick=()=>this.choose(thread);this.menu.append(option);
      });
    }
    choose(thread) {
      const query=this.query();if(!query)return;
      const draft=this.draft(),caret=draft.select(query.start,query.end,thread);
      this.input.value=draft.text;this.input.focus();this.input.setSelectionRange(caret,caret);
      this.dismissed=JSON.stringify([this.getThread(),this.input.value,caret]);this.close();this.render();this.onChange();
    }
    keydown(event) {
      if(this.menu.hidden)return false;
      if(event.key==='Escape'){event.preventDefault();this.dismissed=this.key;this.close();return true;}
      if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();this.index=(this.index+(event.key==='ArrowDown'?1:-1)+this.matches.length)%this.matches.length;this.render();return true;}
      if(event.key==='Enter'||event.key==='Tab'){event.preventDefault();this.choose(this.matches[this.index]);return true;}
      return false;
    }
  }
  function displayMessage(text) {
    const marker = "\n\n[Selected thread references; positions are UTF-16 offsets in the user text above]\n";
    const index = text.lastIndexOf(marker);
    if (index < 0) return {text, refs: []};
    const original = text.slice(0, index);
    try {
      const refs = JSON.parse(text.slice(index + marker.length));
      let end = 0;
      if (!Array.isArray(refs) || !refs.length || refs.length > 50 || !refs.every(ref => {
        const valid = ref && Number.isInteger(ref.start) && Number.isInteger(ref.end) && ref.start >= end && ref.end > ref.start &&
          ref.end <= original.length && typeof ref.session_id === "string" && ref.session_id.length > 0 &&
          typeof ref.text === "string" && ref.text.startsWith("@") && original.slice(ref.start, ref.end) === ref.text;
        end = ref?.end; return valid;
      })) return {text, refs: []};
      return {text: original, refs};
    } catch { return {text, refs: []}; }
  }
  ThreadMentions.displayMessage = displayMessage;
  if(typeof module==='object'&&module.exports)module.exports={ThreadMentionDraft,ThreadMentions,displayMessage};
  else Object.assign(globalThis,{ThreadMentionDraft,ThreadMentions});
})();
