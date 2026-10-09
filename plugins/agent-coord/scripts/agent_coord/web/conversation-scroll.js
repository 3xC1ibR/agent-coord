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
      if (top < this.lastTop || this.distance() < 100) this.following = this.distance() < 100;
      this.lastTop = top;
      this.updateButton();
    };
    timeline.addEventListener("scroll", scrolled, {passive: true});
    this.content.addEventListener("scroll", scrolled, {passive: true});
    button.addEventListener("click", () => {
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
    this.button.hidden = true;
  }
  beforeRender() { return this.initial || this.distance() < 100; }
  afterRender(follow) {
    this.initial = false;
    this.following = follow;
    this.resize.disconnect();
    // Track viewport changes, late-loading images, and expanded tool output.
    for (const el of [this.content, this.timeline, ...this.timeline.children]) this.resize.observe(el);
    this.layout();
  }
  layout() {
    if (this.following) this.scroller().scrollTop = this.scroller().scrollHeight;
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
    this.following = this.distance() < 100;
    this.updateButton();
  }
  updateButton() { this.button.hidden = this.initial || this.distance() <= 240; }
}

if (typeof module !== "undefined" && module.exports) module.exports = {ConversationScroll};
