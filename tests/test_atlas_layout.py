"""Behavioral checks for the Atlas mockup's persistent radial geometry."""
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Atlas JavaScript checks require Node")
class AtlasLayoutTests(unittest.TestCase):
    def run_js(self, body):
        setup = r"""
const assert = require('node:assert/strict');
const Atlas = require('./docs/agent-display-mockups/shared/atlas-layout.js');
const needs = a => !!a.ask || a.next?.actor === 'user';
const family = a => a.parentId || a.id;
const parent = {id: 'parent', phase: 'orchestrating', status: 'working'};
const child = {id: 'child', parentId: 'parent', phase: 'investigation', status: 'idle'};
const unrelated = {id: 'other', phase: 'planning', status: 'idle'};
const agents = [parent, child, unrelated];
const layout = Atlas.create();
const place = () => layout.place('web', agents, family, needs);
const radius = p => Atlas.phaseRadius(layout.describe("web"), p.angle, Math.hypot(p.x, p.y));
const bounds = band => layout.describe('web').bands.find(b => b.name === band);
const near = (a, b) => assert.ok(Math.abs(a - b) < 1e-10, `${a} != ${b}`);
"""
        result = subprocess.run(
            ["node", "-e", setup + body], cwd=ROOT, text=True,
            capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_phase_and_attention_override(self):
        self.run_js(r"""
for (const phase of ['discussion', 'investigation', 'planning', 'orchestrating', 'implementation', 'validation', 'deployment', 'finished']) {
  assert.equal(Atlas.bandFor({phase}, needs), phase);
}
assert.equal(Atlas.bandFor({phase: 'unknown'}, needs), 'starting');
assert.equal(Atlas.bandFor({phase: 'discussion', atlasPhase: 'starting'}, needs), 'starting');
assert.equal(Atlas.bandFor({phase: 'finished', ask: {}}, needs), 'needs');
const first = place(), original = first.get(child.id);
child.ask = {}; const waiting = place();
assert.ok(radius(waiting.get(child.id)) < radius(waiting.get(parent.id)));
near(waiting.get(child.id).angle, original.angle);
assert.ok(radius(waiting.get(parent.id)) >= bounds('orchestrating').lo);
delete child.ask;
assert.deepEqual(place().get(child.id), original);
""")

    def test_population_changes_preserve_angles_and_reload_is_stable(self):
        self.run_js(r"""
const original = place();
agents.unshift({id: 'new-child', parentId: 'parent', phase: 'investigation'});
agents.push({id: 'new-family', phase: 'deployment'});
const after = place();
assert.notDeepEqual(after.get(parent.id), original.get(parent.id));
assert.equal(after.get(parent.id).angle, original.get(parent.id).angle);
agents.reverse();
assert.deepEqual(place(), after);
const reloaded = Atlas.create(JSON.parse(JSON.stringify(layout.snapshot())));
assert.deepEqual(reloaded.place('web', agents, family, needs), after);
""")

    def test_aging_does_not_move_nodes_but_each_phase_has_its_own_band(self):
        self.run_js(r"""
const original = place();
child.updatedAt = -1000000;
assert.deepEqual(place(), original);
parent.phase = 'deployment'; child.phase = 'planning';
const progressed = place();
assert.equal(progressed.get(parent.id).band, 'deployment');
assert.equal(progressed.get(child.id).band, 'planning');
assert.notEqual(radius(progressed.get(parent.id)), radius(original.get(parent.id)));
child.phase = 'finished'; child.status = 'done';
const completed = place().get(child.id);
assert.ok(radius(completed) >= bounds('finished').lo);
child.updatedAt = -9999999999;
assert.deepEqual(place().get(child.id), completed);
assert.equal(agents.length, 3); // Layout does not close or delete aged threads.
""")

    def test_dragging_owns_angle_within_phase_and_does_not_rotate_siblings(self):
        self.run_js(r"""
const original = place();
layout.drag('web', parent.id, 20, 40);
const dragged = place();
assert.equal(dragged.get(parent.id).pinned, false);
near(dragged.get(parent.id).angle, Math.atan2(40, 20));
assert.ok(radius(dragged.get(parent.id)) <= bounds('orchestrating').hi + 1e-10);
assert.deepEqual(dragged.get(child.id), original.get(child.id));
assert.deepEqual(dragged.get(unrelated.id), original.get(unrelated.id));
parent.phase = 'deployment';
near(place().get(parent.id).angle, dragged.get(parent.id).angle);
layout.drag('web', child.id, -20, 20);
const childDragged = place().get(child.id);
near(childDragged.angle, Math.atan2(20, -20));
assert.ok(radius(childDragged) <= bounds('investigation').hi + 1e-10);
""")

    def test_outside_pin_keeps_home_and_inside_drag_releases_it(self):
        self.run_js(r"""
const original = place().get(child.id);
layout.drag('web', child.id, -1200, 800);
let p = place().get(child.id);
assert.equal(p.x, -1200); assert.equal(p.y, 800); assert.equal(p.pinned, true);
assert.deepEqual(p.home, original.home);
child.ask = {}; p = place().get(child.id);
assert.equal(p.x, -1200); assert.equal(p.y, 800);
assert.equal(p.band, 'needs'); assert.notDeepEqual(p.home, original.home);
assert.equal(bounds('needs').count, 1); // The ghost keeps its home band populated.
const reload = Atlas.create(JSON.parse(JSON.stringify(layout.snapshot())));
assert.deepEqual(reload.place('web', agents, family, needs).get(child.id), p);
layout.moveHull('web', 50, 40); p = place().get(child.id);
assert.equal(p.x + 50, -1200); assert.equal(p.y + 40, 800);
layout.drag('web', child.id, 5, 5); p = place().get(child.id);
assert.equal(p.pinned, false);
assert.ok(radius(p) <= bounds('needs').hi + 1e-10);
""")

    def test_explicit_pin_overrides_phase_until_released(self):
        self.run_js(r"""
place(); layout.pin('web', child.id);
layout.drag('web', child.id, 5, 5);
const pinned = place().get(child.id);
assert.equal(pinned.x, 5); assert.equal(pinned.y, 5); assert.equal(pinned.pinned, true);
child.ask = {}; const waiting = place().get(child.id);
assert.equal(waiting.pinned, true);
assert.equal(waiting.x, pinned.x); assert.equal(waiting.y, pinned.y);
layout.unpin('web', child.id);
assert.equal(place().get(child.id).pinned, false);
""")

    def test_organic_contours_are_nested_smooth_and_domain_seeded(self):
        self.run_js(r"""
place(); const frame = layout.describe('web');
const radii = Array.from({length: 720}, (_, i) => Atlas.radiusAt(frame, i * Math.PI / 360));
assert.ok(Math.max(...radii) - Math.min(...radii) > .1 * frame.rim);
assert.ok(radii.every(r => r >= .91 * frame.rim && r <= 1.39 * frame.rim));
for (let i = 1; i < radii.length; i++) assert.ok(Math.abs(radii[i] - radii[i-1]) < .02 * frame.rim);
const inner = Atlas.contour(frame, 20), outer = Atlas.contour(frame, 40);
inner.forEach((p, i) => { near(p.x * 2, outer[i].x); near(p.y * 2, outer[i].y); });
const box = Atlas.bounds(frame);
assert.ok(Atlas.contour(frame).every(p => p.x >= box.left && p.x <= box.right && p.y >= box.top && p.y <= box.bottom));
layout.place('backend', agents, family, needs);
assert.notDeepEqual(Atlas.contour(frame), Atlas.contour(layout.describe('backend')));
const reload = Atlas.create(layout.snapshot()); reload.place('web', agents, family, needs);
assert.deepEqual(Atlas.contour(frame), Atlas.contour(reload.describe('web')));
""")

    def test_initial_force_placement_preserves_saved_territories(self):
        self.run_js(r"""
const domains = Array.from({length: 13}, (_, i) => ({key: `domain-${i}`, r: 80 + i * 10}));
const seeded = Atlas.seedCenters(domains);
assert.deepEqual(Atlas.seedCenters(domains), seeded);
assert.deepEqual(Atlas.seedCenters(domains.reverse(), seeded), seeded);
for (const [a, p] of seeded) for (const [b, q] of seeded) if (a !== b) {
  assert.ok(Math.hypot(p.x - q.x, p.y - q.y) > domains.find(d => d.key === a).r + domains.find(d => d.key === b).r);
}
seeded.set('domain-0', {x: 10000, y: -10000});
const added = Atlas.seedCenters([...domains, {key: 'new-domain', r: 200}], seeded);
for (const [id, p] of seeded) assert.deepEqual(added.get(id), p);
""")

    def test_empty_bands_collapse_and_trailing_bands_do_not_expand_rim(self):
        self.run_js(r"""
const sparse = Atlas.metrics({needs: 0, investigation: 4});
assert.deepEqual(sparse.bands.map(b => b.name), ['needs', 'starting', 'discussion', 'investigation']);
assert.equal(sparse.bands[0].outer, 10);
assert.equal(sparse.bands[1].outer - sparse.bands[1].inner, 6);
assert.equal(sparse.bands[3].outer - sparse.bands[3].inner, 52);
assert.equal(sparse.rim, sparse.bands[3].outer + 20);
const crowded = Atlas.metrics({investigation: 16});
assert.equal(crowded.bands[3].outer - crowded.bands[3].inner, 96);
assert.ok(crowded.rim > sparse.rim);
const empty = Atlas.metrics({}); assert.ok(empty.rim <= 30);
""")

    def test_saved_overlaps_are_repaired_once_without_changing_clear_centers(self):
        self.run_js(r"""
const domains = ['a', 'b', 'c', 'far'].map(key => ({key, bounds: {left: -120, right: 160, top: -100, bottom: 140}}));
const saved = new Map([['a', {x: 0, y: 0}], ['b', {x: 80, y: 0}], ['c', {x: 80, y: 50}], ['far', {x: 3000, y: 2000}]]);
const repaired = Atlas.separateCenters(domains, saved);
assert.deepEqual(repaired.get('a'), saved.get('a'));
assert.deepEqual(repaired.get('far'), saved.get('far'));
assert.notDeepEqual(repaired.get('b'), saved.get('b'));
for (const a of domains) for (const b of domains) if (a.key !== b.key) {
 const p = repaired.get(a.key), q = repaired.get(b.key);
 assert.ok(p.x + a.bounds.right + 40 <= q.x + b.bounds.left + 1e-7 || q.x + b.bounds.right + 40 <= p.x + a.bounds.left + 1e-7 || p.y + a.bounds.bottom + 40 <= q.y + b.bounds.top + 1e-7 || q.y + b.bounds.bottom + 40 <= p.y + a.bounds.top + 1e-7);
}
assert.deepEqual(Atlas.separateCenters(domains, repaired), repaired, 'redraw and zoom do not drift');
assert.deepEqual(Atlas.separateCenters([...domains].reverse(), saved), repaired, 'input order does not change repair');
const dropped = Atlas.separateCenters(domains, saved, 'b');
assert.deepEqual(dropped.get('b'), saved.get('b'), 'user drop stays anchored');
assert.notDeepEqual(dropped.get('a'), saved.get('a'));
""")

    def test_collision_repair_handles_growth_and_coincident_centers(self):
        self.run_js(r"""
const domains = Array.from({length: 13}, (_, i) => ({key: `d-${i}`, bounds: {left: -100 - i, right: 130 + i, top: -90, bottom: 120}}));
const coincident = new Map(domains.map(d => [d.key, {x: 0, y: 0}]));
const repaired = Atlas.separateCenters(domains, coincident);
assert.equal(new Set([...repaired.values()].map(p => `${p.x},${p.y}`)).size, domains.length);
assert.deepEqual(Atlas.separateCenters(domains, repaired), repaired);
domains[0].bounds = {left: -500, right: 500, top: -500, bottom: 500};
const grown = Atlas.separateCenters(domains, repaired);
assert.notDeepEqual(grown, repaired);
assert.deepEqual(Atlas.separateCenters(domains, grown), grown);
""")

    def test_family_sectors_are_proportional_and_cover_the_circle(self):
        self.run_js(r"""
agents.push({id: 'child-two', parentId: 'parent', phase: 'planning'});
agents.push({id: 'child-three', parentId: 'parent', phase: 'planning'});
const positions = place(), frame = layout.describe('web');
const big = frame.families.find(f => f.id === 'parent');
const small = frame.families.find(f => f.id === 'other');
near((big.end - big.start) / (small.end - small.start), 4);
near(frame.families.at(-1).end - frame.families[0].start, Math.PI * 2);
const angles = [...positions.values()].map(p => p.angle).sort((a, b) => a - b);
for (let i = 0; i < angles.length; i++) {
  const next = angles[(i + 1) % angles.length] + (i === angles.length - 1 ? Math.PI * 2 : 0);
  near(next - angles[i], Math.PI * 2 / agents.length);
}
""")

    def test_taper_smooths_center_without_crossing_contours_or_breaking_drag(self):
        self.run_js(r"""
place(); const frame = layout.describe('web');
for (let i = 0; i < 360; i++) {
  const angle = i * Math.PI / 180;
  near(Atlas.radiusAt(frame, angle, frame.rim * .3), frame.rim * .3);
  let previous = 0;
  for (let j = 1; j <= 100; j++) {
    const base = frame.rim * j / 100, actual = Atlas.radiusAt(frame, angle, base);
    assert.ok(actual > previous, 'contours remain nested at every angle');
    near(Atlas.phaseRadius(frame, angle, actual), base);
    previous = actual;
  }
}
const base = bounds('orchestrating').lo + 7, angle = 1.1;
const distance = Atlas.radiusAt(frame, angle, base);
layout.drag('web', parent.id, Math.cos(angle) * distance, Math.sin(angle) * distance);
const p = place().get(parent.id);
near(radius(p), base); near(p.angle, angle);
""")

    def test_finished_population_does_not_take_space_from_active_phases(self):
        self.run_js(r"""
const one = Atlas.metrics({investigation: 1, implementation: 1, finished: 1});
const graveyard = Atlas.metrics({investigation: 1, implementation: 1, finished: 10000});
assert.equal(one.rim, graveyard.rim);
assert.deepEqual(one.bands.map(b => [b.name, b.inner, b.outer]), graveyard.bands.map(b => [b.name, b.inner, b.outer]));
const done = graveyard.bands.find(b => b.name === 'finished');
for (const name of ['investigation', 'implementation']) {
  const active = graveyard.bands.find(b => b.name === name);
  assert.ok(active.outer - active.inner >= 52);
  assert.ok(done.outer - done.inner < (active.outer - active.inner) / 2);
}
""")

    def test_finished_threads_do_not_add_lobe_weight(self):
        self.run_js(r"""
parent.phase = child.phase = unrelated.phase = 'finished';
parent.status = child.status = unrelated.status = 'done';
place(); const frame = layout.describe('web');
assert.equal(frame.shapePopulation, 0);
assert.ok(frame.families.every(f => f.shapeCount === 0));
for (let i = 0; i < 360; i++) assert.ok(Atlas.radiusAt(frame, i * Math.PI / 180) <= frame.rim * 1.091);
child.ask = {}; place(); const attention = layout.describe('web');
assert.equal(attention.shapePopulation, 1);
assert.equal(attention.families.find(f => f.id === 'parent').shapeCount, 1);
""")

    def test_density_bins_preserve_counts_across_angle_wrapping(self):
        self.run_js(r"""
const bins = Atlas.densityBins([0, Math.PI * 2, -Math.PI * 2, -.01, Math.PI]);
assert.equal(bins.length, 24);
assert.equal(bins.reduce((a, b) => a + b, 0), 5);
assert.equal(bins[0], 3); assert.equal(bins[23], 1); assert.equal(bins[12], 1);
assert.equal(Atlas.finishedDensityAge, 2 * 86400000);
""")

    def test_completed_age_fade_is_visible_but_bounded(self):
        self.run_js(r"""
const day = 86400000;
near(Atlas.completedOpacity(0), 1);
assert.ok(Atlas.completedOpacity(day) < 0.75);
assert.ok(Atlas.completedOpacity(7 * day) < 0.3);
assert.ok(Atlas.completedOpacity(1000 * day) >= 0.22);
near(Atlas.completedOpacity(-day), 1);
""")

    def test_legacy_free_pin_coordinates_are_migrated(self):
        self.run_js(r"""
const key = JSON.stringify(['web', child.id]);
const old = Atlas.create({pins: [[key, {x: 0.5, y: -0.2}]]});
const p = old.place('web', agents, family, needs).get(child.id);
assert.equal(p.x, 110); assert.equal(p.y, -44); assert.equal(p.pinned, true);
assert.equal(old.snapshot().version, 3);
""")

    def test_saved_layouts_are_scoped_by_domain(self):
        self.run_js(r"""
const original = place();
layout.place('backend', agents, family, needs);
layout.drag('backend', parent.id, 0.4, 0);
assert.deepEqual(place(), original);
""")


if __name__ == "__main__":
    unittest.main()
