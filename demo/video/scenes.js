/* Scene script for the Ribbon Field demo. Times are in seconds; scene starts
 * sit on beats of the 104 BPM soundtrack so cuts land with the music. */
"use strict";
(() => {
  const E = Engine, {W, H, ease, seg, kf, lerp, clamp01, el, setT} = E;
  const BEAT = 60 / 104, B = n => n * BEAT;
  const SHOT = n => `../out/shots/${n}.png`;
  const BOXES = window.BOXES || {};
  const box = (shot, key, fallback = {x: 0, y: 0, width: 1600, height: 1000}) => (BOXES[shot] && BOXES[shot][key]) || fallback;
  const mid = b => ({x: b.x + b.width / 2, y: b.y + b.height / 2});
  const visible = (t, t0, t1, din = 0.45, dout = 0.35) => Math.min(seg(t, t0, din, ease.outCubic), 1 - seg(t, t1 - dout, dout, ease.inQuad));

  // Interpolate camera keyframes [[t, cam], ...] through a frame.
  function camKF(frame, t, keys, fn = ease.inOutCubic) {
    if (t <= keys[0][0]) return keys[0][1];
    for (let i = 1; i < keys.length; i++) {
      if (t <= keys[i][0]) {
        const [t0, a] = keys[i - 1], [t1, b] = keys[i];
        return frame.camLerp(a, b, fn((t - t0) / (t1 - t0)));
      }
    }
    return keys[keys.length - 1][1];
  }
  // Cursor path keyframes [[t, x, y], ...]
  function pathKF(t, keys, fn = ease.inOutCubic) {
    if (t <= keys[0][0]) return {x: keys[0][1], y: keys[0][2]};
    for (let i = 1; i < keys.length; i++) {
      if (t <= keys[i][0]) {
        const [t0, x0, y0] = keys[i - 1], [t1, x1, y1] = keys[i];
        const p = fn((t - t0) / (t1 - t0));
        return {x: lerp(x0, x1, p), y: lerp(y0, y1, p)};
      }
    }
    const last = keys[keys.length - 1];
    return {x: last[1], y: last[2]};
  }
  function enter(frame, t, t0, d, base, {rise = 70, from = 1} = {}) {
    const p = seg(t, t0, d, ease.outQuart);
    frame.place({...base, y: base.y + (1 - p) * rise, o: p * from, blur: (1 - p) * 14});
  }

  class LowerThird {
    constructor(root, text, sub) {
      this.node = el("div", "lower-third");
      this.node.append(el("div", "lt-text", text));
      if (sub) this.node.append(el("div", "lt-sub", sub));
      root.append(this.node);
    }
    update(t, t0, t1, y = 968) {
      const p = visible(t, t0, t1, 0.5, 0.35);
      this.node.style.display = p <= 0.001 ? "none" : "";
      this.node.style.opacity = String(p);
      this.node.style.transform = `translate(-50%, ${y + (1 - ease.outCubic(p)) * 26}px) scale(${0.96 + 0.04 * ease.outCubic(p)})`;
    }
  }
  class Highlight {
    constructor(frame, cls = "") { this.node = el("div", "hl " + cls); frame.overlay.append(this.node); }
    show(b, o = 1, pad = 6, radius = 8) {
      this.node.style.display = o <= 0.001 ? "none" : "";
      this.node.style.opacity = String(o);
      this.node.style.left = (b.x - pad) + "px"; this.node.style.top = (b.y - pad) + "px";
      this.node.style.width = (b.width + pad * 2) + "px"; this.node.style.height = (b.height + pad * 2) + "px";
      this.node.style.borderRadius = radius + "px";
    }
  }
  class Chip {
    constructor(root, text, key, sub) {
      this.node = el("div", "chip");
      if (key) this.node.append(el("span", "k", key));
      this.node.append(document.createTextNode(text));
      if (sub) this.node.append(el("small", null, sub));
      root.append(this.node);
    }
    update(t, t0, t1, x, y) {
      const p = seg(t, t0, 0.55, ease.outBack), q = 1 - seg(t, t1, 0.3, ease.inQuad);
      const o = Math.min(seg(t, t0, 0.3), q);
      this.node.style.display = o <= 0.001 ? "none" : "";
      this.node.style.left = x + "px"; this.node.style.top = y + "px";
      this.node.style.opacity = String(o);
      this.node.style.transform = `translateY(${(1 - p) * 30}px) scale(${0.8 + 0.2 * p})`;
    }
  }

  // ====================================================================== S0 cold open
  const TASKS = ["deploy-payments", "cart-totals", "search-p95", "backfill-orders", "promo-banner", "address-autocomplete",
                 "ci-matrix", "inventory-plan", "flaky-e2e", "rate-limiter", "push-tokens", "release-notes"];
  const LOGS = [
    ["› helm upgrade payments --set image.tag=v2.14", "› canary 10% healthy for 20m", "› promoting to 50%…", "<r>✗ AWS SSO session expired</r>", "<r>⚠ waiting for you: aws sso login</r>"],
    ["› pytest -k order_7731", "<r>✗ 118.06 != 118.07</r>", "› editing src/checkout/cart_totals.py", "<g>✓ 48 passed</g>", "› gh pr create → #482", "<w>? please review the rounding helper</w>"],
    ["› kubectl logs deploy/search --since=24h", "› EXPLAIN ANALYZE … Seq Scan on products", "› 1.9M rows, no trigram index", "<w>findings ready (2 options)</w>"],
    ["⠋ backfill 38.1M / 61.4M events", "› 11k events/s, 0 rejected", "› partition 2026-08-14 done", "⠙ 62%… ETA 40 min"],
    ["› editing PromoBanner.tsx", "› npm test → 14 passed", "› deploy preview-479", "<g>✓ done, nothing else needed</g>"],
    ["› Places client written", "› useSuggestions hook (debounced)", "› AddressForm keyboard nav", "⠹ npm test src/address…"],
    ["› converting .circleci/config.yml", "› matrix: 3.11, 3.12, 3.13", "› caching uv env", "⠸ running workflow…"],
    ["› reading inventory_sync.py", "› drafting outbox schema", "› plan: 3 phases, ~2 weeks", "<w>plan ready for review</w>"],
    ["› checkout.spec.ts fails 1 in 5", "› correlates with banner animation", "› waiting on animationend fixes 20/20", "› confirming on CI…"],
    ["› token bucket, 600 req/min", "› load test 2x → correct 429s", "⠼ soak test 30 min…"],
    ["› BGTask budget expires → task cancelled", "› rescheduling with earliestBeginDate", "⠴ building for device…"],
    ["› 23 merged PRs since 3.1", "› grouping by area", "› CHANGELOG.md updated", "<g>✓ done</g>"],
  ];
  const coldOpen = {
    start: B(0), duration: B(13), cls: "cold",
    mount(root) {
      this.bg = new E.Background(root, "dark");
      this.panes = [];
      E.rand(); // advance
      const cols = 4, rows = 3;
      for (let i = 0; i < 12; i++) {
        const node = el("div", "pane");
        root.append(node);
        const c = i % cols, r = Math.floor(i / cols);
        const w = 400 + E.rand() * 90, h = 220 + E.rand() * 50;
        const x = 70 + c * 460 + (E.rand() - 0.5) * 90, y = 40 + r * 330 + (E.rand() - 0.5) * 70;
        this.panes.push({node, w, h, x, y, rot: (E.rand() - 0.5) * 7, speed: 0.6 + E.rand() * 0.6, offset: E.rand() * 2, lines: LOGS[i], title: TASKS[i], z: Math.floor(E.rand() * 5)});
        node.style.width = w + "px"; node.style.height = h + "px";
      }
      this.vignette = el("div", "vignette");
      root.append(this.vignette);
      this.t1 = new E.Text(root, ["Twelve agents."], {size: 86, y: 330});
      this.t2 = new E.Text(root, ["Twelve panes."], {size: 86, y: 440});
      this.t3 = new E.Text(root, [[{text: "Which one needs ", cls: ""}, {text: "you?", cls: "gold"}]], {size: 118, y: 580});
    },
    update(t) {
      this.bg.update(t);
      const implode = seg(t, B(11), B(2), ease.inExpo);
      this.panes.forEach((p, i) => {
        const appear = seg(t, 0.05 + i * 0.09, 0.5, ease.outBack);
        const shown = Math.min(p.lines.length, Math.floor(Math.max(0, t - p.offset) * p.speed) + 1);
        let html = `<span class="t">${p.title}</span>\n`;
        for (let k = 0; k < shown; k++) html += p.lines[k].replace(/<(\/?)([grw])>/g, '<$1span class="$2">').replace(/<\/span class="[grw]">/g, "</span>") + "\n";
        if (t > p.offset + 1 && shown < p.lines.length) html += `<span class="w">▋</span>`;
        p.node.innerHTML = html;
        const cx = W / 2 - p.w / 2, cy = H / 2 - p.h / 2;
        const x = lerp(p.x, cx, implode), y = lerp(p.y, cy, implode);
        const jitter = Math.sin(t * 2.1 + i) * 2;
        const s = appear * (1 - implode) * (1 + 0.02 * Math.sin(t + i));
        setT(p.node, {x, y: y + jitter, s: Math.max(0.001, s), r: p.rot * (1 - implode) + implode * 40 * (i % 2 ? 1 : -1), o: appear * (1 - seg(t, B(12), B(1), ease.inQuad))});
        p.node.style.zIndex = String(10 + p.z);
      });
      this.vignette.style.opacity = String(0.2 + 0.55 * seg(t, 2.8, 1.2));
      this.t1.update(t, 0.7, B(11));
      this.t2.update(t, 1.9, B(11));
      this.t3.update(t, 3.3, B(11.3), {d: 0.7, stagger: 0.09});
    },
  };
  E.addScene(coldOpen);
  E.cue(B(11), "riser", {length: 1.15});
  E.cue(B(12.3), "whoosh", {length: 0.7});

  // ====================================================================== S1 logo
  const logoScene = {
    start: B(13), duration: B(9),
    mount(root) {
      this.bg = new E.Background(root, "dark");
      this.flash = el("div", "flash"); root.append(this.flash);
      this.logo = el("div", "logo");
      this.logo.append(el("div", "rf", "rf"), el("div", "dot"));
      root.append(this.logo);
      this.word = el("div", "wordmark", "Ribbon Field"); root.append(this.word);
      this.tag = el("div", "tagline", "A place for work in progress."); root.append(this.tag);
      this.sub = new E.Text(root, [[{text: "Every coding agent. ", cls: ""}, {text: "One workspace.", cls: "green"}]], {size: 54, y: 820, weight: 600});
    },
    update(t) {
      this.bg.update(t + 20);
      this.flash.style.opacity = String(0.9 * (1 - seg(t, 0, 0.5, ease.outCubic)));
      const inL = seg(t, 0, 0.9, ease.outBack);
      const slide = seg(t, 0.9, 0.9, ease.outQuart);
      const logoX = lerp(W / 2 - 120, 520, slide), logoY = H / 2 - 120 - 40 * slide;
      const out = seg(t, B(8), B(1), ease.inQuad);
      setT(this.logo, {x: logoX, y: logoY, s: Math.max(0.001, 0.6 + 0.4 * inL) * (1 - 0.1 * out), r: (1 - inL) * -12, o: inL * (1 - out)});
      const dotP = seg(t, 0.55, 0.45, ease.outBack);
      this.logo.querySelector(".dot").style.transform = `scale(${dotP})`;
      // Wordmark slides out from behind the tile.
      const wmX = logoX + 240 + 48, wmY = logoY + 50;
      const clip = 1 - slide;
      this.word.style.left = wmX + "px"; this.word.style.top = wmY + "px";
      this.word.style.clipPath = `inset(0 ${clip * 100}% 0 0)`;
      setT(this.word, {x: -40 * (1 - slide), y: 0, o: slide * (1 - out)});
      this.tag.style.left = (wmX + 6) + "px"; this.tag.style.top = (wmY + 138) + "px";
      const tagP = seg(t, 1.6, 0.7, ease.outCubic);
      setT(this.tag, {x: 0, y: (1 - tagP) * 18, o: tagP * (1 - out)});
      this.sub.update(t, 2.9, B(8), {d: 0.6, stagger: 0.06});
    },
  };
  E.addScene(logoScene);
  E.cue(B(13), "impact", {gain: 0.85});
  E.cue(B(13) + 1.0, "pop", {pitch: 1320});
  E.cue(B(13) + 0.55, "pop", {pitch: 990});

  // ====================================================================== S2 overview hero
  const HERO = {x: (W - 1440) / 2, y: (H - 938) / 2, s: 1};
  const overviewScene = {
    start: B(22), duration: B(16),
    mount(root) {
      this.bg = new E.Background(root, "dark");
      this.frameA = new E.Frame(root, {src: SHOT("overview-light"), title: "Ribbon Field — All work"});
      this.frameB = new E.Frame(root, {src: SHOT("overview-stages"), title: "Ribbon Field — All work"});
      this.lt = new LowerThird(root, "Every agent conversation. Every repo. One place.", "Codex and Claude Code sessions, from the browser or the terminal.");
      this.c1 = new E.Callout(root, "Attention · what needs you", "gold");
      this.c2 = new E.Callout(root, "Stages · what the agents are doing", "");
      this.c3 = new E.Callout(root, "Green border · an agent is working right now", "green");
      this.hlB = new Highlight(this.frameB);
    },
    update(t) {
      this.bg.update(t + 40);
      const base = {...HERO};
      enter(this.frameA, t, 0, 1.3, base);
      this.frameB.place({...base, o: 1});
      const attention = box("overview-light", "attentionGroup", {x: 292, y: 218, width: 1270, height: 540});
      const camA = camKF(this.frameA, t, [[0, {s: 1.22, cx: 900, cy: 420}], [3.4, {s: 1, cx: 800, cy: 500}]], ease.outCubic);
      this.frameA.camera(camA);
      const swap = seg(t, 6.3, 0.9, ease.inOutCubic);
      const working = box("overview-stages", "working", {x: 842, y: 480, width: 170, height: 300});
      const stagesFocus = this.frameB.focusOn({x: 292, y: 215, width: 1270, height: 640}, 24, 1.35);
      const camB = camKF(this.frameB, t, [[6.3, {s: 1, cx: 800, cy: 500}], [8.2, stagesFocus]], ease.inOutCubic);
      this.frameB.camera(camB);
      this.frameB.node.style.opacity = String(swap);
      this.frameA.node.style.opacity = String(Math.min(this.frameA.pos.o, 1 - swap * 0.999 + 0.001));
      this.frameA.node.style.display = swap >= 0.999 ? "none" : "";
      this.lt.update(t, 1.4, 5.9);
      const head = this.frameA.project(attention.x + 60, attention.y + 6);
      this.c1.show(head.x, head.y, 30, -70, visible(t, 3.6, 6.2));
      const stage = this.frameA.project(box("overview-light", "stageImplementing", {x: 842, y: 788, width: 74, height: 13}).x, box("overview-light", "stageImplementing").y + 6);
      this.c2.show(stage.x, stage.y, 40, -60, visible(t, 4.8, 6.2));
      const wp = this.frameB.project(working.x + working.width, working.y + 60);
      this.c3.show(wp.x, wp.y, 50, -40, visible(t, 8.5, B(16) - 0.2));
      this.hlB.show(working, visible(t, 8.4, B(16) - 0.2), 4, 9);
    },
  };
  E.addScene(overviewScene);
  E.cue(B(22), "whoosh", {length: 0.6});
  E.cue(B(22) + 3.6, "pop", {pitch: 1100});
  E.cue(B(22) + 4.8, "pop", {pitch: 880});
  E.cue(B(22) + 8.5, "pop", {pitch: 1200});

  // ====================================================================== S3 attention
  const SIDE = {x: 640, y: 118, s: 0.84};
  const attentionScene = {
    start: B(38), duration: B(21),
    mount(root) {
      this.bg = new E.Background(root, "dark");
      this.frame = new E.Frame(root, {src: SHOT("overview-light"), title: "Ribbon Field — All work"});
      this.spot = new E.Spotlight(root);
      this.hl = new Highlight(this.frame);
      this.eyebrow = new E.Text(root, ["Attention"], {cls: "eyebrow", size: 20, align: "left", x: 96, y: 150, width: 520});
      this.head = new E.Text(root, ["Know what", [{text: "needs ", cls: ""}, {text: "you.", cls: "gold"}]], {size: 88, align: "left", x: 92, y: 190, width: 540});
      this.sub = new E.Text(root, ["Replies are classified from what they actually say, then ranked."], {size: 26, align: "left", x: 96, y: 420, width: 500, weight: 500, cls: "subtle"});
      this.items = ["Blocked", "Review requested", "Update", "Findings ready", "✓ Done · no action needed"].map((label, i) => {
        const row = el("div", "bullet"); row.append(el("span", "mark"), el("span", null, label));
        row.style.left = "96px"; row.style.top = (560 + i * 78) + "px";
        root.append(row); return row;
      });
      this.keys = ["blocked", "review", "update", "findings", "done"];
    },
    update(t) {
      this.bg.update(t + 60);
      const p = seg(t, 0, 0.7, ease.outQuart);
      this.frame.place({x: lerp(HERO.x, SIDE.x, p), y: lerp(HERO.y, SIDE.y, p), s: lerp(1, SIDE.s, p), o: 1});
      const group = box("overview-light", "attentionGroup", {x: 292, y: 218, width: 1270, height: 540});
      const focus = this.frame.focusOn({x: group.x, y: group.y - 10, width: group.width, height: group.height + 20}, 28, 1.3);
      this.frame.camera(camKF(this.frame, t, [[0, {s: 1, cx: 800, cy: 500}], [0.9, focus]], ease.outCubic));
      this.eyebrow.update(t, 0.5, B(21) - 0.6, {d: 0.4, stagger: 0});
      this.head.update(t, 0.7, B(21) - 0.6, {d: 0.6, stagger: 0.08});
      this.sub.update(t, 1.6, B(21) - 0.6, {d: 0.5, stagger: 0.03, rise: 20});
      const slot = 2.2;
      let active = -1;
      this.items.forEach((row, i) => {
        const t0 = 2.4 + i * slot;
        const on = seg(t, t0, 0.4, ease.outCubic), out = seg(t, B(21) - 0.6, 0.4, ease.inQuad);
        const isActive = t >= t0 && t < t0 + slot - 0.05;
        if (isActive) active = i;
        row.style.display = on <= 0.001 ? "none" : "";
        row.style.opacity = String(on * (1 - out) * (isActive || t >= 2.4 + 5 * slot ? 1 : 0.42));
        row.style.transform = `translateX(${(1 - on) * -24 + (isActive ? 14 : 0)}px)`;
        row.querySelector(".mark").style.transform = isActive ? "scale(1.35)" : "scale(1)";
      });
      if (active >= 0) {
        const b = box("overview-light", this.keys[active], {x: 292, y: 243 + active * 100, width: 1270, height: 90});
        const sb = this.frame.projectBox(b);
        this.spot.show(sb, 1, 10, 6);
        this.hl.show(b, 1, 3, 8);
      } else { this.spot.hide(); this.hl.show({x: 0, y: 0, width: 0, height: 0}, 0); }
    },
  };
  E.addScene(attentionScene);
  E.cue(B(38), "impact", {gain: 0.6});
  for (let i = 0; i < 5; i++) E.cue(B(38) + 2.4 + i * 2.2, "pop", {pitch: 700 + i * 90});

  // ====================================================================== S4 checkpoints
  const checkpointScene = {
    start: B(59), duration: B(17),
    mount(root) {
      this.bg = new E.Background(root, "dark");
      this.frame = new E.Frame(root, {src: SHOT("thread-cart"), title: "Ribbon Field — Cart totals rounding fix"});
      this.spot = new E.Spotlight(root);
      this.hl = new Highlight(this.frame);
      this.eyebrow = new E.Text(root, ["Checkpoints"], {cls: "eyebrow", size: 20, align: "left", x: 96, y: 150, width: 520});
      this.head = new E.Text(root, ["Pick up where", [{text: "you ", cls: ""}, {text: "left it.", cls: "gold"}]], {size: 88, align: "left", x: 92, y: 190, width: 540});
      this.sub = new E.Text(root, ["Agents save a factual checkpoint: phase, summary, next step, owner, and links. Context survives closed windows, restarts, and weeks away."], {size: 26, align: "left", x: 96, y: 420, width: 500, weight: 500, cls: "subtle"});
      this.c = [["Phase", "gold"], ["Next step · and who owns it", "gold"], ["Links · PR, bead, document", ""], ["Checkpoint history", ""], ["Commands, diffs, thinking · the whole turn", "green"]].map(([txt, cls]) => new E.Callout(root, txt, cls));
    },
    update(t) {
      this.bg.update(t + 80);
      enter(this.frame, t, 0, 0.9, SIDE, {rise: 40});
      const ctx = box("thread-cart", "checkpoint", {x: 1261, y: 44, width: 339, height: 450});
      const timeline = box("thread-cart", "timeline", {x: 254, y: 97, width: 1006, height: 735});
      const ctxFocus = this.frame.focusOn({x: ctx.x - 20, y: ctx.y, width: ctx.width + 40, height: 520}, 20, 1.75);
      const tlFocus = this.frame.focusOn({x: timeline.x + 20, y: 120, width: timeline.width - 40, height: 700}, 20, 1.25);
      this.frame.camera(camKF(this.frame, t, [[0.2, {s: 1, cx: 800, cy: 500}], [1.6, ctxFocus], [7.4, ctxFocus], [8.6, tlFocus]], ease.inOutCubic));
      this.eyebrow.update(t, 0.4, B(17) - 0.6, {d: 0.4, stagger: 0});
      this.head.update(t, 0.6, B(17) - 0.6, {d: 0.6, stagger: 0.08});
      this.sub.update(t, 1.5, B(17) - 0.6, {d: 0.5, stagger: 0.02, rise: 20});
      // Page-space targets inside the context panel (from the capture layout).
      const targets = [
        {b: {x: 1283, y: 72, width: 280, height: 20}, dx: -60, dy: -60, t0: 2.2, t1: 3.8},
        {b: {x: 1283, y: 178, width: 300, height: 48}, dx: -80, dy: 50, t0: 3.9, t1: 5.5},
        {b: box("thread-cart", "links", {x: 1283, y: 232, width: 295, height: 140}), dx: -90, dy: -36, t0: 5.6, t1: 7.0},
        {b: {x: 1283, y: 414, width: 140, height: 22}, dx: -80, dy: 52, t0: 7.0, t1: 8.3},
        {b: {x: 282, y: 162, width: 950, height: 420}, dx: 40, dy: -56, t0: 9.0, t1: B(17) - 0.5},
      ];
      let any = false;
      targets.forEach((tg, i) => {
        const v = visible(t, tg.t0, tg.t1, 0.35, 0.25);
        const anchor = this.frame.project(tg.b.x + (i === 4 ? 24 : 6), tg.b.y + (i === 4 ? 20 : tg.b.height / 2));
        this.c[i].show(anchor.x, anchor.y, tg.dx, tg.dy, v);
        if (v > 0.001 && t < tg.t1 - 0.2) { this.hl.show(tg.b, v, i === 4 ? 10 : 6, 8); any = true; }
      });
      if (!any) this.hl.show({x: 0, y: 0, width: 0, height: 0}, 0);
    },
  };
  E.addScene(checkpointScene);
  E.cue(B(59), "whoosh", {length: 0.5});
  [2.2, 3.9, 5.6, 7.0, 9.0].forEach((d, i) => E.cue(B(59) + d, "pop", {pitch: 820 + i * 60}));

  // ====================================================================== S5 conversations
  const convoScene = {
    start: B(76), duration: B(18),
    mount(root) {
      this.bg = new E.Background(root, "dark");
      this.f1 = new E.Frame(root, {src: SHOT("draft-model-menu"), title: "Ribbon Field — New session"});
      this.f2 = new E.Frame(root, {src: SHOT("thread-slash"), title: "Ribbon Field — Cart totals rounding fix"});
      this.f3 = new E.Frame(root, {src: SHOT("thread-draft"), title: "Ribbon Field — Cart totals rounding fix"});
      this.cursor = new E.Cursor(root);
      this.lt1 = new LowerThird(root, "Codex and Claude Code. Same workspace.", "Pick a provider and model per conversation. Streamed replies, approvals, and questions in one chat.");
      this.lt2 = new LowerThird(root, "Slash commands for the things you do constantly.", "/cd · /model · /effort · /permissions · /fork · /close");
      this.lt3 = new LowerThird(root, "Enter sends. Tab queues. Steer a running turn.", "Drop up to four images. Follow-ups wait their turn, even if you close the tab.");
      this.c1 = new E.Callout(root, "OpenAI · Codex models", "");
      this.c2 = new E.Callout(root, "Anthropic · Claude Code", "gold");
      this.c3 = new E.Callout(root, "Queue ⇥ · Steer · Stop", "green");
    },
    update(t) {
      this.bg.update(t + 100);
      const s1 = seg(t, 5.6, 0.7, ease.inOutCubic), s2 = seg(t, 10.9, 0.7, ease.inOutCubic);
      enter(this.f1, t, 0, 0.9, HERO, {rise: 50});
      this.f1.node.style.opacity = String(Math.min(this.f1.pos.o, 1 - s1));
      this.f2.place({...HERO, o: s1 * (1 - s2)});
      this.f3.place({...HERO, o: s2});
      const menu = box("draft-model-menu", "menu", {x: 290, y: 512, width: 285, height: 360});
      const menuFocus = this.f1.focusOn({x: menu.x - 40, y: menu.y - 30, width: menu.width + 500, height: menu.height + 60}, 30, 1.9);
      this.f1.camera(camKF(this.f1, t, [[0.3, {s: 1.05, cx: 800, cy: 540}], [1.6, menuFocus]], ease.inOutCubic));
      const slash = box("thread-slash", "slash", {x: 283, y: 507, width: 948, height: 320});
      const slashFocus = this.f2.focusOn({x: slash.x - 20, y: slash.y - 20, width: slash.width + 40, height: slash.height + 160}, 20, 1.5);
      this.f2.camera(camKF(this.f2, t, [[5.6, {s: 1.1, cx: 800, cy: 650}], [6.8, slashFocus]], ease.inOutCubic));
      const composer = box("thread-draft", "composer", {x: 282, y: 832, width: 950, height: 118});
      const compFocus = this.f3.focusOn({x: composer.x - 30, y: composer.y - 230, width: composer.width + 60, height: composer.height + 260}, 20, 1.45);
      this.f3.camera(camKF(this.f3, t, [[10.9, {s: 1.05, cx: 800, cy: 760}], [12.0, compFocus]], ease.inOutCubic));
      this.lt1.update(t, 1.2, 5.4);
      this.lt2.update(t, 6.6, 10.6);
      this.lt3.update(t, 12.0, B(18) - 0.3);
      // Callouts on the model menu groups.
      const openai = this.f1.project(menu.x + 30, menu.y + 22), claude = this.f1.project(menu.x + 30, menu.y + menu.height - 36);
      this.c1.show(openai.x, openai.y, -70, -40, visible(t, 2.4, 5.3) * (1 - s1));
      this.c2.show(claude.x, claude.y, -70, 44, visible(t, 3.2, 5.3) * (1 - s1));
      // Cursor: type then click Send.
      const send = box("thread-draft", "send", {x: 1154, y: 912, width: 65, height: 29});
      const sendP = this.f3.project(send.x + send.width / 2, send.y + send.height / 2);
      const q = box("thread-draft", "queue", {x: 1080, y: 912, width: 70, height: 29});
      const qp = this.f3.project(send.x - 110, send.y + send.height / 2);
      const path = pathKF(t, [[11.4, sendP.x - 420, sendP.y + 160], [13.2, sendP.x + 2, sendP.y + 2], [15.6, sendP.x + 2, sendP.y + 2], [16.4, qp.x, qp.y]]);
      this.cursor.set(path.x, path.y, s2 * visible(t, 11.4, B(18) - 0.2, 0.3, 0.3));
      this.cursor.ripple(t - 13.4, sendP.x + 2, sendP.y + 2);
      this.c3.show(qp.x, qp.y - 10, -40, -70, visible(t, 14.0, B(18) - 0.3));
    },
  };
  E.addScene(convoScene);
  E.cue(B(76), "whoosh", {length: 0.5});
  E.cue(B(76) + 2.4, "pop", {pitch: 900});
  E.cue(B(76) + 3.2, "pop", {pitch: 1100});
  E.cue(B(76) + 5.6, "whoosh", {length: 0.4});
  E.cue(B(76) + 10.9, "whoosh", {length: 0.4});
  E.cue(B(76) + 13.4, "click");
  E.cue(B(76) + 14.0, "pop", {pitch: 1000});

  // ====================================================================== S6 tiled
  const tiledScene = {
    start: B(94), duration: B(10),
    mount(root) {
      this.bg = new E.Background(root, "dark");
      this.frame = new E.Frame(root, {src: SHOT("tiled"), title: "Ribbon Field — Checkout redesign · Tiled threads"});
      this.lt = new LowerThird(root, "Six conversations. One window.", "Tile a view's open threads side by side · each pane keeps its own composer, scroll, and approvals · ⌃⌥⌘T");
    },
    update(t) {
      this.bg.update(t + 120);
      this.frame.place({...HERO, o: 1});
      const pane = box("tiled", "pane", {x: 260, y: 108, width: 439, height: 439});
      const paneFocus = this.frame.focusOn(pane, 10, 2.2);
      this.frame.camera(camKF(this.frame, t, [[0.2, paneFocus], [2.6, {s: 1, cx: 800, cy: 500}]], ease.inOutCubic));
      this.lt.update(t, 2.2, B(10) - 0.3);
    },
  };
  E.addScene(tiledScene);
  E.cue(B(94), "impact", {gain: 0.55});
  E.cue(B(94) + 3.4, "pop", {pitch: 1250});

  // ====================================================================== S7 views
  const viewsScene = {
    start: B(104), duration: B(13),
    mount(root) {
      this.bg = new E.Background(root, "dark");
      this.f = ["view-checkout", "view-infra", "view-needsme"].map(n => new E.Frame(root, {src: SHOT(n), title: "Ribbon Field — Saved views"}));
      this.cursor = new E.Cursor(root);
      this.lt = new LowerThird(root, "Focus by project, repository, or stage.", "Save filters and grouping as a tab. Each tab counts what needs you.");
      this.c1 = new E.Callout(root, "Projects can span repositories", "");
      this.c2 = new E.Callout(root, "Grouped by project", "gold");
      this.c3 = new E.Callout(root, "Only what needs you", "green");
    },
    update(t) {
      this.bg.update(t + 140);
      const s1 = seg(t, 2.5, 0.45, ease.inOutCubic), s2 = seg(t, 5.0, 0.45, ease.inOutCubic);
      this.f[0].place({...HERO, o: 1 - s1}); this.f[1].place({...HERO, o: s1 * (1 - s2)}); this.f[2].place({...HERO, o: s2});
      const tabs = box("view-checkout", "viewTabs", {x: 292, y: 48, width: 1063, height: 53});
      const focus = this.f[0].focusOn({x: tabs.x - 40, y: tabs.y - 30, width: 1300, height: 560}, 20, 1.35);
      for (const f of this.f) f.camera(camKF(f, t, [[0.3, {s: 1, cx: 800, cy: 500}], [1.3, focus]], ease.inOutCubic));
      const tabInfra = box("view-infra", "activeTab", {x: 620, y: 54, width: 100, height: 47});
      const tabNeeds = box("view-needsme", "activeTab", {x: 720, y: 54, width: 130, height: 47});
      const tabCheckout = box("view-checkout", "activeTab", {x: 409, y: 54, width: 176, height: 47});
      const P = b => this.f[0].project(b.x + b.width / 2, b.y + b.height / 2);
      const pc = P(tabCheckout), pi = P(tabInfra), pn = P(tabNeeds);
      const path = pathKF(t, [[0.4, pc.x + 300, pc.y + 300], [1.5, pc.x, pc.y], [2.0, pc.x, pc.y], [2.5, pi.x, pi.y], [4.4, pi.x, pi.y], [5.0, pn.x, pn.y], [7.5, pn.x, pn.y], [8.4, pn.x + 90, pn.y + 120]]);
      this.cursor.set(path.x, path.y, visible(t, 0.4, B(13) - 0.3, 0.3, 0.3));
      if (t >= 2.5 && t < 3.1) this.cursor.ripple(t - 2.5, pi.x, pi.y);
      else if (t >= 5.0 && t < 5.6) this.cursor.ripple(t - 5.0, pn.x, pn.y);
      else this.cursor.ripple(-1);
      this.lt.update(t, 0.9, B(13) - 0.3);
      this.c1.show(pc.x, pc.y + 24, -40, 70, visible(t, 1.4, 2.4));
      this.c2.show(pi.x, pi.y + 24, 50, 70, visible(t, 3.3, 4.8));
      this.c3.show(pn.x, pn.y + 24, 60, 70, visible(t, 5.8, B(13) - 0.4));
    },
  };
  E.addScene(viewsScene);
  E.cue(B(104), "whoosh", {length: 0.5});
  E.cue(B(104) + 2.5, "click");
  E.cue(B(104) + 5.0, "click");
  E.cue(B(104) + 1.4, "pop", {pitch: 900});
  E.cue(B(104) + 3.3, "pop", {pitch: 1000});
  E.cue(B(104) + 5.8, "pop", {pitch: 1150});

  // ====================================================================== S8 now / later / closed / roll up
  const placementScene = {
    start: B(117), duration: B(11),
    mount(root) {
      this.bg = new E.Background(root, "dark");
      this.f = ["overview-light", "overview-later", "overview-closed", "rollup"].map(n => new E.Frame(root, {src: SHOT(n), title: "Ribbon Field — All work"}));
      this.cursor = new E.Cursor(root);
      this.lt1 = new LowerThird(root, "Now. Later. Closed. Your call.", "Park a thread without losing its context. Reopen a closed one any time.");
      this.lt2 = new LowerThird(root, "Roll up: work through the waiting queue, one thread at a time.", "Handled & next · Skip · Exit — ⌥⌘R");
    },
    update(t) {
      this.bg.update(t + 160);
      const s1 = seg(t, 1.3, 0.4, ease.inOutCubic), s2 = seg(t, 2.9, 0.4, ease.inOutCubic), s3 = seg(t, 4.5, 0.5, ease.inOutCubic);
      this.f[0].place({...HERO, o: 1 - s1}); this.f[1].place({...HERO, o: s1 * (1 - s2)}); this.f[2].place({...HERO, o: s2 * (1 - s3)}); this.f[3].place({...HERO, o: s3});
      const place = box("overview-light", "placement", {x: 670, y: 130, width: 165, height: 44});
      const focusTop = this.f[0].focusOn({x: 260, y: 30, width: 1340, height: 700}, 20, 1.2);
      const roll = box("overview-light", "rollUp", {x: 20, y: 217, width: 214, height: 39});
      const bar = box("rollup", "bar", {x: 254, y: 44, width: 1346, height: 58});
      const rollFocus = this.f[3].focusOn({x: 0, y: 0, width: 1600, height: 760}, 10, 1.3);
      for (const f of this.f) f.camera(camKF(f, t, [[0.2, {s: 1, cx: 800, cy: 500}], [1.0, focusTop], [4.4, focusTop], [5.2, rollFocus]], ease.inOutCubic));
      const later = this.f[0].project(place.x + place.width * 0.5, place.y + place.height / 2);
      const closed = this.f[0].project(place.x + place.width * 0.83, place.y + place.height / 2);
      const rollP = this.f[0].project(roll.x + roll.width / 2, roll.y + roll.height / 2);
      const handle = this.f[3].project(bar.x + bar.width - 230, bar.y + bar.height / 2);
      const path = pathKF(t, [[0.3, later.x + 200, later.y + 260], [1.2, later.x, later.y], [2.3, later.x, later.y], [2.8, closed.x, closed.y], [3.7, closed.x, closed.y], [4.5, rollP.x, rollP.y], [5.4, rollP.x, rollP.y], [6.2, handle.x, handle.y]]);
      this.cursor.set(path.x, path.y, visible(t, 0.3, B(11) - 0.3, 0.3, 0.3));
      const clicks = [[1.3, later], [2.9, closed], [4.5, rollP]];
      let drawn = false;
      for (const [ct, pt] of clicks) { if (t >= ct && t < ct + 0.6) { this.cursor.ripple(t - ct, pt.x, pt.y); drawn = true; } }
      if (!drawn) this.cursor.ripple(-1);
      this.lt1.update(t, 0.6, 4.3);
      this.lt2.update(t, 5.3, B(11) - 0.3);
    },
  };
  E.addScene(placementScene);
  E.cue(B(117) + 1.3, "click");
  E.cue(B(117) + 2.9, "click");
  E.cue(B(117) + 4.5, "click");
  E.cue(B(117) + 5.3, "pop", {pitch: 1000});

  // ====================================================================== S9 coordination
  const coordScene = {
    start: B(128), duration: B(26),
    mount(root) {
      this.bg = new E.Background(root, "dark");
      this.eyebrow = new E.Text(root, ["Coordination"], {cls: "eyebrow", size: 20, align: "left", x: 96, y: 86, width: 900});
      this.head = new E.Text(root, [[{text: "Agents that ", cls: ""}, {text: "don't step on each other.", cls: "gold"}]], {size: 64, align: "left", x: 92, y: 116, width: 1500});
      this.term = new E.Terminal(root, {x: 92, y: 230, width: 980, height: 720, title: "zsh — agent-coord"});
      const sid = "7f3a…", other = "2c91…";
      this.term.setScript([
        {type: "cmd", at: 1.2, text: `agent-coord begin-work --session-id ${sid} --scope 'src/checkout/**' --lease-mode write`},
        {type: "out", at: 3.1, cls: "ok", text: "✓ cart-totals-worker now owns src/checkout/** (write lease)"},
        {type: "cmd", at: 4.0, text: `agent-coord begin-work --session-id ${other} --scope 'src/checkout/cart_totals.py'`},
        {type: "out", at: 5.9, cls: "err", text: "✗ Conflict: src/checkout/cart_totals.py is owned by cart-totals-worker"},
        {type: "out", at: 6.2, cls: "out", text: "  ↳ sent action_required to cart-totals-worker: \"Can you release cart_totals.py?\""},
        {type: "cmd", at: 7.2, text: `agent-coord delegate --from-session ${sid} --bead acme-151 --client claude --scope 'tests/e2e/**' --lease-mode validation`},
        {type: "out", at: 10.1, cls: "ok", text: "✓ Launched autocomplete-validator in an Agent Coord-owned PTY · delegation 65ab…"},
        {type: "cmd", at: 11.0, text: `agent-coord handoff --from-session ${sid} --to-session ${other} --patch-label cart-totals-v2 --mode validation`},
        {type: "out", at: 13.3, cls: "ok", text: "✓ Transferred 2 scopes + validation boundary in one SQLite transaction"},
        {type: "out", at: 13.6, cls: "em", text: "no window where the files were unowned"},
      ]);
      this.monitor = new E.Frame(root, {src: SHOT("monitor-child"), title: "Ribbon Field — Coordination monitor", fit: 0.49});
      this.c1 = new E.Callout(root, "Parent / child tree", "gold");
      this.c2 = new E.Callout(root, "Live, bounded terminal output", "");
      this.c3 = new E.Callout(root, "Durable messages · wake idle agents", "green");
      this.chips = [new Chip(root, "Write scopes & conflict detection"), new Chip(root, "Delegate to Codex or Claude, own PTY"), new Chip(root, "Atomic handoff")];
    },
    update(t) {
      this.bg.update(t + 180);
      this.eyebrow.update(t, 0.2, B(26) - 0.5, {d: 0.4, stagger: 0});
      this.head.update(t, 0.4, B(26) - 0.5, {d: 0.6, stagger: 0.07});
      const termIn = seg(t, 0.6, 0.8, ease.outQuart);
      setT(this.term.node, {x: 0, y: (1 - termIn) * 40, o: termIn * (1 - seg(t, B(26) - 0.5, 0.4, ease.inQuad))});
      this.term.update(t);
      // Monitor window slides in from the right after the delegate command.
      const mIn = seg(t, 8.4, 1.0, ease.outQuart);
      const base = {x: 1110 + (1 - mIn) * 300, y: 440, s: 1, o: mIn};
      this.monitor.place(base);
      const treeFocus = this.monitor.focusOn({x: 0, y: 60, width: 1600, height: 680}, 10, 1.2);
      const outFocus = this.monitor.focusOn({x: 330, y: 330, width: 1240, height: 260}, 16, 1.7);
      const msgFocus = this.monitor.focusOn({x: 300, y: 70, width: 1300, height: 560}, 16, 1.4);
      this.monitor.camera(camKF(this.monitor, t, [[8.4, {s: 1.0, cx: 800, cy: 500}], [10.0, treeFocus], [11.6, treeFocus], [12.4, outFocus], [13.4, outFocus], [14.1, msgFocus]], ease.inOutCubic));
      const tree = this.monitor.project(190, 660), out = this.monitor.project(1120, 380), msgs = this.monitor.project(553, 394);
      this.c1.show(tree.x, tree.y, -30, -60, visible(t, 9.8, 11.6) * mIn);
      this.c2.show(out.x, out.y, -120, -70, visible(t, 11.9, 13.4) * mIn);
      this.c3.show(msgs.x, msgs.y, -150, -70, visible(t, 13.7, B(26) - 0.4) * mIn);
      this.chips[0].update(t, 3.4, B(26) - 0.5, 1110, 222);
      this.chips[1].update(t, 10.4, B(26) - 0.5, 1110, 294);
      this.chips[2].update(t, 13.6, B(26) - 0.5, 1110, 366);
    },
  };
  E.addScene(coordScene);
  E.cue(B(128), "impact", {gain: 0.5});
  E.cue(B(128) + 3.1, "pop", {pitch: 1200});
  E.cue(B(128) + 5.9, "pop", {pitch: 420});
  E.cue(B(128) + 8.4, "whoosh", {length: 0.6});
  E.cue(B(128) + 10.1, "pop", {pitch: 1200});
  E.cue(B(128) + 13.3, "pop", {pitch: 1400});

  // ====================================================================== S10 everywhere
  const everywhereScene = {
    start: B(154), duration: B(11),
    mount(root) {
      this.bg = new E.Background(root, "dark");
      this.dark = new E.Frame(root, {src: SHOT("overview-dark"), title: "Ribbon Field", fit: 0.52});
      this.phone = new E.Phone(root, {src: SHOT("mobile-thread")});
      this.eyebrow = new E.Text(root, ["Everywhere"], {cls: "eyebrow", size: 20, align: "left", x: 96, y: 86, width: 900});
      this.head = new E.Text(root, [[{text: "Leave the laptop. ", cls: ""}, {text: "Keep the thread.", cls: "gold"}]], {size: 68, align: "left", x: 92, y: 120, width: 1200});
      this.chips = [["Native macOS app", ""], ["iPhone via private Tailscale HTTPS", ""], ["Desktop notifications", ""],
                    ["Dark mode", ""], ["New session", "⌘N"], ["Roll up waiting threads", "⌥⌘R"], ["Switch views", "⌃⇧←→"], ["Command-click opens beside", ""]]
        .map(([txt, key]) => new Chip(root, txt, key));
    },
    update(t) {
      this.bg.update(t + 200);
      this.eyebrow.update(t, 0.2, B(11) - 0.5, {d: 0.4, stagger: 0});
      this.head.update(t, 0.4, B(11) - 0.5, {d: 0.6, stagger: 0.07});
      const dIn = seg(t, 0.8, 1.0, ease.outQuart);
      this.dark.place({x: 92, y: 290 + (1 - dIn) * 60, s: 1, o: dIn});
      this.dark.camera({s: 1, cx: 800, cy: 500});
      const pIn = seg(t, 1.4, 1.1, ease.outQuart);
      this.phone.place({x: 1530 + (1 - pIn) * 400, y: 250, s: 0.86, o: pIn, r: (1 - pIn) * 6});
      const positions = [[970, 300], [970, 378], [970, 456], [970, 534], [970, 632], [970, 710], [970, 788], [970, 866]];
      this.chips.forEach((c, i) => c.update(t, 2.2 + i * 0.38, B(11) - 0.5, positions[i][0], positions[i][1]));
    },
  };
  E.addScene(everywhereScene);
  E.cue(B(154), "whoosh", {length: 0.6});
  for (let i = 0; i < 8; i++) E.cue(B(154) + 2.2 + i * 0.38, "pop", {pitch: 800 + i * 70});

  // ====================================================================== S11 simple
  const simpleScene = {
    start: B(165), duration: B(10),
    mount(root) {
      this.bg = new E.Background(root, "dark");
      this.head = new E.Text(root, [[{text: "One Python CLI. ", cls: ""}, {text: "SQLite. Hooks.", cls: "green"}], [{text: "No MCP server. No daemon. ", cls: "muted"}, {text: "No lock-in.", cls: "gold"}]], {size: 74, y: 180, width: 1700});
      this.term = new E.Terminal(root, {x: 330, y: 470, width: 1260, height: 330, title: "zsh"});
      this.term.setScript([
        {type: "cmd", at: 1.0, text: "claude plugin install agent-coord@agent-coord"},
        {type: "out", at: 2.3, cls: "ok", text: "✓ installed · hooks register every session automatically"},
        {type: "cmd", at: 2.9, text: "codex plugin add agent-coord@personal"},
        {type: "out", at: 4.0, cls: "ok", text: "✓ installed · same skills, same database"},
        {type: "cmd", at: 4.5, text: "open ~/Applications/Ribbon\\ Field.app"},
      ]);
    },
    update(t) {
      this.bg.update(t + 220);
      this.head.update(t, 0.2, B(10) - 0.5, {d: 0.6, stagger: 0.06});
      const tin = seg(t, 0.6, 0.8, ease.outQuart);
      setT(this.term.node, {x: 0, y: (1 - tin) * 40, o: tin * (1 - seg(t, B(10) - 0.5, 0.4, ease.inQuad))});
      this.term.update(t, 40);
    },
  };
  E.addScene(simpleScene);
  E.cue(B(165), "whoosh", {length: 0.5});

  // ====================================================================== S12 outro
  const outroScene = {
    start: B(175), duration: B(13),
    mount(root) {
      this.bg = new E.Background(root, "dark");
      this.logo = el("div", "logo"); this.logo.append(el("div", "rf", "rf"), el("div", "dot")); root.append(this.logo);
      this.word = el("div", "wordmark", "Ribbon Field"); root.append(this.word);
      this.tag = el("div", "tagline", "A place for work in progress."); root.append(this.tag);
      this.line = new E.Text(root, ["Leave agents working.", [{text: "Return to the ", cls: ""}, {text: "right conversation.", cls: "gold"}]], {size: 62, y: 700, width: 1500, weight: 600});
      this.foot = new E.Text(root, ["Open source · works with Claude Code and Codex · runs on your machine"], {cls: "mono", size: 22, y: 950, width: 1600, weight: 500});
    },
    update(t) {
      this.bg.update(t + 240);
      const inL = seg(t, 0.2, 0.9, ease.outBack);
      const logoX = 520, logoY = 300;
      const fade = 1 - seg(t, B(13) - 1.0, 1.0, ease.inQuad);
      setT(this.logo, {x: logoX, y: logoY, s: Math.max(0.001, 0.7 + 0.3 * inL), o: inL * fade, r: (1 - inL) * 8});
      this.logo.querySelector(".dot").style.transform = `scale(${seg(t, 0.7, 0.4, ease.outBack)})`;
      const slide = seg(t, 0.8, 0.9, ease.outQuart);
      this.word.style.left = (logoX + 288) + "px"; this.word.style.top = (logoY + 50) + "px";
      this.word.style.clipPath = `inset(0 ${(1 - slide) * 100}% 0 0)`;
      setT(this.word, {x: -30 * (1 - slide), o: slide * fade});
      this.tag.style.left = (logoX + 294) + "px"; this.tag.style.top = (logoY + 188) + "px";
      const tagP = seg(t, 1.5, 0.6);
      setT(this.tag, {y: (1 - tagP) * 14, o: tagP * fade});
      this.line.update(t, 2.4, B(13) - 1.2, {d: 0.6, stagger: 0.06});
      this.foot.update(t, 3.6, B(13) - 1.2, {d: 0.5, stagger: 0.015, rise: 14});
    },
  };
  E.addScene(outroScene);
  E.cue(B(175), "impact", {gain: 0.6});
  E.cue(B(175) + 0.7, "pop", {pitch: 1320});
})();
