/* Atlas geometry in world pixels. Population shapes phase contours; lineage initializes angles, then users own
 * them. Free pins retain a phase home and survive contour changes. */
"use strict";
const AtlasLayout = (() => {
  const TAU = Math.PI * 2;
  const order = ["needs", "starting", "discussion", "investigation", "planning", "orchestrating", "implementation", "validation", "deployment", "finished"];
  const labels = {needs: "Needs you", starting: "Getting started", discussion: "Discussing", investigation: "Investigating", planning: "Planning", orchestrating: "Orchestrating", implementation: "Implementing", validation: "Validating", deployment: "Deploying", finished: "Finished"};
  const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
  const wrap = a => ((a + Math.PI) % TAU + TAU) % TAU - Math.PI;
  const hash = text => { let h = 2166136261; for (const ch of text) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); } return (h >>> 0) / 4294967296; };
  const sorted = list => [...list].sort((a, b) => hash(a.id) - hash(b.id) || a.id.localeCompare(b.id));
  const bandFor = (a, needsUser) => {
    if (needsUser(a)) return "needs";
    if (a.status === "done") return "finished";
    const phase = a.atlasPhase ?? a.phase;
    return order.includes(phase) && phase !== "needs" ? phase : "starting";
  };
  const point = s => ({x: Math.cos(s.angle) * s.radius, y: Math.sin(s.angle) * s.radius});
  function metrics(counts) {
    let cursor = 0;
    const last = Math.max(0, ...order.map((b, i) => counts[b] ? i : 0));
    const bands = order.slice(0, last + 1).map(name => {
      const count = counts[name] || 0;
      // Active work gets breathing room; completed work never buys more territory.
      const width = !count ? (name === "needs" ? 10 : 6) : name === "finished" ? 18
        : Math.max(name === "needs" ? 32 : 52, Math.min(96, 24 * Math.sqrt(count)));
      const inner = cursor, outer = cursor += width, inset = Math.min(10, width / 4);
      return {name, count, inner, outer, lo: inner + inset, hi: outer - inset};
    });
    return {bands, rim: cursor + 20};
  }
  const completedOpacity = ageMs => Math.max(0.22, 0.22 + 0.78 * Math.exp(-Math.max(0, ageMs) / (2 * 86400000)));
  // Broad cosine lobes and low-frequency noise keep the outline star-shaped.
  function radiusAt(frame, angle, radius = frame.rim) {
    const seed = hash(frame.domain || "");
    const noise = .055 * Math.sin(3 * angle + seed * TAU) + .035 * Math.cos(5 * angle - seed * 13);
    const bumps = (frame.families || []).reduce((sum, f) => {
      const distance = Math.abs(wrap(angle - f.angle)), halfWidth = Math.PI / 3;
      return sum + (distance < halfWidth ? .3 * Math.sqrt((f.shapeCount ?? f.count) / Math.max(1, frame.shapePopulation ?? frame.population)) * (1 + Math.cos(Math.PI * distance / halfWidth)) / 2 : 0);
    }, 0);
    const t = clamp((radius / frame.rim - .3) / .7, 0, 1), taper = t * t * (3 - 2 * t);
    return radius * (1 + taper * (noise + Math.min(.3, bumps)));
  }
  // The tapered contours are nested but no longer proportional: invert by bisection.
  function phaseRadius(frame, angle, distance) {
    let lo = 0, hi = frame.rim;
    for (let i = 0; i < 48; i++) {
      const mid = (lo + hi) / 2;
      if (radiusAt(frame, angle, mid) < distance) lo = mid; else hi = mid;
    }
    return (lo + hi) / 2;
  }
  function contour(frame, radius = frame.rim, start = 0, end = TAU) {
    const steps = Math.max(2, Math.ceil(128 * (end - start) / TAU));
    return Array.from({length: steps + 1}, (_, i) => {
      const angle = start + (end - start) * i / steps, r = radiusAt(frame, angle, radius);
      return {x: Math.cos(angle) * r, y: Math.sin(angle) * r};
    });
  }
  function bounds(frame) {
    const points = contour(frame);
    return {left: Math.min(...points.map(p => p.x)), right: Math.max(...points.map(p => p.x)), top: Math.min(...points.map(p => p.y)), bottom: Math.max(...points.map(p => p.y))};
  }
  // Run only for new territories. Saved positions are fixed obstacles, never relaxed.
  function seedCenters(domains, saved = new Map()) {
    const nodes = [...domains].sort((a, b) => a.key.localeCompare(b.key)).map((d, i) => {
      const p = saved.get(d.key), angle = i * 2.3999632297;
      return {...d, fixed: !!p, x: p?.x ?? 800 + Math.cos(angle) * 300 * Math.sqrt(i + 1), y: p?.y ?? 500 + Math.sin(angle) * 300 * Math.sqrt(i + 1)};
    });
    if (nodes.some(n => !n.fixed)) for (let step = 0; step < 240; step++) {
      for (const n of nodes) if (!n.fixed) { n.x += (800 - n.x) * .002; n.y += (500 - n.y) * .002; }
      for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j], dx = b.x - a.x, dy = b.y - a.y, distance = Math.hypot(dx, dy) || 1;
        const overlap = a.r + b.r + 140 - distance;
        if (overlap <= 0 || (a.fixed && b.fixed)) continue;
        const force = overlap / (a.fixed || b.fixed ? 1 : 2);
        if (!a.fixed) { a.x -= dx / distance * force; a.y -= dy / distance * force; }
        if (!b.fixed) { b.x += dx / distance * force; b.y += dy / distance * force; }
      }
    }
    return new Map(nodes.map(n => [n.key, {x: n.x, y: n.y}]));
  }
  // Repair only collisions. Greedy placement preserves clear saved centers; a dragged
  // territory goes first so the user's drop stays fixed and its neighbors yield.
  function separateCenters(domains, centers, anchor = null, gap = 40) {
    const placed = [], result = new Map();
    const ordered = [...domains].sort((a, b) => Number(b.key === anchor) - Number(a.key === anchor) || a.key.localeCompare(b.key));
    for (const domain of ordered) {
      const origin = centers.get(domain.key), b = domain.bounds;
      const clear = p => placed.every(o =>
        p.x + b.right + gap <= o.x + o.bounds.left + 1e-7 ||
        o.x + o.bounds.right + gap <= p.x + b.left + 1e-7 ||
        p.y + b.bottom + gap <= o.y + o.bounds.top + 1e-7 ||
        o.y + o.bounds.bottom + gap <= p.y + b.top + 1e-7);
      let best = {...origin};
      if (!clear(best)) {
        // The closest available rectangle position lies on an obstacle boundary,
        // or shares the original x/y. Include all such coordinates, then minimize
        // displacement without moving any already-clear territory.
        const xs = [origin.x], ys = [origin.y];
        for (const o of placed) {
          xs.push(o.x + o.bounds.left - b.right - gap, o.x + o.bounds.right - b.left + gap);
          ys.push(o.y + o.bounds.top - b.bottom - gap, o.y + o.bounds.bottom - b.top + gap);
        }
        let distance = Infinity;
        for (const x of xs) for (const y of ys) {
          const candidate = {x, y}, d = (x - origin.x) ** 2 + (y - origin.y) ** 2;
          if (d < distance && clear(candidate)) { best = candidate; distance = d; }
        }
      }
      placed.push({...domain, ...best}); result.set(domain.key, best);
    }
    return result;
  }
  const finishedDensityAge = 2 * 86400000;
  function densityBins(angles, count = 24) {
    const bins = Array(count).fill(0);
    for (const angle of angles) bins[Math.min(count - 1, Math.floor(((angle % TAU + TAU) % TAU) / TAU * count))]++;
    return bins;
  }
  function create(saved = {}) {
    const preferences = new Map(saved.version >= 2 ? saved.preferences || [] : []);
    const rotations = new Map(saved.rotations || []); // v2 migration only
    const familyAngles = new Map(saved.familyAngles || []);
    const pins = new Map((saved.pins || []).map(([key, p]) => [key, saved.version >= 2 ? p : {x: p.x * 220, y: p.y * 220}]));
    const slots = new Map(), frames = new Map();
    const keyFor = (domain, id) => JSON.stringify([domain, id]);
    const home = slot => point({...slot, radius: radiusAt(frames.get(slot.domain), slot.angle, slot.radius)});
    function place(domain, members, familyOf, needsUser) {
      const groups = new Map(), counts = Object.fromEntries(order.map(b => [b, 0]));
      for (const a of members) {
        const id = familyOf(a); if (!groups.has(id)) groups.set(id, []); groups.get(id).push(a);
        counts[bandFor(a, needsUser)]++; // Pins still occupy a home in their phase.
      }
      const frame = {...metrics(counts), domain, population: members.length, shapePopulation: members.filter(a => bandFor(a, needsUser) !== "finished").length, families: []};
      let angle = -Math.PI / 2 + (rotations.get(domain) || 0);
      for (const group of sorted([...groups].map(([id, agents]) => ({id, agents})))) {
        const count = group.agents.length, span = TAU * count / Math.max(1, members.length), familyKey = keyFor(domain, group.id);
        if (!familyAngles.has(familyKey)) familyAngles.set(familyKey, angle + span / 2);
        const family = {id: group.id, count, shapeCount: group.agents.filter(a => bandFor(a, needsUser) !== "finished").length, start: angle, end: angle + span, angle: familyAngles.get(familyKey)};
        frame.families.push(family); angle += span;
        const children = sorted(group.agents.filter(a => a.id !== group.id));
        const parent = group.agents.find(a => a.id === group.id);
        if (parent) children.splice(Math.floor(children.length / 2), 0, parent);
        children.forEach((a, i) => {
          const key = keyFor(domain, a.id), pref = preferences.get(key) || {depths: {}}, band = bandFor(a, needsUser);
          const {lo, hi} = frame.bands.find(b => b.name === band);
          pref.angle ??= family.start + (pref.fraction ?? (i + .5) / count) * span;
          preferences.set(key, pref);
          slots.set(key, {domain, family: group.id, band, angle: pref.angle, radius: lo + (pref.depths[band] ?? .5) * (hi - lo), lo, hi});
        });
      }
      frames.set(domain, frame);
      return new Map(members.map(a => {
        const key = keyFor(domain, a.id), slot = slots.get(key), origin = home(slot);
        return [a.id, {...(pins.get(key) || origin), home: origin, angle: slot.angle, band: slot.band, family: slot.family, pinned: pins.has(key)}];
      }));
    }
    function drag(domain, id, x, y) {
      const key = keyFor(domain, id), slot = slots.get(key); if (!slot) return;
      const angle = Math.atan2(y, x), frame = frames.get(domain), rim = radiusAt(frame, angle);
      if (Math.hypot(x, y) > rim) { pins.set(key, {x, y}); return; }
      if (pins.get(key)?.free) { pins.set(key, {x, y, free: true}); return; }
      pins.delete(key);
      const pref = preferences.get(key);
      pref.angle = angle;
      const normalizedRadius = phaseRadius(frame, angle, Math.hypot(x, y));
      pref.depths[slot.band] = slot.hi > slot.lo ? clamp((normalizedRadius - slot.lo) / (slot.hi - slot.lo), 0, 1) : .5;
    }
    return {place, drag, describe: domain => frames.get(domain),
      pin(domain, id) { const key = keyFor(domain, id), s = slots.get(key); if (s) pins.set(key, {...home(s), free: true}); },
      unpin(domain, id) { pins.delete(keyFor(domain, id)); },
      moveHull(domain, dx, dy) { for (const [key, p] of pins) if (JSON.parse(key)[0] === domain) pins.set(key, {...p, x: p.x - dx, y: p.y - dy}); },
      snapshot: () => ({version: 3, preferences: [...preferences], familyAngles: [...familyAngles], pins: [...pins]}),
    };
  }
  return {create, bandFor, metrics, completedOpacity, radiusAt, phaseRadius, contour, bounds, seedCenters, separateCenters, finishedDensityAge, densityBins, order, labels};
})();
if (typeof module !== "undefined") module.exports = AtlasLayout;
