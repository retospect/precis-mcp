// se flow player — the payload half (no three.js, no DOM): decode the
// view='flow' format='json' payload (precis_se/flow.py::flow_payload),
// interpolate particle positions between recorded frames, map them into
// the scene's frame, colour them, and read the clock. The three.js half
// is se-flow-player.js; scripts/se_flow_frames_smoke.mjs tests this one
// under node.
//
// Scene frame: X = x*, Z = y* (both in units of L, the plan on the XZ
// plane), Y = up in units of `vUnit` per h0 (the vertical exaggeration
// is the caller's). Nominal floor at Y = 0, flat ceiling at Y = vUnit;
// a deepened cell's floor sits at -(h* - 1) * vUnit.

function b64Bytes(s) {
  const bin = atob(s || "");
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function f32(s) {
  const b = b64Bytes(s);
  return new Float32Array(b.buffer, b.byteOffset, b.byteLength >> 2);
}

/** Decode the payload into typed arrays. `n` is the instance count the
 * player draws (the frames blob's particle axis, after any server
 * subsampling). */
export function decodePayload(payload) {
  const mesh = payload.mesh;
  const [ny, nx] = mesh.mask.shape;
  const fr = payload.frames;
  const [K, n] = fr ? fr.shape : [0, 0];
  const frames = fr ? f32(fr.data_b64) : new Float32Array(0);
  if (frames.length !== K * n * 3) {
    throw new Error(`frames blob holds ${frames.length} floats, shape says ${K * n * 3}`);
  }
  const p = payload.particles || {};
  const ports = mesh.ports || {};
  return {
    nx,
    ny,
    dx: 1 / nx,
    K,
    n,
    frames,
    frameT: Float64Array.from((fr && fr.frame_t) || []),
    mask: b64Bytes(mesh.mask.data_b64),
    depth: f32(mesh.floor_depth.data_b64),
    inlet: b64Bytes(ports.inlet_b64),
    outlet: b64Bytes(ports.outlet_b64),
    residence: f32(p.residence_b64),
    broke: b64Bytes(p.broke_b64),
    exited: b64Bytes(p.exited_b64),
    cross: f32(p.cross_b64),
    outline: mesh.outline || [],
    zPlane: payload.z_plane_star ?? 0.99,
    scale: payload.scale || {},
    card: payload.card || null,
    subsampled: payload.subsampled || null,
  };
}

/** `[k, frac]`: t* lies between frame k and k+1 at fraction frac
 * (clamped to the recorded span). */
export function frameAt(frameT, t) {
  const K = frameT.length;
  if (K === 0) return [0, 0];
  if (t <= frameT[0]) return [0, 0];
  if (t >= frameT[K - 1]) return [K - 1, 0];
  let lo = 0;
  let hi = K - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (frameT[mid] <= t) lo = mid;
    else hi = mid;
  }
  const span = frameT[hi] - frameT[lo];
  return [lo, span > 0 ? (t - frameT[lo]) / span : 0];
}

/** Interpolated (x*, y*, psi) of every particle at t* into `out`
 * (Float32Array n*3). */
export function positionsAt(d, t, out) {
  const [k, f] = frameAt(d.frameT, t);
  const a = k * d.n * 3;
  const k1 = Math.min(k + 1, d.K - 1);
  const b = k1 * d.n * 3;
  const fr = d.frames;
  for (let i = 0; i < d.n * 3; i++) out[i] = fr[a + i] + (fr[b + i] - fr[a + i]) * f;
  return out;
}

/** h* (local gap over h0) of the cell holding (x*, y*); 1 outside. */
export function hStarAt(d, x, y) {
  const i = Math.min(d.nx - 1, Math.max(0, Math.floor(x / d.dx)));
  const j = Math.min(d.ny - 1, Math.max(0, Math.floor(y / d.dx)));
  const c = j * d.nx + i;
  return d.mask[c] ? 1 + d.depth[c] : 1;
}

/** Scene Y (in units of vUnit per h0) of a particle at gap fraction psi. */
export function sceneY(d, x, y, psi, vUnit) {
  const h = hStarAt(d, x, y);
  return (psi * h - (h - 1)) * vUnit;
}

/** Per-particle horizontal speed between frame k and k+1 (reduced
 * units, |dx*|/dt*). */
