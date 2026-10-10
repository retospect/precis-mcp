#!/usr/bin/env node
// Smoke test for the se viewer's shared view state (static/se-view-state.js,
// gr477845 / gr458084). Run by tests/test_se_view_state_js.py (skips without
// node). Exits 0 when every check passes, 1 on the first failure.

import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import assert from 'node:assert/strict';

const here = dirname(fileURLToPath(import.meta.url));
const { createObjectVisibility, createLatest } = await import(
  resolve(here, '../src/precis_web/static/se-view-state.js')
);

// One eye state, consulted by every renderer; absent paths default shown.
const vis = createObjectVisibility();
assert.equal(vis.shapeShown('d/bud96'), true);
let fired = 0;
vis.onChange(() => fired++);
vis.update({ 'd/bud96': [0, 1], 'd/bud96 (grid)': [1, 0], 'd/other': [2, 2] });
assert.equal(fired, 1);
assert.equal(vis.shapeShown('d/bud96'), false, 'eye off hides the object');
assert.equal(vis.edgesShown('d/bud96'), true);
assert.equal(vis.edgesShown('d/bud96 (grid)'), false, 'grid lines eye is its own flag');
assert.equal(vis.shapeShown('d/other'), true, 'mixed group is not hidden');

// Survives a revision step: restore() re-applies only plain hidden entries.
assert.deepEqual(vis.hiddenStates(), {
  'd/bud96': [0, 1],
  'd/bud96 (grid)': [1, 0],
});
const applied = [];
vis.restore({ setStates: (s) => applied.push(s) });
assert.equal(applied.length, 1);
assert.deepEqual(Object.keys(applied[0]).sort(), ['d/bud96', 'd/bud96 (grid)']);

// A path missing from a later notification keeps its eye; showing it again clears it.
vis.update({ 'd/x': [1, 1] });
assert.equal(vis.shapeShown('d/bud96'), false);
vis.update({ 'd/bud96': [1, 1] });
assert.equal(vis.shapeShown('d/bud96'), true);
assert.ok(!('d/bud96' in vis.hiddenStates()));
const none = [];
const v2 = createObjectVisibility();
v2.restore({ setStates: (s) => none.push(s) });
assert.equal(none.length, 0, 'nothing hidden: no setStates call');

// Latest wins: a newer begin() aborts and stales the older request.
const latest = createLatest();
const a = latest.begin();
assert.equal(a.isStale(), false);
const b = latest.begin();
assert.equal(a.isStale(), true, 'older request is stale');
assert.equal(a.signal.aborted, true, 'older request is aborted');
assert.equal(b.isStale(), false);
assert.equal(b.signal.aborted, false);
latest.cancel();
assert.equal(b.isStale(), true);

// Overlapping loads: only the newest renders, exactly once.
const rendered = [];
async function load(rev, delayMs) {
  const req = latest.begin();
  await new Promise((r) => setTimeout(r, delayMs));
  if (req.isStale()) return;
  rendered.push(rev);
}
await Promise.all([load(1, 30), load(2, 10), load(3, 20)]);
assert.deepEqual(rendered, [3]);

console.log('se-view-state smoke: ok');
