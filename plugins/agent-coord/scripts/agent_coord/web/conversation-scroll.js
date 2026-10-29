"use strict";

class ConversationScroll {
  constructor(timeline, button, view = window) {
    this.timeline = timeline;
    this.content = timeline.parentElement;
    this.button = button;
    this.view = view;
    this.reset();
    const scrolled = event => {
      if (event.target !== this.scroller() || this.initial) return;
      const top = this.position();
      // A queued scroll event from our own jump may arrive after the composer
      // changes height. Only scrolling upward should detach a following view.
      if (top < this.lastTop) this.following = false;
      else if (top > this.lastTop && this.distance() < 2 && !this.selecting()) this.following = true;
      this.lastTop = top;
      if (!this.following) this.captureAnchor();
      this.updateButton();
    };
    timeline.addEventListener("scroll", scrolled, {passive: true});
    this.content.addEventListener("scroll", scrolled, {passive: true});
    button.addEventListener("click", () => {
      if (this.selecting()) this.view.getSelection().removeAllRanges();
      this.latest();
      timeline.focus({preventScroll: true});
    });
    this.resize = new view.ResizeObserver(() => this.layout());
  }

  // Prefer the transcript's scroll area; retain support for older page layouts.
  scroller() {
    return this.view.getComputedStyle(this.timeline).overflowY === "visible" ? this.content : this.timeline;
  }
  distance() {
    const el = this.scroller();
    return Math.max(0, el.scrollHeight - el.clientHeight - el.scrollTop);
  }
  position() { return this.scroller().scrollTop; }
  reset() {
    this.initial = true;
    this.following = true;
    this.lastTop = 0;
    this.anchor = null;
    this.button.hidden = true;
  }
  selecting() {
    const selection = this.view.getSelection?.();
    return !!selection && !selection.isCollapsed &&
      (this.timeline.contains(selection.anchorNode) || this.timeline.contains(selection.focusNode));
  }
  hold() {
    this.initial = false; this.following = false; this.captureAnchor();
  }
  captureAnchor() {
    if (!this.timeline.getBoundingClientRect) return;
    const viewport = this.scroller().getBoundingClientRect();
    const visible = node => { const rect = node.getBoundingClientRect(); return rect.bottom > viewport.top && rect.top < viewport.bottom; };
    // Prefer a visible passage: an expanded tool can start far above the view
    // and grow while the reader is looking at the response immediately below it.
    const prose = [...this.timeline.querySelectorAll(".message .text > *")].find(visible);
    const child = prose || [...this.timeline.children].find(visible);
    if (!child) { this.anchor = null; return; }
    const message = child.closest(".message");
    this.anchor = {node: child, offset: child.getBoundingClientRect().top - viewport.top,
      id: child.dataset.id, turn: message?.dataset.searchTurn || child.dataset.searchTurn,
      item: message?.dataset.searchItem || child.dataset.searchItem,
      passage: prose ? [...prose.parentElement.children].indexOf(prose) : null};
  }
  restoreAnchor() {
    const anchor = this.anchor;
    if (!anchor) return;
    let node = anchor.node;
    if (!this.timeline.contains(node)) {
      node = [...this.timeline.children].find(child => anchor.id ? child.dataset.id === anchor.id :
        anchor.turn && child.dataset.searchTurn === anchor.turn && child.dataset.searchItem === anchor.item);
      if (node && anchor.passage != null) node = node.querySelector(".text")?.children[anchor.passage] || node;
    }
    if (!node) return;
    const viewport = this.scroller().getBoundingClientRect();
    this.scroller().scrollTop += node.getBoundingClientRect().top - viewport.top - anchor.offset;
    anchor.node = node;
  }
  beforeRender() {
    if (this.selecting()) this.hold();
    if (!this.following) this.captureAnchor();
    return this.initial || this.following;
  }
  afterRender(follow) {
    this.initial = false;
    this.following = follow;
    this.resize.disconnect();
    // Track viewport changes, late-loading images, and expanded tool output.
    for (const el of [this.content, this.timeline, ...this.timeline.children]) this.resize.observe(el);
    this.layout();
  }
  layout() {
    if (this.selecting() && this.following) this.hold();
    if (this.following) this.scroller().scrollTop = this.scroller().scrollHeight;
    else this.restoreAnchor();
    this.lastTop = this.position();
    this.updateButton();
  }
  latest() {
    this.initial = false;
    this.following = true;
    this.layout();
  }
  restore(top) {
    this.initial = false;
    this.scroller().scrollTop = top;
    this.lastTop = this.position();
    this.following = this.distance() < 2;
    if (!this.following) this.captureAnchor();
    this.updateButton();
  }
  updateButton() { this.button.hidden = this.initial || this.distance() <= 240; }
}

if (typeof module !== "undefined" && module.exports) module.exports = {ConversationScroll};