export function speedsAt(d, k, out) {
  const k1 = Math.min(k + 1, d.K - 1);
  const dt = d.frameT[k1] - d.frameT[k] || 1;
  const a = k * d.n * 3;
  const b = k1 * d.n * 3;
  for (let p = 0; p < d.n; p++) {
    const dx = d.frames[b + 3 * p] - d.frames[a + 3 * p];
    const dy = d.frames[b + 3 * p + 1] - d.frames[a + 3 * p + 1];
    out[p] = Math.hypot(dx, dy) / dt;
  }
  return out;
}

/** The speed colour's full scale: twice the median moving speed — a
 * point-like inlet makes the top percentiles the singular speeds at the
 * port, which would paint the chamber body the bottom colour. */
export function speedRange(d) {
  if (d.K < 2) return 1;
  const sample = [];
  const tmp = new Float32Array(d.n);
  const step = Math.max(1, Math.floor(d.K / 16));
  for (let k = 0; k < d.K - 1; k += step) {
    speedsAt(d, k, tmp);
    for (let p = 0; p < d.n; p++) if (tmp[p] > 0) sample.push(tmp[p]);
  }
  if (!sample.length) return 1;
  sample.sort((x, y) => x - y);
  return 2 * sample[Math.floor(0.5 * (sample.length - 1))] || 1;
}

export function residenceRange(d) {
  let hi = 0;
  for (const r of d.residence) if (Number.isFinite(r) && r > hi) hi = r;
  return hi || 1;
}

// viridis, 9 stops — perceptual, colour-blind safe, reads on light and dark.
const VIRIDIS = [
  [0.267, 0.005, 0.329],
  [0.283, 0.141, 0.458],
  [0.254, 0.265, 0.53],
  [0.207, 0.372, 0.553],
  [0.164, 0.471, 0.558],
  [0.128, 0.567, 0.551],
  [0.135, 0.659, 0.518],
  [0.267, 0.749, 0.441],
  [0.993, 0.906, 0.144],
];

/** viridis(v) for v in [0, 1] → [r, g, b]; NaN → neutral grey. */
export function colormap(v) {
  if (!Number.isFinite(v)) return [0.55, 0.55, 0.55];
  const x = Math.min(1, Math.max(0, v)) * (VIRIDIS.length - 1);
  const i = Math.min(VIRIDIS.length - 2, Math.floor(x));
  const f = x - i;
  const a = VIRIDIS[i];
  const b = VIRIDIS[i + 1];
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
}

/** Colour value in [0, 1] (or NaN) per particle for `mode`:
 * 'speed' | 'height' | 'residence'. `pos` is positionsAt's output,
 * `ranges` = {speed, residence}. */
export function colourValues(d, mode, t, pos, ranges, out) {
  if (mode === "height") {
    for (let p = 0; p < d.n; p++) out[p] = pos[3 * p + 2];
  } else if (mode === "residence") {
    for (let p = 0; p < d.n; p++) out[p] = d.residence[p] / ranges.residence;
  } else {
    const [k] = frameAt(d.frameT, t);
    speedsAt(d, Math.min(k, Math.max(0, d.K - 2)), out);
    for (let p = 0; p < d.n; p++) out[p] = out[p] / ranges.speed;
  }
  return out;
}

/** Transit time t0 = L / U0 in seconds, or null without U0. */
export function transitSeconds(scale) {
  const L = Number(scale && scale.L);
  const u0 = Number(scale && scale.u0);
  return L > 0 && u0 > 0 ? L / u0 : null;
}

function fmtSeconds(s) {
  if (s >= 3600) return `${(s / 3600).toPrecision(3)} h`;
  if (s >= 60) return `${(s / 60).toPrecision(3)} min`;
  if (s >= 1) return `${s.toPrecision(3)} s`;
  if (s >= 1e-3) return `${(s * 1e3).toPrecision(3)} ms`;
  return `${(s * 1e6).toPrecision(3)} µs`;
}

/** The clock line: "t* = 1.23" and, with a scale, " · 4.56 s". */
export function clockText(t, scale) {
  const t0 = transitSeconds(scale);
  const head = `t* = ${t.toFixed(3)}`;
  return t0 === null ? head : `${head} · ${fmtSeconds(t * t0)}`;
}

/** Number of broke particles whose first crossing is at or before t*. */
export function crossedBy(d, t) {
  let c = 0;
  for (let p = 0; p < d.n; p++) if (d.broke[p] && d.residence[p] <= t) c++;
  return c;
}
