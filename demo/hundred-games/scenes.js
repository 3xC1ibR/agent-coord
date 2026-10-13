/* Editorial diagram, not app UI. Every frame is a pure function of time.
 * One request launches one hundred game agents across four providers, every
 * chat is closed, and one later request wakes the same three agents with their
 * context intact. The games are make-believe; nothing here runs a provider. */
"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const clamp = n => Math.min(1, Math.max(0, n));
  const ease = n => 1 - Math.pow(1 - clamp(n), 3);
  const smooth = n => { const x = clamp(n); return x * x * (3 - 2 * x); };
  const back = n => { const x = clamp(n), c = 1.5; return 1 + (c + 1) * Math.pow(x - 1, 3) + c * Math.pow(x - 1, 2); };
  const progress = (t, start, length = .5) => ease((t - start) / length);
  const fade = (t, start, end, length = .4) => progress(t, start, length) * (1 - progress(t, end - length, length));
  const mix = (a, b, p) => a + (b - a) * p;
  const show = (el, o) => { el.style.opacity = String(clamp(o)); el.style.visibility = o <= 0 ? "hidden" : "visible"; };
  let seed = 100;
  const rand = () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; };
  const shuffle = list => { for (let i = list.length - 1; i > 0; i--) { const j = Math.floor(rand() * (i + 1)); [list[i], list[j]] = [list[j], list[i]]; } return list; };
  const NS = "http://www.w3.org/2000/svg";
  const add = (parent, name, attrs = {}) => { const e = document.createElementNS(NS, name); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); parent.append(e); return e; };
  const text = (parent, attrs, content) => { const e = add(parent, "text", attrs); e.textContent = content; return e; };

  // ---- Cast ------------------------------------------------------------------------------------
  const PROVIDERS = [
    {name: "OpenAI", tone: "#3f7062", tint: "#e4eee8"},
    {name: "Anthropic", tone: "#9c7949", tint: "#f1eadd"},
    {name: "Gemini", tone: "#4f7a8a", tint: "#e3ebee"},
    {name: "Kimi", tone: "#847099", tint: "#efebf4"},
  ];
  const HEROES = {23: {name: "Pigeon Bread Heist", provider: 1, glyph: "pigeon"}, 67: {name: "Parallel Parking on Mars", provider: 2, glyph: "mars"},
    45: {name: "Spreadsheet Boss Fight", provider: 3, glyph: "sheet"}, 81: {name: "Escape the Meeting", provider: 0, glyph: "meeting"}};
  const NAMES = ["Raccoon Tax Season", "Goose Customs", "Cat Elevator Rush", "Haunted Laundromat", "Moon Laundry", "Dentist for Dragons", "Snail Delivery", "Toaster Tycoon",
    "Ghost Accountant", "Llama Lifeguard", "Fridge Raiders", "Office Chair Derby", "Sock Matching Speedrun", "Penguin Pizza Night", "Dog Walk Chaos", "Volcano Bakery",
    "Hamster Heist", "Sleepy Sloth Racer", "Umbrella Fencing", "Alarm Clock Boss", "Cactus Hug Trials", "Koala Courier", "Moth vs. Lamp", "Invisible Maze",
    "Duck Tax Audit", "Traffic Cone Collector", "Gravity Gardening", "Submarine Sudoku", "Ferret Fire Drill", "Cloud Shepherd", "Teacup Rally", "Robot Yoga",
    "Owl Night Shift", "Lost Luggage Lottery", "Lighthouse Keeper", "Jellyfish Jump", "Bus Stop Standoff", "Broccoli Defense", "Mirror Maze Mail", "Popcorn Avalanche",
    "Yeti Hairdresser", "Pillow Fort Siege", "Beaver Bridge Builder", "Comet Catcher", "Tofu Tower", "Walrus Wedding", "Dishwasher Tetris", "Meteor Mini Golf",
    "Squid Spelling Bee", "Garage Sale Rush", "Kite Storm", "Snowman Sauna", "Honey Heist", "Lunch Line Legend", "Crab Crossword", "Printer Rebellion",
    "Night Bus Navigator", "Frog Fax Machine", "Lava Lamp Logic", "Piano Stairs", "Shopping Cart Drift", "Otter Orchestra", "Window Washer Wobble", "Parrot Password",
    "Hedgehog Hurdles", "Balloon Mail", "Vending Machine Vault", "Sheep Sorting", "Thunder Toast", "Moose on Skates", "Rainy Day Rocket", "Tiny Tornado Tamer",
    "Bee Bookkeeper", "Igloo Inspector", "Wizard Dishwasher", "Pancake Flip Fever", "Fog Horn Hero", "Chameleon Camouflage", "Seagull Snack Standoff", "Bubble Wrap Bandit",
    "Elevator Pitch Panic", "Puddle Jumper Pro", "Narwhal Navigator", "Turtle Taxi", "Coffee Cup Catapult", "Jigsaw Jailbreak", "Meerkat Meeting", "Ski Lift Shuffle",
    "Garden Gnome Patrol", "Ice Cream Inventory", "Pigeon Pilot", "Zero-G Ping Pong", "Capybara Car Wash", "Sandwich Sorting Hat", "Lobster Lost and Found", "Spelling Bee Sprint"];
  const ARCHETYPES = ["snake", "pong", "invaders", "blocks", "maze", "flappy", "racer", "cards", "dice", "jumper"];
  const N = 100, COLS = 10, TILE = 44, GAP = 5, STEP = TILE + GAP, GX = 298, GY = 462;
  const HUB = {x: 540, y: 398};
  const cx = i => GX + (i % COLS) * STEP + TILE / 2, cy = i => GY + Math.floor(i / COLS) * STEP + TILE / 2;
  const providerOf = new Array(N).fill(-1), counts = [0, 0, 0, 0];
  for (const [i, h] of Object.entries(HEROES)) { providerOf[i] = h.provider; counts[h.provider]++; }
  for (const i of shuffle([...Array(N).keys()])) {
    if (providerOf[i] >= 0) continue;
    let p = 0; for (let k = 1; k < 4; k++) if (counts[k] < counts[p]) p = k;
    providerOf[i] = p; counts[p]++;
  }
  let nameCursor = 0;
  const tiles = Array.from({length: N}, (_, i) => {
    const hero = HEROES[i];
    return {i, hero, name: hero ? hero.name : NAMES[nameCursor++], provider: providerOf[i], glyph: hero ? hero.glyph : ARCHETYPES[i % ARCHETYPES.length], phase: rand() * 6.28};
  });
  const byDistance = (x0, y0) => [...Array(N).keys()].sort((a, b) => Math.hypot(cx(a) - x0, cy(a) - y0) - Math.hypot(cx(b) - x0, cy(b) - y0));
  const burstRank = new Array(N), closeRank = new Array(N);
  byDistance(HUB.x, HUB.y).forEach((i, k) => burstRank[i] = k);
  byDistance(540, GY + (COLS * STEP - GAP) / 2).forEach((i, k) => closeRank[i] = k);
  const buildOrder = new Array(N); shuffle([...Array(N).keys()]).forEach((i, k) => buildOrder[i] = k);

  // ---- Timeline (seconds; the soundtrack runs at 120 BPM so bars fall on even seconds) ---------
  const T = {burst: 4.0, spread: 2.4, flight: .55, legend: 4.6, build: 12.6, buildSpread: 7.2, allBuilt: 20.0,
    live: 26.6, reportPacket: {at: 27.0, end: 27.8}, report: {at: 28.0, end: 30.6}, close: 31.2, closeSpread: 2.2,
    wakeSend: 39.0, outro: 51.8, duration: 59};
  const depart = i => T.burst + burstRank[i] / (N - 1) * T.spread, landAt = i => depart(i) + T.flight;
  const playableAt = i => HEROES[i]?.glyph === "pigeon" ? 14.4 : HEROES[i]?.glyph === "mars" ? 17.8 : T.build + buildOrder[i] / (N - 1) * T.buildSpread;
  const closedAt = i => T.close + closeRank[i] / (N - 1) * T.closeSpread;
  const WAKES = [{tile: 23, at: 39.3}, {tile: 67, at: 39.7}, {tile: 45, at: 40.1}];
  const WAKE_FLIGHT = .8;
  const wakeAt = i => { const w = WAKES.find(w => w.tile === i); return w ? w.at + WAKE_FLIGHT : Infinity; };
  const headings = [
    {at: 0, end: 4.0, l1: "One request.", l2: "One hundred studios."},
    {at: 4.0, end: 11.8, l1: "One chat becomes a swarm.", l2: "Four providers, one team."},
    {at: 12.0, end: 20.4, l1: "A hundred weird games.", l2: "Built in parallel."},
    {at: 20.5, end: 26.4, l1: "They share one launcher.", l2: "They settle it themselves."},
    {at: 26.6, end: 30.8, l1: "One arcade.", l2: "Reported back to you."},
    {at: 31.0, end: 36.0, l1: "Now close every chat.", l2: "The work stays."},
    {at: 36.2, end: T.outro, l1: "One request.", l2: "The right three agents wake up."},
  ];
  const requests = [
    {at: .8, typed: 2.4, send: 2.9, end: 7.4, text: "Build an arcade with 100 one-screen games. Give each game its own agent. Make them weird."},
    {at: 36.4, typed: 38.6, send: T.wakeSend, end: 42.0, text: "Make Pigeon Bread Heist multiplayer, give Mars Parking moon gravity, and make Spreadsheet Boss Fight playable on my phone."},
  ];
  const captions = [
    {at: 7.6, end: 11.6, text: "An OpenAI dispatcher launches agents on Gemini, Anthropic, Kimi, and OpenAI."},
    {at: 12.4, end: 15.8, text: "Each agent owns one game and keeps its own conversation."},
    {at: 20.9, end: 26.2, text: "Two studios reach for the launcher's shared file. They settle it between themselves."},
    {at: 31.6, end: 35.6, text: "Closing a chat keeps its history and context."},
    {at: 42.4, end: 51.2, text: "Closed chats reopen with their history. Nobody has to remember which chat built what."},
  ];
  const lenses = [
    {at: 13.0, end: 16.0, tile: 23, done: 14.4, working: "Building…", finished: "Playable ✓"},
    {at: 16.4, end: 19.4, tile: 67, done: 17.8, working: "Building…", finished: "Playable ✓"},
    {at: 41.2, end: 44.0, tile: 23, done: 42.6, working: "Adding a second player…", finished: "Multiplayer ✓", variant: "multiplayer", reopened: true},
    {at: 44.2, end: 47.0, tile: 67, done: 45.6, working: "Lowering the gravity…", finished: "Moon gravity ✓", variant: "moon", reopened: true},
    {at: 47.2, end: 51.4, tile: 45, done: 48.4, working: "Adding touch controls…", finished: "Phone controls ✓", variant: "phone", reopened: true,
      direct: 49.2, directText: "Also let me press R to restart.", directDone: 50.6, directFinished: "Phone controls ✓ · R restarts ✓"},
  ];
  const duo = {at: 20.6, end: 26.2, a: 45, b: 81, conflict: 21.4, pills: [{at: 22.4, end: 24.0, text: "→  I'll own launcher.js."}, {at: 24.0, end: 26.0, text: "←  I'll register through your API."}], resolved: 25.4};

  // ---- Tile glyphs (40 × 40) -------------------------------------------------------------------
  const GLYPHS = {
    snake: {build: g => Array.from({length: 4}, () => add(g, "rect", {width: 6, height: 6, rx: 1.5, class: "fill"})),
      update: (els, t, ph) => { const path = [[8, 12], [14, 12], [20, 12], [26, 12], [26, 18], [26, 24], [20, 24], [14, 24], [8, 24], [8, 18]]; const step = Math.floor(t * 4 + ph);
        els.forEach((r, k) => { const p = path[((step - k) % path.length + path.length) % path.length]; r.setAttribute("x", p[0]); r.setAttribute("y", p[1]); }); }},
    pong: {build: g => [add(g, "line", {x1: 8, y1: 14, x2: 8, y2: 26}), add(g, "line", {x1: 32, y1: 14, x2: 32, y2: 26}), add(g, "circle", {r: 2.5, class: "fill"})],
      update: (els, t, ph) => { const x = 12 + 16 * (.5 + .5 * Math.sin(t * 4 + ph)), y = 20 + 7 * Math.sin(t * 2.6 + ph); els[2].setAttribute("cx", x); els[2].setAttribute("cy", y);
        els[0].setAttribute("y1", y - 6); els[0].setAttribute("y2", y + 6); const r = 20 + 3 * Math.sin(t * 2 + ph); els[1].setAttribute("y1", r - 6); els[1].setAttribute("y2", r + 6); }},
    invaders: {build: g => [0, 1, 2].map(() => add(g, "rect", {y: 9, width: 6, height: 5, rx: 1, class: "fill"})).concat([add(g, "rect", {x: 17, y: 28, width: 6, height: 5, rx: 1, class: "fill"}), add(g, "line", {x1: 20, x2: 20, y1: 22, y2: 26})]),
      update: (els, t, ph) => { const dx = 4 * Math.sin(t * 2 + ph); [0, 1, 2].forEach(k => els[k].setAttribute("x", 9 + k * 9 + dx)); const by = 26 - ((t * 30 + ph * 5) % 14); els[4].setAttribute("y1", by); els[4].setAttribute("y2", by + 4); }},
    blocks: {build: g => [add(g, "rect", {x: 8, y: 26, width: 24, height: 6, class: "fill"}), add(g, "rect", {x: 8, y: 20, width: 12, height: 6, class: "fill"}), add(g, "rect", {x: 20, width: 12, height: 6, class: "fill"})],
      update: (els, t, ph) => { els[2].setAttribute("y", 4 + ((t * 12 + ph * 3) % 16)); }},
    maze: {build: g => [add(g, "path", {d: "M8 10h24v8H16v8h16"}), add(g, "circle", {r: 3, class: "fill"})],
      update: (els, t, ph) => { const path = [[8, 10], [32, 10], [32, 18], [16, 18], [16, 26], [32, 26]]; const u = ((t * .5 + ph) % 1) * (path.length - 1); const k = Math.floor(u), f = u - k;
        const a = path[k], b = path[Math.min(k + 1, path.length - 1)]; els[1].setAttribute("cx", a[0] + (b[0] - a[0]) * f); els[1].setAttribute("cy", a[1] + (b[1] - a[1]) * f); }},
    flappy: {build: g => [add(g, "rect", {y: 6, width: 6, height: 10, rx: 1}), add(g, "rect", {y: 24, width: 6, height: 10, rx: 1}), add(g, "circle", {r: 3.5, cx: 14, class: "fill"})],
      update: (els, t, ph) => { const px = 34 - ((t * 14 + ph * 7) % 30); els[0].setAttribute("x", px); els[1].setAttribute("x", px); els[2].setAttribute("cy", 20 + 6 * Math.sin(t * 5 + ph)); }},
    racer: {build: g => [add(g, "line", {x1: 12, y1: 6, x2: 12, y2: 34, "stroke-dasharray": "4 4"}), add(g, "line", {x1: 28, y1: 6, x2: 28, y2: 34, "stroke-dasharray": "4 4"}), add(g, "rect", {width: 7, height: 11, rx: 2, y: 22, class: "fill"})],
      update: (els, t, ph) => { const off = -((t * 20 + ph * 4) % 8); els[0].setAttribute("stroke-dashoffset", off); els[1].setAttribute("stroke-dashoffset", off); els[2].setAttribute("x", 16.5 + 4 * Math.sin(t * 2.5 + ph)); }},
    cards: {build: g => [add(g, "rect", {x: 7, y: 11, width: 12, height: 18, rx: 2}), add(g, "rect", {x: 21, y: 11, width: 12, height: 18, rx: 2, class: "fill"})],
      update: (els, t, ph) => { els[1].setAttribute("transform", `translate(27 20) scale(${Math.cos(t * 3 + ph)} 1) translate(-27 -20)`); }},
    dice: {build: g => [add(g, "rect", {x: 10, y: 10, width: 20, height: 20, rx: 4})].concat(Array.from({length: 6}, () => add(g, "circle", {r: 2, class: "fill"}))),
      update: (els, t, ph) => { const n = 1 + Math.floor((t * 1.5 + ph) % 6); const pips = [[20, 20], [14, 14], [26, 26], [26, 14], [14, 26], [14, 20], [26, 20]];
        const sets = {1: [0], 2: [1, 2], 3: [0, 1, 2], 4: [1, 2, 3, 4], 5: [0, 1, 2, 3, 4], 6: [1, 2, 3, 4, 5, 6]}[n];
        els.slice(1).forEach((c, k) => { const p = pips[sets[k] ?? 0]; c.setAttribute("cx", p[0]); c.setAttribute("cy", p[1]); c.style.opacity = k < sets.length ? "1" : "0"; }); }},
    jumper: {build: g => [add(g, "line", {x1: 8, y1: 31, x2: 32, y2: 31}), add(g, "line", {x1: 22, y1: 19, x2: 33, y2: 19}), add(g, "circle", {r: 4, cx: 14, class: "fill"})],
      update: (els, t, ph) => { els[2].setAttribute("cy", 27 - 14 * Math.abs(Math.sin(t * 3 + ph))); }},
    pigeon: {build: g => [add(g, "ellipse", {rx: 7, ry: 5, class: "fill"}), add(g, "circle", {r: 3.5, class: "fill"}), add(g, "path", {d: "M0 0l4 1.5-4 1.5z", class: "fill"}), add(g, "rect", {x: 24, y: 25, width: 10, height: 6, rx: 2, class: "fill", opacity: .55})],
      update: (els, t, ph) => { const x = 10 + 6 * (.5 + .5 * Math.sin(t * 1.5 + ph)), y = 26 - 3 * Math.abs(Math.sin(t * 6 + ph));
        els[0].setAttribute("cx", x); els[0].setAttribute("cy", y); els[1].setAttribute("cx", x + 6); els[1].setAttribute("cy", y - 5); els[2].setAttribute("transform", `translate(${x + 9} ${y - 6.5})`); }},
    mars: {build: g => [add(g, "line", {x1: 6, y1: 31, x2: 34, y2: 31}), add(g, "circle", {cx: 10, cy: 27, r: 3.5, class: "fill", opacity: .5}), add(g, "circle", {cx: 31, cy: 27, r: 3.5, class: "fill", opacity: .5}), add(g, "rect", {x: 15.5, width: 10, height: 5, rx: 1.5, class: "fill"})],
      update: (els, t, ph) => { const u = (t * .6 + ph) % 1, e = 1 - Math.pow(1 - Math.min(1, u / .7), 3); const y = 12 + 13 * e; els[3].setAttribute("y", y); els[3].setAttribute("transform", `rotate(${-20 * (1 - e)} 20 ${y + 2.5})`); }},
    sheet: {build: g => [add(g, "path", {d: "M8 10h24M8 17h24M8 24h24M8 31h24M15 10v21M24 10v21"}), add(g, "rect", {x: 15, y: 17, width: 9, height: 7, class: "fill", opacity: .55}), add(g, "rect", {width: 9, height: 7, rx: 1})],
      update: (els, t, ph) => { const cells = [[8, 10], [24, 10], [24, 24], [8, 24], [15, 24]]; const c = cells[Math.floor(t * 2 + ph) % cells.length]; els[2].setAttribute("x", c[0]); els[2].setAttribute("y", c[1]); }},
    meeting: {build: g => [add(g, "rect", {x: 9, y: 15, width: 18, height: 9, rx: 3}), add(g, "rect", {x: 31, y: 10, width: 4, height: 20, rx: 1}), add(g, "circle", {cx: 12, cy: 11, r: 2.2, class: "fill", opacity: .5}), add(g, "circle", {cx: 24, cy: 11, r: 2.2, class: "fill", opacity: .5}), add(g, "circle", {r: 3, class: "fill"})],
      update: (els, t, ph) => { const u = (t * .5 + ph) % 1, e = 1 - Math.pow(1 - Math.min(1, u / .8), 3); els[4].setAttribute("cx", 10 + 20 * e); els[4].setAttribute("cy", 31 - 4 * Math.sin(e * Math.PI)); }},
  };

  // ---- Lens scenes (320 × 200) -----------------------------------------------------------------
  const SCENES = {
    pigeon(svg, tone) {
      const g = add(svg, "g", {});
      add(g, "line", {x1: 0, y1: 170, x2: 320, y2: 170, stroke: "#d9dcd1", "stroke-width": 2});
      add(g, "rect", {x: 196, y: 108, width: 112, height: 62, rx: 8, fill: "#f0e6d2", stroke: "#d8c49c", "stroke-width": 2});
      add(g, "rect", {x: 196, y: 108, width: 112, height: 10, rx: 4, fill: "#d8c49c"});
      add(g, "rect", {x: 262, y: 100, width: 28, height: 14, rx: 2, fill: "#f5f1e6", stroke: "#8b9185", "stroke-width": 1.5});
      add(g, "circle", {cx: 276, cy: 86, r: 11, fill: "#e3d5bb", stroke: "#8b9185", "stroke-width": 1.5});
      add(g, "rect", {x: 264, y: 68, width: 24, height: 9, rx: 2, fill: "#f6f5ef", stroke: "#8b9185", "stroke-width": 1.5});
      const bread = add(g, "rect", {x: 212, y: 90, width: 40, height: 18, rx: 8, fill: "#c99a5b"});
      add(g, "path", {d: "M220 96q6 -4 12 0t12 0", stroke: "#8a6230", "stroke-width": 1.5, fill: "none", opacity: .6});
      const alert = text(g, {x: 294, y: 62, "font-size": 22, "font-weight": 700, fill: "#a87f32", "text-anchor": "middle"}, "!");
      const makePigeon = fill => { const p = add(g, "g", {}); add(p, "line", {x1: -6, y1: 10, x2: -8, y2: 18, stroke: "#c98f3b", "stroke-width": 2.5}); add(p, "line", {x1: 4, y1: 10, x2: 4, y2: 18, stroke: "#c98f3b", "stroke-width": 2.5});
        add(p, "ellipse", {rx: 17, ry: 11, fill}); add(p, "path", {d: "M-12 -2q10 6 16 -1", stroke: "#f6f5ef", "stroke-width": 2, fill: "none", opacity: .5}); add(p, "circle", {cx: 14, cy: -10, r: 8, fill});
        add(p, "path", {d: "M21 -11l8 2 -8 3z", fill: "#e2a53d"}); add(p, "circle", {cx: 16, cy: -12, r: 1.6, fill: "#f6f5ef"}); return p; };
      const p1 = makePigeon("#8d8f99"), p2 = makePigeon(tone);
      const score = text(g, {x: 14, y: 26, "font-size": 14, fill: "#8b9185", "font-weight": 600, "letter-spacing": 1}, "BREAD ×3");
      const players = text(g, {x: 306, y: 26, "font-size": 14, fill: tone, "font-weight": 600, "text-anchor": "end", "letter-spacing": 1}, "2 PLAYERS");
      return (t, variant) => {
        const u = .5 - .5 * Math.cos(t * 1.3), px = 44 + 112 * u, hop = -Math.abs(Math.sin(t * 7)) * 9;
        p1.setAttribute("transform", `translate(${px} ${152 + hop})`);
        const u2 = .5 - .5 * Math.cos(t * 1.3 + 2.1), px2 = 30 + 112 * u2, hop2 = -Math.abs(Math.sin(t * 7 + 1)) * 9;
        p2.setAttribute("transform", `translate(${px2} ${158 + hop2}) scale(.92)`); p2.style.opacity = variant === "multiplayer" ? "1" : "0"; players.style.opacity = variant === "multiplayer" ? "1" : "0";
        const near = clamp((Math.max(px, variant === "multiplayer" ? px2 : 0) - 136) / 16); alert.style.opacity = String(near);
        bread.setAttribute("transform", `rotate(${near * 7 * Math.sin(t * 22)} 232 99)`);
        score.textContent = "BREAD ×" + (3 + Math.floor(t / 4.83) % 3);
      };
    },
    mars(svg, tone) {
      const g = add(svg, "g", {});
      [[30, 28], [92, 16], [150, 36], [230, 20], [290, 40]].forEach(([x, y]) => add(g, "circle", {cx: x, cy: y, r: 1.7, fill: "#c9c3b6"}));
      add(g, "circle", {cx: 262, cy: 52, r: 11, fill: "#efe6d6"});
      add(g, "path", {d: "M0 134 L60 104 L112 128 L180 96 L240 126 L320 110 L320 134 Z", fill: "#e9d5c3"});
      add(g, "rect", {x: 0, y: 130, width: 320, height: 70, fill: "#e6c8b3"});
      add(g, "line", {x1: 0, y1: 130, x2: 320, y2: 130, stroke: "#d3ae95", "stroke-width": 2});
      const zone = add(g, "rect", {x: 98, y: 102, width: 124, height: 34, rx: 6, fill: "none", stroke: "#8b9185", "stroke-width": 1.5, "stroke-dasharray": "5 5"});
      const rover = (x, fill) => { const r = add(g, "g", {}); add(r, "rect", {x: 0, y: 0, width: 52, height: 22, rx: 6, fill}); add(r, "rect", {x: 10, y: -8, width: 20, height: 10, rx: 3, fill, opacity: .8});
        add(r, "circle", {cx: 12, cy: 24, r: 6, fill: "#5d5b57"}); add(r, "circle", {cx: 40, cy: 24, r: 6, fill: "#5d5b57"}); r.setAttribute("transform", `translate(${x} 106)`); return r; };
      const parkedA = rover(38, "#aeaca4"), parkedB = rover(232, "#aeaca4"), player = rover(150, tone);
      add(player, "line", {x1: 44, y1: -8, x2: 50, y2: -18, stroke: tone, "stroke-width": 2});
      const dust = [0, 1, 2].map(k => add(g, "circle", {cx: 150 + k * 22, cy: 134, r: 6 + k * 2, fill: "#d9b9a2"}));
      const parked = text(g, {x: 160, y: 164, "font-size": 15, "font-weight": 700, fill: tone, "text-anchor": "middle", "letter-spacing": 1.5}, "PARKED");
      const gravity = text(g, {x: 306, y: 26, "font-size": 14, fill: tone, "font-weight": 600, "text-anchor": "end", "letter-spacing": 1}, "0.16 g");
      return (t, variant) => {
        const moon = variant === "moon";
        parkedA.setAttribute("transform", `translate(${moon ? 12 : 38} 106)`); parkedB.setAttribute("transform", `translate(${moon ? 258 : 232} 106)`);
        zone.setAttribute("x", moon ? 72 : 98); zone.setAttribute("width", moon ? 176 : 124); zone.style.opacity = moon ? "1" : "0"; gravity.style.opacity = moon ? "1" : "0";
        const u = (t / 3.2) % 1, e = 1 - Math.pow(1 - Math.min(1, u / .72), 3);
        const float = moon ? -10 - 7 * Math.sin(t * 2.2) : 0;
        const y = mix(40, 106, e) + float * e, x = mix(176, 134, e), rot = -22 * (1 - e);
        player.setAttribute("transform", `translate(${x} ${y}) rotate(${rot} 26 11)`);
        const puff = u > .7 && u < .95 ? Math.sin((u - .7) / .25 * Math.PI) : 0;
        dust.forEach((d, k) => { d.style.opacity = String(puff * (moon ? .25 : .7)); d.setAttribute("r", 5 + k * 2 + puff * 6); d.setAttribute("cx", 132 + k * 28 + puff * 10); });
        parked.style.opacity = u > .76 ? String(clamp((u - .76) / .1)) : "0";
        parked.textContent = moon ? "PARKED · MOON" : "PARKED";
      };
    },
    sheet(svg, tone) {
      const g = add(svg, "g", {});
      const inner = add(g, "g", {});
      add(inner, "rect", {x: 0, y: 0, width: 320, height: 22, fill: "#ebebe4"});
      "ABCDEF".split("").forEach((c, k) => text(inner, {x: 44 + k * 48, y: 16, "font-size": 12, fill: "#8b9185", "text-anchor": "middle", "font-weight": 600}, c));
      [1, 2, 3, 4, 5].forEach(r => text(inner, {x: 10, y: 46 + (r - 1) * 34, "font-size": 12, fill: "#8b9185", "text-anchor": "middle"}, String(r)));
      for (let k = 0; k <= 6; k++) add(inner, "line", {x1: 20 + k * 48, y1: 22, x2: 20 + k * 48, y2: 200, stroke: "#dfe2d8", "stroke-width": 1});
      for (let r = 0; r <= 5; r++) add(inner, "line", {x1: 20, y1: 22 + r * 34, x2: 320, y2: 22 + r * 34, stroke: "#dfe2d8", "stroke-width": 1});
      add(inner, "rect", {x: 116, y: 56, width: 96, height: 68, rx: 4, fill: "#efebf4", stroke: "#847099", "stroke-width": 2});
      text(inner, {x: 164, y: 96, "font-size": 14, fill: "#847099", "text-anchor": "middle", "font-family": "ui-monospace, Menlo, monospace", "font-weight": 600}, "=SUM(ALL)");
      add(inner, "rect", {x: 120, y: 44, width: 88, height: 6, rx: 3, fill: "#dfe2d8"});
      const hp = add(inner, "rect", {x: 120, y: 44, width: 88, height: 6, rx: 3, fill: "#a87f32"});
      const cursor = add(inner, "rect", {width: 48, height: 34, rx: 3, fill: "none", stroke: tone, "stroke-width": 2.5});
      const dmg = text(inner, {x: 232, y: 70, "font-size": 15, "font-weight": 700, fill: "#a87f32"}, "-12");
      const phone = add(g, "g", {});
      add(phone, "rect", {x: 6, y: 3, width: 308, height: 194, rx: 22, fill: "none", stroke: "#253d35", "stroke-width": 3});
      add(phone, "rect", {x: 136, y: 186, width: 48, height: 4, rx: 2, fill: "#253d35"});
      [[34, 160], [286, 160]].forEach(([x, y]) => { add(phone, "circle", {cx: x, cy: y, r: 15, fill: tone, opacity: .22}); add(phone, "circle", {cx: x, cy: y, r: 15, fill: "none", stroke: tone, "stroke-width": 2}); });
      text(phone, {x: 34, y: 165, "font-size": 13, fill: tone, "text-anchor": "middle", "font-weight": 700}, "◀");
      text(phone, {x: 286, y: 165, "font-size": 13, fill: tone, "text-anchor": "middle", "font-weight": 700}, "▶");
      const key = add(g, "g", {});
      add(key, "rect", {x: 258, y: 28, width: 38, height: 32, rx: 7, fill: "#fdfdfa", stroke: "#253d35", "stroke-width": 2});
      text(key, {x: 277, y: 50, "font-size": 18, "font-weight": 700, fill: "#253d35", "text-anchor": "middle"}, "R");
      return (t, variant, extra) => {
        const cells = [[20, 22], [68, 22], [68, 124], [20, 124], [116, 158], [212, 124], [260, 56], [212, 22]]; const c = cells[Math.floor(t * 1.7) % cells.length];
        cursor.setAttribute("x", c[0]); cursor.setAttribute("y", c[1]);
        hp.setAttribute("width", 88 * (1 - ((t * .22) % 1)));
        const d = (t * 1.1) % 1; dmg.setAttribute("y", 72 - 22 * d); dmg.style.opacity = String(1 - d);
        const isPhone = variant === "phone"; phone.style.opacity = isPhone ? "1" : "0";
        inner.setAttribute("transform", isPhone ? "translate(26 14) scale(.84 .78)" : "");
        key.style.opacity = extra?.restart ? "1" : "0";
      };
    },
    meeting(svg, tone) {
      const g = add(svg, "g", {});
      add(g, "rect", {x: 62, y: 68, width: 176, height: 64, rx: 20, fill: "#e9eae2", stroke: "#c9cdbf", "stroke-width": 2});
      [[90, 52], [150, 52], [210, 52], [90, 148], [150, 148], [210, 148]].forEach(([x, y]) => add(g, "circle", {cx: x, cy: y, r: 9, fill: "#b8bfb2"}));
      add(g, "circle", {cx: 40, cy: 100, r: 10, fill: "#c7a86d"});
      const bubble = add(g, "g", {}); add(bubble, "rect", {x: 10, y: 58, width: 92, height: 24, rx: 8, fill: "#fdfdfa", stroke: "#d3d8cc", "stroke-width": 1.5});
      text(bubble, {x: 56, y: 75, "font-size": 12, fill: "#65755e", "text-anchor": "middle"}, "Any questions?");
      add(g, "rect", {x: 270, y: 34, width: 44, height: 15, rx: 3, fill: "#3f7062"}); text(g, {x: 292, y: 45, "font-size": 10, "font-weight": 700, fill: "#f6f5ef", "text-anchor": "middle", "letter-spacing": 1}, "EXIT");
      const door = add(g, "rect", {x: 280, y: 56, width: 26, height: 92, rx: 3, fill: "#d8c6a3", stroke: "#b9a06f", "stroke-width": 2});
      add(g, "circle", {cx: 300, cy: 104, r: 2.5, fill: "#8a6230"});
      const who = add(g, "g", {}); add(who, "rect", {x: -7, y: 0, width: 14, height: 18, rx: 6, fill: tone}); add(who, "circle", {cx: 0, cy: -8, r: 8, fill: tone});
      const escaped = text(g, {x: 160, y: 186, "font-size": 15, "font-weight": 700, fill: tone, "text-anchor": "middle", "letter-spacing": 1.5}, "ESCAPED");
      return t => {
        const u = (t / 3.6) % 1, e = Math.min(1, u / .82), s = 1 - Math.pow(1 - e, 2);
        const x = mix(52, 266, s), y = mix(158, 112, s) + (s < .98 ? -3 * Math.abs(Math.sin(t * 9)) : 0);
        who.setAttribute("transform", `translate(${x} ${y})`);
        const open = clamp((e - .82) / .12); door.setAttribute("transform", `translate(280 0) scale(${1 - .7 * open} 1) translate(-280 0)`);
        escaped.style.opacity = u > .86 ? String(clamp((u - .86) / .08)) : "0";
        bubble.style.opacity = String(.35 + .65 * (Math.floor(t / 1.8) % 2));
      };
    },
  };

  // ---- DOM --------------------------------------------------------------------------------------
  const field = $("field"), packetsG = $("packets");
  const tileEls = tiles.map(tile => {
    const p = PROVIDERS[tile.provider];
    const el = document.createElement("div"); el.className = "tile"; el.style.setProperty("--tone", p.tone); el.style.setProperty("--tint", p.tint);
    el.style.left = (cx(tile.i) - TILE / 2) + "px"; el.style.top = (cy(tile.i) - TILE / 2) + "px";
    const svg = document.createElementNS(NS, "svg"); svg.setAttribute("viewBox", "0 0 40 40"); el.append(svg);
    const glyph = GLYPHS[tile.glyph]; const els = glyph.build(svg);
    const dot = document.createElement("i"); dot.className = "dot"; el.append(dot);
    field.append(el); return {el, svg, els, dot, update: glyph.update};
  });
  const ripple = add(packetsG, "circle", {cx: HUB.x, cy: HUB.y, r: 24, fill: "none", stroke: "#587052", "stroke-width": 2});
  const burstDots = tiles.map(tile => add(packetsG, "circle", {r: 4.5, fill: PROVIDERS[tile.provider].tone}));
  const wakeDots = WAKES.map(w => add(packetsG, "circle", {r: 6, fill: PROVIDERS[tiles[w.tile].provider].tone}));
  const reportDot = add(packetsG, "circle", {r: 6, fill: "#3f7062"});
  const legendEls = PROVIDERS.map(p => { const chip = document.createElement("span"); chip.className = "chip"; chip.style.setProperty("--tone", p.tone); chip.innerHTML = `${p.name}<b></b>`; $("legend").append(chip); return chip; });
  const lensScene = $("lens-scene"), lensRenderers = {};
  for (const key of ["pigeon", "mars", "sheet"]) { const holder = add(lensScene, "g", {}); lensRenderers[key] = {holder, update: SCENES[key](holder, PROVIDERS[tiles[Object.keys(HEROES).find(i => HEROES[i].glyph === key)].provider].tone)}; }
  const duoA = $("duo-a"), duoB = $("duo-b");
  const duoRender = {a: SCENES.sheet(duoA.querySelector(".duo-scene"), PROVIDERS[3].tone), b: SCENES.meeting(duoB.querySelector(".duo-scene"), PROVIDERS[0].tone)};
  for (const [col, i] of [[duoA, duo.a], [duoB, duo.b]]) { col.querySelector("h2").textContent = tiles[i].name; const chip = col.querySelector(".chip"); chip.textContent = PROVIDERS[tiles[i].provider].name; chip.style.setProperty("--tone", PROVIDERS[tiles[i].provider].tone); col.style.setProperty("--tone", PROVIDERS[tiles[i].provider].tone); }

  const grooves = [[4.0, 30.8], [36.2, T.outro]];
  const lensCenter = {x: 540, y: 704};

  function render(time) {
    const t = Math.max(0, Math.min(T.duration - .001, time));
    const outro = progress(t, T.outro, .7);
    show($("brand"), 1 - outro); show($("story"), 1 - outro); show($("endcard"), outro);
    $("endcard").style.transform = `translateY(${(1 - outro) * 18}px)`;

    const heading = headings.find(h => t >= h.at && t < h.end);
    const l1 = $("headline").querySelector(".l1"), l2 = $("headline").querySelector(".l2");
    l1.textContent = heading?.l1 || ""; l2.textContent = heading?.l2 || "";
    show(l1, heading ? fade(t, heading.at, heading.end, .35) : 0); show(l2, heading ? fade(t, heading.at + .12, heading.end, .35) : 0);
    l1.style.transform = `translateY(${heading ? (1 - progress(t, heading.at, .5)) * 12 : 0}px)`; l2.style.transform = `translateY(${heading ? (1 - progress(t, heading.at + .12, .5)) * 12 : 0}px)`;

    const request = requests.find(r => t >= r.at && t < r.end);
    let requestText = "";
    if (request) { const typed = clamp((t - request.at) / (request.typed - request.at)); requestText = request.text.slice(0, Math.floor(typed * request.text.length)); if (typed < 1 && Math.floor(t * 3) % 2 === 0) requestText += "|"; }
    $("request-text").textContent = requestText;
    show($("request"), request ? fade(t, request.at, request.end, .3) : 0);
    const caption = captions.find(c => t >= c.at && t < c.end);
    $("caption").textContent = caption?.text || "";
    show($("caption"), caption ? fade(t, caption.at, caption.end, .35) : 0);
    const reportIn = fade(t, T.report.at, T.report.end, .45);
    show($("report"), reportIn); $("report").style.transform = `translateY(${(1 - progress(t, T.report.at, .6)) * 14}px)`;

    show($("hub"), progress(t, .9, .6));
    const sends = requests.map(r => r.send).concat([T.reportPacket.end]);
    const pulse = Math.max(...sends.map(s => fade(t, s - .05, s + .6, .2)));
    const groove = grooves.find(([a, b]) => t >= a && t < b);
    const breathe = groove ? .022 * Math.exp(-6 * ((t - groove[0]) % .5)) : 0;
    $("hub").querySelector(".hub-mark").style.transform = `scale(${1 + pulse * .08 + breathe})`;
    const rippleAt = sends.find(s => t >= s && t < s + .6);
    show(ripple, rippleAt ? (1 - (t - rippleAt) / .6) * .8 : 0); ripple.setAttribute("r", rippleAt ? 24 + 46 * ease((t - rippleAt) / .6) : 24);

    const landed = tiles.filter(x => t >= landAt(x.i)).length, playable = tiles.filter(x => t >= playableAt(x.i)).length, closed = tiles.filter(x => t >= closedAt(x.i)).length;
    const awake = WAKES.filter(w => t >= w.at + WAKE_FLIGHT).length;
    let counter = "";
    if (t >= T.burst && t < 12.0) counter = `${landed} agents`;
    else if (t >= 12.0 && t < 30.9) counter = `${playable} / 100 playable`;
    else if (t >= 31.0 && t < 36.2) counter = closed >= N ? "All 100 closed" : `${closed} closed`;
    else if (t >= 36.2) counter = awake ? `${awake} awake · ${N - awake} closed` : "All 100 closed";
    $("counter").textContent = counter; show($("counter"), counter ? progress(t, t >= 36.2 ? 36.2 : t >= 31 ? 31 : t >= 12 ? 12 : T.burst, .4) : 0);
    $("counter").style.color = t >= T.allBuilt && t < 30.9 ? "#3f7062" : "#65755e";
    const legendOn = (t >= T.legend && t < 26.4) || (t >= 31.0 && t < T.outro);
    show($("legend"), legendOn ? progress(t, t >= 31 ? 31.0 : T.legend, .5) : 0);
    PROVIDERS.forEach((p, k) => legendEls[k].querySelector("b").textContent = String(tiles.filter(x => x.provider === k && t >= landAt(x.i)).length));
    const bannerOn = fade(t, T.live, 30.9, .35);
    show($("banner"), bannerOn); $("banner-text").textContent = "Arcade live · 100 games behind one launcher"; $("banner").style.transform = `translateY(${(1 - progress(t, T.live, .45)) * 10}px)`;

    const lens = lenses.find(l => t >= l.at && t < l.end + .35);
    const duoOn = t >= duo.at && t < duo.end + .35;
    const focus = new Set(lens ? [lens.tile] : duoOn ? [duo.a, duo.b] : []);
    const dimming = lens ? fade(t, lens.at, lens.end + .35, .3) : duoOn ? fade(t, duo.at, duo.end + .35, .3) : 0;
    const state = {time: t, headline: heading ? `${heading.l1} ${heading.l2}` : "", request: request ? request.text : null, caption: caption?.text || null, counter,
      landed, playable, closed, awake, lens: lens ? {name: tiles[lens.tile].name, status: null} : null, duo: duoOn, pills: [], banner: bannerOn > .5, report: reportIn > .5, endcard: outro >= 1};

    tiles.forEach((tile, i) => {
      const {el, els, dot, update} = tileEls[i];
      const land = landAt(i), intro = t >= land ? back((t - land) / .4) : 0;
      if (t < land) { show(el, 0); return; }
      const built = t >= playableAt(i), isClosed = t >= closedAt(i) && t < wakeAt(i), woke = t >= wakeAt(i);
      const p = PROVIDERS[tile.provider];
      const closing = t >= closedAt(i) ? clamp((t - closedAt(i)) / .35) : 0;
      const waking = woke ? progress(t, wakeAt(i), .5) : 0;
      const grey = isClosed ? closing : woke ? 1 - waking : 0;
      el.style.background = grey > 0 ? `color-mix(in srgb, ${p.tint} ${(1 - grey) * 100}%, #ecebe3)` : p.tint;
      el.style.borderColor = grey > 0 ? `color-mix(in srgb, ${p.tone} ${(1 - grey) * 100}%, #d3d6cc)` : p.tone;
      const glow = woke ? (1 - progress(t, wakeAt(i), 1.4)) * .35 + .18 : focus.has(i) && !duoOn ? .25 * dimming : focus.has(i) ? .3 * dimming : 0;
      el.style.boxShadow = glow > 0.01 ? `0 0 0 ${4 + 3 * glow}px ${p.tone}${Math.round(glow * 255).toString(16).padStart(2, "0")}` : "none";
      const dimmed = dimming * (focus.has(i) ? 0 : .55);
      show(el, Math.min(1, intro) * (1 - dimmed));
      const wobble = woke ? 1 + .12 * Math.sin(Math.PI * clamp((t - wakeAt(i)) / .45)) : 1;
      el.style.transform = `scale(${intro * wobble})`;
      const building = !built;
      show(dot, building ? 1 : 0); dot.style.transform = building ? `translateY(${-4 * Math.abs(Math.sin(t * 5 + tile.phase))}px)` : "";
      const svgOpacity = built ? (1 - .65 * grey) : 0;
      tileEls[i].svg.style.opacity = String(svgOpacity);
      if (built) update(els, isClosed ? closedAt(i) : t, tile.phase);
    });

    burstDots.forEach((d, i) => { const f = (t - depart(i)) / T.flight; if (f < 0 || f > 1) { show(d, 0); return; } const s = smooth(f);
      d.setAttribute("cx", mix(HUB.x, cx(i), s)); d.setAttribute("cy", mix(HUB.y, cy(i), s)); d.setAttribute("r", 5.5 - 2 * s); show(d, Math.sin(Math.PI * f) * .95); });
    wakeDots.forEach((d, k) => { const w = WAKES[k], f = (t - w.at) / WAKE_FLIGHT; if (f < 0 || f > 1) { show(d, 0); return; } const s = smooth(f);
      d.setAttribute("cx", mix(HUB.x, cx(w.tile), s)); d.setAttribute("cy", mix(HUB.y, cy(w.tile), s)); d.setAttribute("r", 7 + 3 * Math.sin(Math.PI * f)); show(d, Math.min(1, 2.2 * Math.sin(Math.PI * f))); });
    { const f = (t - T.reportPacket.at) / (T.reportPacket.end - T.reportPacket.at); if (f < 0 || f > 1) show(reportDot, 0); else { const s = smooth(f); reportDot.setAttribute("cx", 540); reportDot.setAttribute("cy", mix(966, HUB.y + 24, s)); show(reportDot, Math.sin(Math.PI * f)); } }

    const lensEl = $("lens");
    if (lens) {
      const tile = tiles[lens.tile], p = PROVIDERS[tile.provider];
      const opening = progress(t, lens.at, .45), closing = t >= lens.end ? progress(t, lens.end, .35) : 0, k = back(opening) * (1 - closing);
      const sc = mix(.12, 1, k), dx = (cx(lens.tile) - lensCenter.x) * (1 - k), dy = (cy(lens.tile) - lensCenter.y) * (1 - k);
      lensEl.style.transform = `translate(${dx}px, ${dy}px) scale(${Math.max(.01, sc)})`;
      show(lensEl, Math.min(1, opening * 2) * (1 - closing)); lensEl.style.setProperty("--tone", p.tone);
      $("lens-name").textContent = tile.name; const chip = $("lens-chip"); chip.textContent = p.name; chip.style.setProperty("--tone", p.tone);
      const done = t >= lens.done, statusEl = $("lens-status");
      const directDone = lens.direct && t >= lens.directDone;
      statusEl.textContent = directDone ? lens.directFinished : done ? lens.finished : lens.working; statusEl.className = "lens-status" + (done ? " done" : "");
      const direct = $("lens-direct"); direct.innerHTML = lens.direct ? `You, directly:<br><em>“${lens.directText}”</em>` : ""; show(direct, lens.direct ? fade(t, lens.direct, lens.end + .35, .3) : 0);
      const arrival = $("lens-arrival"); arrival.textContent = lens.reopened ? "Reopened · same context" : "Its own conversation"; show(arrival, fade(t, lens.at + .35, lens.end, .3));
      for (const [key, r] of Object.entries(lensRenderers)) { const on = key === tile.glyph; r.holder.style.opacity = on ? "1" : "0"; if (on) r.update(t, done ? lens.variant : null, {restart: directDone}); }
      state.lens.status = statusEl.textContent;
    } else show(lensEl, 0);

    const duoEl = $("duo");
    if (duoOn) {
      const opening = progress(t, duo.at, .45), closing = t >= duo.end ? progress(t, duo.end, .35) : 0, k = back(opening) * (1 - closing);
      duoEl.style.transform = `scale(${mix(.3, 1, k)})`; show(duoEl, Math.min(1, opening * 2) * (1 - closing));
      const contested = t >= duo.conflict && t < duo.resolved, resolved = t >= duo.resolved;
      const sa = duoA.querySelector(".lens-status"), sb = duoB.querySelector(".lens-status");
      sa.textContent = resolved ? "Owns launcher.js ✓" : contested ? "Editing launcher.js…" : "Playable ✓"; sa.className = "lens-status " + (resolved ? "done" : contested ? "conflict" : "done");
      sb.textContent = resolved ? "Uses its API ✓" : contested ? "Editing launcher.js…" : "Playable ✓"; sb.className = "lens-status " + (resolved ? "done" : contested ? "conflict" : "done");
      const token = $("file-token"); token.className = "file-token" + (resolved ? " owned" : ""); token.style.opacity = t >= duo.conflict ? "1" : ".35";
      duo.pills.forEach((pill, k) => { const el = $("duo-pill-" + k); el.textContent = pill.text; const on = fade(t, pill.at, pill.end, .25); show(el, on);
        el.style.left = k === 0 ? "0px" : ""; el.style.right = k === 1 ? "0px" : ""; el.style.transform = `translateX(${(1 - progress(t, pill.at, .4)) * (k === 0 ? -14 : 14)}px)`; if (on > .5) state.pills.push(pill.text); });
      duoRender.a(t, null, {}); duoRender.b(t);
    } else show(duoEl, 0);
    window.__sceneState = state;
  }

  function cues() {
    const pent = [0, 2, 4, 7, 9], midi = n => 440 * Math.pow(2, (n - 69) / 12);
    const list = [{t: 2.0, kind: "riser", length: 2.0}, {t: 24.6, kind: "riser", length: 2.0}, {t: 34.2, kind: "riser", length: 2.0},
      {t: T.burst, kind: "impact", gain: .9}, {t: T.live, kind: "impact", gain: .8}, {t: 36.2, kind: "impact", gain: .9}, {t: T.outro, kind: "impact", gain: 1.0},
      {t: T.allBuilt, kind: "accent"}, {t: duo.resolved, kind: "pop", pitch: 1046.5, gain: .14}, {t: T.reportPacket.end, kind: "pop", pitch: 783.99, gain: .14},
      {t: 4.0, kind: "mark", name: "groove"}, {t: 30.8, kind: "mark", name: "break"}, {t: 36.2, kind: "mark", name: "groove"}, {t: T.outro, kind: "mark", name: "outro"}];
    requests.forEach(r => list.push({t: r.send, kind: "click"}));
    tiles.forEach(tile => { const k = burstRank[tile.i]; if (k % 3 === 0) { const n = k / 3; list.push({t: landAt(tile.i), kind: "pop", pitch: midi(60 + pent[n % 5] + 12 * (Math.floor(n / 5) % 3)), gain: .07}); }
      const b = buildOrder[tile.i]; if (b % 4 === 0) list.push({t: playableAt(tile.i), kind: "tick", pitch: midi(72 + pent[(b / 4) % 5]), gain: .05}); });
    WAKES.forEach((w, k) => list.push({t: w.at + WAKE_FLIGHT, kind: "pop", pitch: [659.25, 783.99, 1046.5][k], gain: .2}));
    lenses.forEach(l => { list.push({t: l.at, kind: "whoosh", length: .45}); list.push({t: l.end, kind: "whoosh", length: .35}); });
    list.push({t: duo.at, kind: "whoosh", length: .5}, {t: duo.end, kind: "whoosh", length: .35});
    return list.sort((a, b) => a.t - b.t);
  }
  window.__render = t => { render(t); return true; };
  window.__duration = () => T.duration;
  window.__cues = cues;
  window.__ready = Promise.all([document.fonts.ready, ...Array.from(document.images, im => im.decode())]).then(() => { render(0); return true; });
})();
