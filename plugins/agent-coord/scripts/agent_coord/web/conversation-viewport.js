"use strict";

// Geometry is cached per conversation. A window consists of chronological
// groups, including coordination cards interleaved with native turns.
class ConversationViewport {
  constructor(timeline, scroller, invalidate) {
    this.timeline = timeline;
    this.scroller = scroller;
    this.invalidate = invalidate;
    this.heights = new Map();
    this.onScroll = () => {
      if (this.frame) return;
      this.frame = requestAnimationFrame(() => {
        this.frame = null;
        const range = this.range(this.lastFollow, null);
        if (range && this.signature(range) !== this.lastRange) this.invalidate();
      });
    };
    this.resize = new ResizeObserver(() => this.measure());
    scroller.addEventListener("scroll", this.onScroll, {passive: true});
  }

  resume() {
    this.scroller.addEventListener("scroll", this.onScroll, {passive: true});
    this.observe();
  }
  suspend() {
    this.resize.disconnect();
    cancelAnimationFrame(this.frame); this.frame = null;
    this.scroller.removeEventListener("scroll", this.onScroll);
  }
  dispose() {
    this.resize.disconnect();
    cancelAnimationFrame(this.frame);
    this.scroller.removeEventListener("scroll", this.onScroll);
  }

  signature(range) { return range.join(","); }

  range(follow, target) {
    if (!this.keys?.length) return null;
    const height = Math.max(1, this.scroller.clientHeight), total = this.offsets.at(-1);
    // During a user's scroll use the actual scroll offset. On initial render
    // and append, following readers start in the newest window.
    let top = follow && this.atRender ? total - height : this.scroller.scrollTop;
    const targetIndex = target && this.keys.indexOf(target);
    if (Number.isInteger(targetIndex) && targetIndex >= 0) top = this.offsets[targetIndex];
    let start = 0, end = this.keys.length;
    const lower = Math.max(0, top - height), upper = Math.max(height * 2, top + height * 2);
    while (start + 1 < end && this.offsets[start + 1] < lower) start++;
    while (end > start + 1 && this.offsets[end - 1] > upper) end--;
    return [start, end];
  }

  window(entries, follow, target) {
    const groups = [];
    for (const entry of entries) {
      // Interleaved coordination records belong to the enclosing turn's window.
      let key = entry.turn?.id || entry.message?.turn_id || groups.at(-1)?.key || "message-" + entry.message?.id;
      if (groups.at(-1)?.key !== key) groups.push({key, entries: []});
      groups.at(-1).entries.push(entry);
    }
    this.keys = groups.map(group => group.key);
    this.offsets = [0];
    for (const key of this.keys) this.offsets.push(this.offsets.at(-1) + (this.heights.get(key) ?? 180));
    this.atRender = true;
    const [start, end] = this.range(follow, target || this.anchorKey) || [0, 0];
    this.anchorKey = null;
    this.atRender = false;
    this.lastFollow = follow;
    this.lastRange = this.signature([start, end]);
    const pinned = new Set();
    const selection = this.timeline.ownerDocument.getSelection?.();
    for (const node of [this.timeline.ownerDocument.activeElement, selection?.anchorNode, selection?.focusNode]) {
      const row = (node?.closest ? node : node?.parentElement)?.closest("[data-viewport-key]");
      if (row && this.timeline.contains(row)) pinned.add(row.dataset.viewportKey);
    }
    const result = [];
    let skipped = 0;
    const spacer = () => { if (skipped > 0) result.push({spacer: skipped}); skipped = 0; };
    groups.forEach(({key, entries}, index) => {
      if ((index < start || index >= end) && !pinned.has(key)) {
        skipped += this.offsets[index + 1] - this.offsets[index];
      } else {
        spacer();
        for (const entry of entries) result.push({...entry, viewportKey: key});
      }
    });
    spacer();
    return result;
  }

  observe() {
    this.resize.disconnect();
    this.resize.observe(this.scroller);
    for (const row of this.timeline.children) if (row.dataset.viewportKey) this.resize.observe(row);
    this.measure();
  }

  measure() {
    const heights = new Map();
    for (const row of this.timeline.children) {
      const key = row.dataset.viewportKey;
      if (!key) continue;
      const rect = row.getBoundingClientRect(), next = row.nextElementSibling;
      const height = next ? next.getBoundingClientRect().top - rect.top : rect.height;
      if (height > 0) heights.set(key, (heights.get(key) || 0) + height);
    }
    for (const [key, height] of heights) this.heights.set(key, height);
  }
}
