/* Deterministic motion engine for the Ribbon Field demo.
 *
 * Everything is a pure function of time: window.__render(t) lays out the stage
 * for second `t`, so a headless browser can step frames at any rate. No CSS
 * transitions or animations are used anywhere.
 */
"use strict";
window.Engine = (() => {
  const W = 1920, H = 1080;
  const stage = document.getElementById("stage");

  // ---------------------------------------------------------------- math
  const clamp01 = x => x < 0 ? 0 : x > 1 ? 1 : x;
  const lerp = (a, b, x) => a + (b - a) * x;
  const ease = {
    linear: x => x,
    inQuad: x => x * x,
    outQuad: x => 1 - (1 - x) * (1 - x),
    inOutCubic: x => x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2,
    outCubic: x => 1 - Math.pow(1 - x, 3),
    outQuart: x => 1 - Math.pow(1 - x, 4),
    outExpo: x => x >= 1 ? 1 : 1 - Math.pow(2, -10 * x),
    inExpo: x => x <= 0 ? 0 : Math.pow(2, 10 * x - 10),
    inOutExpo: x => x <= 0 ? 0 : x >= 1 ? 1 : x < 0.5 ? Math.pow(2, 20 * x - 10) / 2 : (2 - Math.pow(2, -20 * x + 10)) / 2,
    outBack: x => { const c = 1.70158; return 1 + (c + 1) * Math.pow(x - 1, 3) + c * Math.pow(x - 1, 2); },
    outSoftBack: x => { const c = 0.9; return 1 + (c + 1) * Math.pow(x - 1, 3) + c * Math.pow(x - 1, 2); },
    inOutSine: x => -(Math.cos(Math.PI * x) - 1) / 2,
  };
  // Progress 0..1 of a segment starting at t0 lasting d.
  const seg = (t, t0, d, fn = ease.outCubic) => fn(clamp01((t - t0) / d));
  // Piecewise keyframes: [[time, value], ...] with one easing between keys.
  function kf(t, keys, fn = ease.inOutCubic) {
    if (t <= keys[0][0]) return keys[0][1];
    for (let i = 1; i < keys.length; i++) {
      if (t <= keys[i][0]) {
        const [t0, v0] = keys[i - 1], [t1, v1] = keys[i];
        return lerp(v0, v1, fn((t - t0) / (t1 - t0)));
      }
    }
    return keys[keys.length - 1][1];
  }
  // Fade in over d then out over d at end of a window.
  const window01 = (t, t0, t1, din = 0.5, dout = 0.5) => Math.min(seg(t, t0, din, ease.outCubic), 1 - seg(t, t1 - dout, dout, ease.inQuad));
  let seed = 7;
  const rand = () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; };

  // ---------------------------------------------------------------- dom
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }
  function setT(node, {x = 0, y = 0, s = 1, r = 0, o = 1, blur = 0, sx, sy} = {}) {
    node.style.transform = `translate(${x}px, ${y}px) scale(${sx ?? s}, ${sy ?? s}) rotate(${r}deg)`;
    node.style.opacity = String(clamp01(o));
    node.style.filter = blur > 0.05 ? `blur(${blur}px)` : "";
    node.style.display = o <= 0.001 ? "none" : "";
  }

  // ---------------------------------------------------------------- background
  class Background {
    constructor(root, theme = "dark") {
      this.node = el("div", "bg " + theme);
      this.ribbons = [];
      const colors = theme === "dark"
        ? ["#1f3a30", "#2f624b", "#4d8a6a", "#d4a85a", "#213a32"]
        : ["#dfe8d5", "#c9dcc3", "#eaf0e5", "#f1dfb9", "#d9e4d3"];
      for (let i = 0; i < 5; i++) {
        const rib = el("div", "ribbon");
        rib.style.setProperty("--c", colors[i]);
        rib.style.width = (900 + i * 220) + "px";
        rib.style.height = (260 + i * 60) + "px";
        this.node.append(rib);
        this.ribbons.push({node: rib, phase: i * 1.7, speed: 0.05 + i * 0.012, x: 200 + i * 300, y: 150 + i * 190});
      }
      const grain = el("div", "grain");
      this.node.append(grain);
      root.append(this.node);
    }
    update(t) {
      for (const r of this.ribbons) {
        const x = r.x + Math.sin(t * r.speed + r.phase) * 260 - 500;
        const y = r.y + Math.cos(t * r.speed * 0.8 + r.phase) * 140 - 200;
        r.node.style.transform = `translate(${x}px, ${y}px) rotate(${Math.sin(t * 0.03 + r.phase) * 18 - 12}deg)`;
      }
    }
  }

  // ---------------------------------------------------------------- product frame
  class Frame {
    /* A macOS-style window showing one captured screenshot with a camera. */
    constructor(root, {src, pageW = 1600, pageH = 1000, fit = 0.9, title = "Ribbon Field", kind = "mac"}) {
      this.pageW = pageW; this.pageH = pageH; this.fit = fit; this.kind = kind;
      this.node = el("div", "frame " + kind);
      this.viewW = pageW * fit; this.viewH = pageH * fit;
      if (kind === "mac") {
        const bar = el("div", "titlebar");
        bar.append(el("span", "light red"), el("span", "light yellow"), el("span", "light green"), el("span", "title", title));
        this.node.append(bar);
      }
      this.view = el("div", "view");
      this.view.style.width = this.viewW + "px";
      this.view.style.height = this.viewH + "px";
      this.img = document.createElement("img");
      this.img.src = src;
      this.img.width = pageW; this.img.height = pageH;
      this.img.draggable = false;
      this.view.append(this.img);
      this.node.append(this.view);
      this.overlay = el("div", "frame-overlay");
      this.view.append(this.overlay);
      root.append(this.node);
      this.cam = {s: 1, cx: pageW / 2, cy: pageH / 2};
      this.pos = {x: (W - this.viewW) / 2, y: (H - this.viewH - (kind === "mac" ? 38 : 0)) / 2, s: 1, o: 1, r: 0};
      this.place(this.pos);
      this.camera(this.cam);
    }
    swap(src) { if (this.img.getAttribute("src") !== src) this.img.src = src; }
    place(p) { Object.assign(this.pos, p); setT(this.node, this.pos); }
    camera({s, cx, cy}) {
      Object.assign(this.cam, {s, cx, cy});
      // Image scaled by fit*s with (cx,cy) at the viewport centre.
      const k = this.fit * s;
      const tx = this.viewW / 2 - cx * k, ty = this.viewH / 2 - cy * k;
      this.img.style.transform = `translate(${tx}px, ${ty}px) scale(${k})`;
      this.overlay.style.transform = this.img.style.transform;
    }
    // Camera that frames a page box (CSS px) with padding.
    focusOn(box, pad = 40, maxScale = 2.6) {
      const s = Math.min(maxScale, this.viewW / ((box.width + pad * 2) * this.fit), this.viewH / ((box.height + pad * 2) * this.fit));
      return {s: Math.max(1, s), cx: box.x + box.width / 2, cy: box.y + box.height / 2};
    }
    camLerp(a, b, x) {
      return {s: Math.exp(lerp(Math.log(a.s), Math.log(b.s), x)), cx: lerp(a.cx, b.cx, x), cy: lerp(a.cy, b.cy, x)};
    }
    // Page CSS px -> stage coordinates (for cursor/callouts). The frame scales
    // about its top-left corner, so stage = pos + view * pos.s.
    project(px, py) {
      const k = this.fit * this.cam.s;
      const vx = this.viewW / 2 + (px - this.cam.cx) * k;
      const vy = this.viewH / 2 + (py - this.cam.cy) * k;
      const barH = this.kind === "mac" ? 38 : 0;
      return {x: this.pos.x + vx * this.pos.s, y: this.pos.y + (vy + barH) * this.pos.s, k: k * this.pos.s};
    }
    projectBox(box) {
      const a = this.project(box.x, box.y), b = this.project(box.x + box.width, box.y + box.height);
      return {x: a.x, y: a.y, width: b.x - a.x, height: b.y - a.y};
    }
  }

  // ---------------------------------------------------------------- phone frame
  class Phone {
    constructor(root, {src, pageW = 390, pageH = 844, scale = 1}) {
      this.node = el("div", "phone");
      this.img = document.createElement("img");
      this.img.src = src; this.img.width = pageW; this.img.height = pageH;
      const screen = el("div", "screen");
      screen.append(this.img, el("div", "island"));
      this.node.append(screen);
      root.append(this.node);
      this.pos = {x: 0, y: 0, s: scale, o: 1, r: 0};
    }
    swap(src) { if (this.img.getAttribute("src") !== src) this.img.src = src; }
    place(p) { Object.assign(this.pos, p); setT(this.node, this.pos); }
    scroll(y) { this.img.style.transform = `translateY(${-y}px)`; }
  }

  // ---------------------------------------------------------------- cursor
  class Cursor {
    constructor(root) {
      this.node = el("div", "cursor");
      this.node.innerHTML = `<svg viewBox="0 0 28 28" width="34" height="34"><path d="M6 3 L6 23 L11 18.5 L14.5 26 L18 24.5 L14.5 17 L21 17 Z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
      this.ring = el("div", "ring");
      root.append(this.ring, this.node);
      this.x = -100; this.y = -100;
      this.clickAt = null;
    }
    set(x, y, o = 1) { this.x = x; this.y = y; setT(this.node, {x, y, o}); }
    // Click ripple centred on the hot spot at time since click.
    ripple(dt, x = this.x, y = this.y) {
      if (dt < 0 || dt > 0.6) { setT(this.ring, {o: 0}); return; }
      const p = dt / 0.6;
      setT(this.ring, {x: x - 22, y: y - 22, s: 0.3 + p * 1.6, o: (1 - p) * 0.9});
    }
  }

  // ---------------------------------------------------------------- callout / spotlight / text
  class Callout {
    constructor(root, text, cls = "") {
      this.node = el("div", "callout " + cls);
      this.dot = el("span", "dot");
      this.label = el("span", "label", text);
      this.line = el("span", "line");
      this.node.append(this.dot, this.line, this.label);
      root.append(this.node);
    }
    // Anchor at stage point, label offset by (dx,dy); progress 0..1 for reveal.
    show(x, y, dx, dy, p) {
      const o = clamp01(p);
      setT(this.node, {x, y, o});
      const len = Math.hypot(dx, dy), ang = Math.atan2(dy, dx) * 180 / Math.PI;
      this.line.style.width = (len * ease.outCubic(o)) + "px";
      this.line.style.transform = `rotate(${ang}deg)`;
      this.label.style.transform = `translate(${dx}px, ${dy}px) translate(${dx >= 0 ? 0 : -100}%, -50%) scale(${0.85 + 0.15 * ease.outBack(o)})`;
      this.label.style.opacity = String(ease.outCubic(clamp01((o - 0.35) / 0.65)));
    }
    hide() { setT(this.node, {o: 0}); }
  }

  class Spotlight {
    constructor(root) {
      this.node = el("div", "spotlight");
      this.parts = ["top", "bottom", "left", "right"].map(() => { const d = el("div", "dim"); this.node.append(d); return d; });
      this.ring = el("div", "ring-box");
      this.node.append(this.ring);
      root.append(this.node);
    }
    show(box, o = 1, radius = 10, pad = 8) {
      const a = clamp01(o);
      this.node.style.display = a <= 0.001 ? "none" : "";
      const x = box.x - pad, y = box.y - pad, w = box.width + pad * 2, h = box.height + pad * 2;
      const dim = `rgba(6, 14, 11, ${0.62 * a})`;
      const [top, bottom, left, right] = this.parts;
      for (const part of this.parts) part.style.background = dim;
      top.style.cssText += `;left:0;top:0;width:${W}px;height:${Math.max(0, y)}px`;
      bottom.style.cssText += `;left:0;top:${y + h}px;width:${W}px;height:${Math.max(0, H - y - h)}px`;
      left.style.cssText += `;left:0;top:${y}px;width:${Math.max(0, x)}px;height:${h}px`;
      right.style.cssText += `;left:${x + w}px;top:${y}px;width:${Math.max(0, W - x - w)}px;height:${h}px`;
      this.ring.style.cssText = `left:${x}px;top:${y}px;width:${w}px;height:${h}px;border-radius:${radius}px;opacity:${a}`;
    }
    hide() { this.node.style.display = "none"; }
  }

  class Text {
    /* Kinetic headline: words rise, un-blur, and settle. */
    constructor(root, lines, {cls = "", size = 96, align = "center", x = W / 2, y = H / 2, width = 1500, weight = 650} = {}) {
      this.node = el("div", "kinetic " + cls);
      this.node.style.fontSize = size + "px";
      this.node.style.textAlign = align;
      this.node.style.width = width + "px";
      this.node.style.fontWeight = String(weight);
      this.node.style.left = (align === "center" ? x - width / 2 : x) + "px";
      this.node.style.top = y + "px";
      this.words = [];
      lines.forEach((line, li) => {
        const row = el("div", "line");
        const parts = typeof line === "string" ? [{text: line}] : line;
        for (const part of parts) {
          for (const word of part.text.split(" ")) {
            const span = el("span", "word " + (part.cls || ""), word);
            row.append(span, document.createTextNode(" "));
            this.words.push(span);
          }
        }
        this.node.append(row);
      });
      root.append(this.node);
    }
    // Reveal between t0 and t0+d with stagger; hide between t1 and t1+dOut.
    update(t, t0, t1 = Infinity, {d = 0.6, stagger = 0.07, dOut = 0.4, rise = 44} = {}) {
      let any = false;
      this.words.forEach((w, i) => {
        const pin = seg(t, t0 + i * stagger, d, ease.outQuart);
        const pout = seg(t, t1 + i * stagger * 0.4, dOut, ease.inQuad);
        const o = pin * (1 - pout);
        any = any || o > 0.001;
        w.style.opacity = String(o);
        w.style.transform = `translateY(${(1 - pin) * rise - pout * 24}px)`;
        w.style.filter = pin < 0.999 ? `blur(${(1 - pin) * 12}px)` : pout > 0 ? `blur(${pout * 8}px)` : "";
      });
      this.node.style.display = any ? "" : "none";
    }
  }

  class Terminal {
    /* Typed command lines with instant result lines. script: [{type:'cmd'|'out', text, at}] */
    constructor(root, {x, y, width = 1120, height = 560, title = "zsh — agent-coord"}) {
      this.node = el("div", "terminal");
      this.node.style.left = x + "px"; this.node.style.top = y + "px";
      this.node.style.width = width + "px"; this.node.style.height = height + "px";
      const bar = el("div", "titlebar");
      bar.append(el("span", "light red"), el("span", "light yellow"), el("span", "light green"), el("span", "title", title));
      this.body = el("pre", "body");
      this.node.append(bar, this.body);
      root.append(this.node);
      this.lines = [];
    }
    setScript(script) { this.script = script; }
    update(t, cps = 46) {
      let html = "";
      let cursorDrawn = false;
      for (const line of this.script) {
        if (t < line.at) break;
        if (line.type === "cmd") {
          const n = Math.min(line.text.length, Math.floor((t - line.at) * cps));
          const typed = line.text.slice(0, n);
          const done = n >= line.text.length;
          html += `<span class="prompt">$</span> <span class="cmd">${escape(typed)}</span>${done ? "" : '<span class="caret"></span>'}\n`;
          if (!done) { cursorDrawn = true; break; }
        } else {
          html += `<span class="${line.cls || "out"}">${escape(line.text)}</span>\n`;
        }
      }
      if (!cursorDrawn) html += `<span class="prompt">$</span> <span class="caret"></span>`;
      this.body.innerHTML = html;
    }
  }
  const escape = s => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  // ---------------------------------------------------------------- timeline
  const scenes = [];
  const cues = [];
  function addScene(scene) {
    scene.root = el("div", "scene " + (scene.cls || ""));
    scene.root.style.display = "none";
    stage.append(scene.root);
    scene.mount(scene.root);
    scenes.push(scene);
    return scene;
  }
  function cue(t, kind, extra = {}) { cues.push({t, kind, ...extra}); }
  function render(t) {
    for (const scene of scenes) {
      const lead = scene.lead || 0, tail = scene.tail || 0;
      const on = t >= scene.start - lead && t < scene.start + scene.duration + tail;
      scene.root.style.display = on ? "" : "none";
      if (on) scene.update(t - scene.start, scene.root);
    }
  }
  function duration() { return scenes.reduce((m, s) => Math.max(m, s.start + s.duration), 0); }
  function ready() {
    const images = Array.from(document.images).map(img => img.decode().catch(() => {}));
    return Promise.all([document.fonts.ready, ...images]);
  }

  return {W, H, ease, seg, kf, lerp, clamp01, window01, rand, el, setT, Background, Frame, Phone, Cursor, Callout, Spotlight, Text, Terminal,
          addScene, cue, render, duration, ready, cues: () => cues};
})();
