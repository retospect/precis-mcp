#!/usr/bin/env node
// Smoke test for the Flow tab's payload half (static/se-flow-frames.js,
// docs/backlog/se-flow-kernel.md slice 2b): decode, frame advance and
// interpolation, scene height over deepened floor, colours, the clock.
// Run by tests/test_se_flow_frames_js.py (skips without node). Exits 0
// when every check passes, 1 on the first failure.

import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import assert from 'node:assert/strict';

const here = dirname(fileURLToPath(import.meta.url));
const F = await import(resolve(here, '../src/precis_web/static/se-flow-frames.js'));

const b64 = (typed) => Buffer.from(typed.buffer, typed.byteOffset, typed.byteLength).toString('base64');
const near = (a, b, msg) => assert.ok(Math.abs(a - b) < 1e-6, `${msg}: ${a} != ${b}`);

// 4×2 cells, all inside; cell (i=1, j=0) is a hole two h0 deep.
const nx = 4;
const ny = 2;
const depth = new Float32Array(nx * ny);
depth[1] = 2;
const mask = new Uint8Array(nx * ny).fill(1);
const inlet = new Uint8Array(nx * ny);
inlet[0] = 1;
// K = 3 frames at t* = 0, 1, 2; n = 2 particles moving along +x.
const frames = new Float32Array([
  0.0, 0.1, 0.5, 0.0, 0.3, 0.2,
  0.2, 0.1, 0.5, 0.1, 0.3, 0.4,
  0.4, 0.1, 1.0, 0.2, 0.3, 0.6,
]);
const payload = {
  scale: { L: 0.1, h0: 0.001, u0: 0.01 },
  z_plane_star: 0.99,
  mesh: {
    outline: [[0, 0], [1, 0], [1, 0.5], [0, 0.5]],
    floor_depth: { shape: [ny, nx], data_b64: b64(depth) },
    mask: { shape: [ny, nx], data_b64: b64(mask) },
    ports: { inlet_b64: b64(inlet), outlet_b64: b64(new Uint8Array(nx * ny)) },
  },
  frames: { shape: [3, 2, 3], frame_t: [0, 1, 2], data_b64: b64(frames) },
  particles: {
    n: 2,
    residence_b64: b64(new Float32Array([1.5, NaN])),
    broke_b64: b64(new Uint8Array([1, 0])),
    exited_b64: b64(new Uint8Array([0, 0])),
    cross_b64: b64(new Float32Array([0.3, 0.1, NaN, NaN])),
  },
  card: null,
};

const d = F.decodePayload(payload);
assert.equal(d.n, 2, 'instance count is the frames particle axis');
assert.equal(d.K, 3);
assert.equal(d.nx, 4);
assert.equal(d.ny, 2);
assert.equal(d.inlet[0], 1);
near(d.depth[1], 2, 'depth decodes');

// A frames blob that disagrees with its shape is refused, not drawn.
assert.throws(() =>
  F.decodePayload({ ...payload, frames: { ...payload.frames, shape: [3, 3, 3] } }),
);

// Frame advance: index and fraction, clamped at both ends.
assert.deepEqual(F.frameAt(d.frameT, -1), [0, 0]);
assert.deepEqual(F.frameAt(d.frameT, 0.5), [0, 0.5]);
assert.deepEqual(F.frameAt(d.frameT, 1.25), [1, 0.25]);
assert.deepEqual(F.frameAt(d.frameT, 9), [2, 0]);

// Interpolation between stored frames.
const pos = new Float32Array(6);
F.positionsAt(d, 1.5, pos);
near(pos[0], 0.3, 'x of particle 0 halfway between frames 1 and 2');
near(pos[2], 0.75, 'psi of particle 0');
near(pos[5], 0.5, 'psi of particle 1');
F.positionsAt(d, 2, pos);
near(pos[0], 0.4, 'last frame holds');

// Scene height: psi 0 sits on the local floor, psi 1 on the flat ceiling.
const v = 0.04;
near(F.hStarAt(d, 0.3, 0.1), 3, 'h* over the hole');
near(F.sceneY(d, 0.3, 0.1, 0, v), -2 * v, 'floor of the hole');
near(F.sceneY(d, 0.3, 0.1, 1, v), v, 'ceiling over the hole');
near(F.sceneY(d, 0.6, 0.1, 0.5, v), 0.5 * v, 'mid-gap over flat floor');

// Speeds and colours.
const sp = new Float32Array(2);
F.speedsAt(d, 0, sp);
near(sp[0], 0.2, 'speed of particle 0 over frame 0→1');
assert.ok(F.speedRange(d) > 0);
assert.deepEqual(F.colormap(NaN), [0.55, 0.55, 0.55], 'trapped is grey');
assert.notDeepEqual(F.colormap(0), F.colormap(1));
const vals = new Float32Array(2);
F.colourValues(d, 'height', 1.5, F.positionsAt(d, 1.5, pos), { speed: 1, residence: 1.5 }, vals);
near(vals[0], 0.75, 'height colour is psi');
F.colourValues(d, 'residence', 1.5, pos, { speed: 1, residence: F.residenceRange(d) }, vals);
near(vals[0], 1, 'residence normalised');
assert.ok(Number.isNaN(vals[1]), 'trapped residence stays NaN');

// Plane crossings appear at their residence time.
assert.equal(F.crossedBy(d, 1.0), 0);
assert.equal(F.crossedBy(d, 1.5), 1);

// The clock: t* alone, and seconds through the scale triple (t0 = L/U0 = 10 s).
assert.equal(F.clockText(1.5, {}), 't* = 1.500');
assert.equal(F.clockText(1.5, payload.scale), 't* = 1.500 · 15.0 s');
assert.equal(F.transitSeconds({ L: 0.1 }), null);

console.log('se-flow-frames smoke: ok');
