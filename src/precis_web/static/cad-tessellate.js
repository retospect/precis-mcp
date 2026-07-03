// Client-side CAD tessellator — a faithful port of precis.cad.tessellate
// (+ the node/pattern transforms from precis.cad.scene). It turns a design's
// *recipe* (alias + params + pose, served by /cad/<slug>/scene.json) into the
// same (verts, tris) the server's numpy tessellator emits, so the browser view
// can never drift from the STL/glTF geometry. Pure ES module — no three.js — so
// the render-parity test can run it headless under node.
//
// Conventions match precis/cad/tessellate.py exactly:
//   _FN = 64 curved segments, _FN_LAT = 32 sphere latitude bands, and the
//   z-up (mm) IR frame. Vertices are [x, y, z]; triangles are [a, b, c].

const FN = 64;
const FN_LAT = 32;
const EPS = 1e-9;

// ── low-level linear algebra (3-vectors, 3×3 matrices) ──────────────────────
const DEG2RAD = Math.PI / 180.0;

function matVec(m, v) {
  return [
    m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
    m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
    m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
  ];
}
function matMul(a, b) {
  const out = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
  for (let i = 0; i < 3; i++)
    for (let j = 0; j < 3; j++)
      out[i][j] = a[i][0] * b[0][j] + a[i][1] * b[1][j] + a[i][2] * b[2][j];
  return out;
}
const IDENT = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
function rotX(r) { const c = Math.cos(r), s = Math.sin(r); return [[1, 0, 0], [0, c, -s], [0, s, c]]; }
function rotY(r) { const c = Math.cos(r), s = Math.sin(r); return [[c, 0, s], [0, 1, 0], [-s, 0, c]]; }
function rotZ(r) { const c = Math.cos(r), s = Math.sin(r); return [[c, -s, 0], [s, c, 0], [0, 0, 1]]; }

// A rigid transform {R, t}: world = R @ local + t.
function xform(R, t) { return { R, t }; }
const T_IDENT = xform(IDENT, [0, 0, 0]);
function translation(x, y, z) { return xform(IDENT, [x, y, z]); }
function rotation(rxDeg, ryDeg, rzDeg) {
  // Rz @ Ry @ Rx, matching precis.cad.vec.rotation.
  const R = matMul(matMul(rotZ(rzDeg * DEG2RAD), rotY(ryDeg * DEG2RAD)), rotX(rxDeg * DEG2RAD));
  return xform(R, [0, 0, 0]);
}
// self ∘ other — apply `other` first (precis.cad.vec.Transform.compose).
function compose(self, other) {
  return xform(matMul(self.R, other.R), [
    self.R[0][0] * other.t[0] + self.R[0][1] * other.t[1] + self.R[0][2] * other.t[2] + self.t[0],
    self.R[1][0] * other.t[0] + self.R[1][1] * other.t[1] + self.R[1][2] * other.t[2] + self.t[1],
    self.R[2][0] * other.t[0] + self.R[2][1] * other.t[1] + self.R[2][2] * other.t[2] + self.t[2],
  ]);
}
function applyXform(xf, v) {
  const rv = matVec(xf.R, v);
  return [rv[0] + xf.t[0], rv[1] + xf.t[1], rv[2] + xf.t[2]];
}

// ── orientation ─────────────────────────────────────────────────────────────
function signedVolume(verts, tris) {
  // 6× signed volume — positive iff faces wind outward (CCW).
  let acc = 0;
  for (const [i0, i1, i2] of tris) {
    const a = verts[i0], b = verts[i1], c = verts[i2];
    // a · (b × c)
    const cx = b[1] * c[2] - b[2] * c[1];
    const cy = b[2] * c[0] - b[0] * c[2];
    const cz = b[0] * c[1] - b[1] * c[0];
    acc += a[0] * cx + a[1] * cy + a[2] * cz;
  }
  return acc;
}
function orientOutward(verts, tris) {
  if (signedVolume(verts, tris) < 0.0) {
    return { verts, tris: tris.map((t) => [t[2], t[1], t[0]]) };
  }
  return { verts, tris };
}

// ── ring / cap helpers ───────────────────────────────────────────────────────
function ngonXY(n, r) {
  const out = [];
  for (let i = 0; i < n; i++) {
    out.push([r * Math.cos((2 * Math.PI * i) / n), r * Math.sin((2 * Math.PI * i) / n)]);
  }
  return out;
}
function fan(indices) {
  // Triangulate a convex ring as a fan from the first index.
  const out = [];
  for (let i = 1; i < indices.length - 1; i++) out.push([indices[0], indices[i], indices[i + 1]]);
  return out;
}

function extrude(bottomXY, topXY, h) {
  const n = bottomXY.length;
  const verts = [];
  for (const [x, y] of bottomXY) verts.push([x, y, 0.0]);
  for (const [x, y] of topXY) verts.push([x, y, h]);
  const bot = []; for (let i = 0; i < n; i++) bot.push(i);
  const top = []; for (let i = 0; i < n; i++) top.push(n + i);
  const tris = [];
  for (const [a, b, c] of fan(bot)) tris.push([a, c, b]); // bottom cap reversed
  for (const t of fan(top)) tris.push(t);                 // top cap
  for (let i = 0; i < n; i++) {                            // sides
    const j = (i + 1) % n;
    tris.push([bot[i], bot[j], top[j]]);
    tris.push([bot[i], top[j], top[i]]);
  }
  return orientOutward(verts, tris);
}

function cone(bottomXY, h) {
  const n = bottomXY.length;
  const verts = [];
  for (const [x, y] of bottomXY) verts.push([x, y, 0.0]);
  verts.push([0.0, 0.0, h]);
  const apex = n;
  const base = []; for (let i = 0; i < n; i++) base.push(i);
  const tris = [];
  for (const [a, b, c] of fan(base)) tris.push([a, c, b]); // base cap reversed
  for (let i = 0; i < n; i++) tris.push([i, (i + 1) % n, apex]);
  return orientOutward(verts, tris);
}

function circular(rb, rt, h, seg = FN) {
  if (rt <= EPS) return cone(ngonXY(seg, rb), h);
  if (rb <= EPS) {
    // inverted cone — apex at the base; shift so base sits at z=h, apex at z=0
    const { verts, tris } = cone(ngonXY(seg, rt), -h);
    const v = verts.map(([x, y, z]) => [x, y, z + h]);
    return orientOutward(v, tris);
  }
  return extrude(ngonXY(seg, rb), ngonXY(seg, rt), h);
}

function sphere(r) {
  const nlon = FN, nlat = FN_LAT;
  const verts = [[0.0, 0.0, r]]; // north pole
  for (let i = 1; i < nlat; i++) {
    const phi = (Math.PI * i) / nlat;
    const z = r * Math.cos(phi);
    const rho = r * Math.sin(phi);
    for (let j = 0; j < nlon; j++) {
      const th = (2 * Math.PI * j) / nlon;
      verts.push([rho * Math.cos(th), rho * Math.sin(th), z]);
    }
  }
  const south = verts.length;
  verts.push([0.0, 0.0, -r]); // south pole
  const tris = [];
  const ring = (i, j) => 1 + (i - 1) * nlon + (((j % nlon) + nlon) % nlon);
  for (let j = 0; j < nlon; j++) tris.push([0, ring(1, j), ring(1, j + 1)]);
  for (let i = 1; i < nlat - 1; i++) {
    for (let j = 0; j < nlon; j++) {
      const a = ring(i, j), b = ring(i, j + 1), c = ring(i + 1, j), d = ring(i + 1, j + 1);
      tris.push([a, c, d]);
      tris.push([a, d, b]);
    }
  }
  for (let j = 0; j < nlon; j++) tris.push([south, ring(nlat - 1, j + 1), ring(nlat - 1, j)]);
  return orientOutward(verts, tris);
}

function torus(R, r) {
  const nu = FN, nv = FN;
  const verts = [];
  for (let i = 0; i < nu; i++) {
    const u = (2 * Math.PI * i) / nu;
    for (let j = 0; j < nv; j++) {
      const v = (2 * Math.PI * j) / nv;
      const rho = R + r * Math.cos(v);
      verts.push([rho * Math.cos(u), rho * Math.sin(u), r * Math.sin(v)]);
    }
  }
  const idx = (i, j) => (i % nu) * nv + (j % nv);
  const tris = [];
  for (let i = 0; i < nu; i++) {
    for (let j = 0; j < nv; j++) {
      const a = idx(i, j), b = idx(i + 1, j), c = idx(i, j + 1), d = idx(i + 1, j + 1);
      tris.push([a, b, d]);
      tris.push([a, d, c]);
    }
  }
  return orientOutward(verts, tris);
}

// ── public: shape → local-frame mesh (mirrors tessellate.mesh_shape) ─────────
export function tessellateShape(alias, p) {
  switch (alias) {
    case 'box': {
      const hw = p.w / 2.0, hd = p.d / 2.0;
      const ring = [[-hw, -hd], [hw, -hd], [hw, hd], [-hw, hd]];
      return extrude(ring, ring, p.h);
    }
    case 'cyl': return circular(p.r, p.r, p.h);
    case 'cone': return circular(p.r, 0.0, p.h);
    case 'tcone': return circular(p.rb, p.rt, p.h);
    case 'sphere': return sphere(p.r);
    case 'torus': return torus(p.R, p.r);
    case 'hex': { const ring = ngonXY(6, p.r); return extrude(ring, ring, p.h); }
    case 'ngon': { const ring = ngonXY(Math.round(p.n), p.r); return extrude(ring, ring, p.h); }
    case 'frustum': return extrude(ngonXY(Math.round(p.n), p.rb), ngonXY(Math.round(p.n), p.rt), p.h);
    case 'pyramid': return cone(ngonXY(Math.round(p.n), p.r), p.h);
    default:
      return null; // chamfer / unbounded — no finite mesh (matches TessellationError skip)
  }
}

// leaf world transform: translate(loc) ∘ rotate(rot)  (scene._node_xform)
function nodeXform(loc, rot) {
  const t = translation(loc[0], loc[1], loc[2]);
  if (rot[0] === 0.0 && rot[1] === 0.0 && rot[2] === 0.0) return t;
  return compose(t, rotation(rot[0], rot[1], rot[2]));
}

// per-instance transforms for a patterned node (scene._pattern_transforms)
function patternTransforms(node) {
  const pat = node.pattern;
  const loc = node.loc, rot = node.rot;
  const baseRot = (rot[0] || rot[1] || rot[2]) ? rotation(rot[0], rot[1], rot[2]) : T_IDENT;
  const out = [];
  if (pat.kind === 'polar') {
    const n = Math.round(pat.n), r = pat.r, z = loc[2];
    for (let i = 0; i < n; i++) {
      const theta = (360.0 * i) / n;
      const xf = compose(rotation(0.0, 0.0, theta), translation(r, 0.0, z));
      out.push(compose(xf, baseRot));
    }
  } else if (pat.kind === 'linear') {
    const n = Math.round(pat.n), dx = pat.dx, dy = pat.dy, dz = pat.dz;
    for (let i = 0; i < n; i++) {
      const xf = translation(loc[0] + i * dx, loc[1] + i * dy, loc[2] + i * dz);
      out.push(compose(xf, baseRot));
    }
  }
  return out;
}

// ── public: node → world-space meshes (mirrors tessellate.node_meshes) ───────
// `node` is a scene.json node: {shape:{alias,params}|null, loc, rot, pattern}.
export function nodeMeshes(node) {
  if (!node.shape) return [];
  const base = tessellateShape(node.shape.alias, node.shape.params);
  if (!base) return [];
  const xfs = node.pattern ? patternTransforms(node) : [nodeXform(node.loc, node.rot)];
  return xfs.map((xf) => ({ verts: base.verts.map((v) => applyXform(xf, v)), tris: base.tris }));
}

// Concatenate meshes into one {verts, tris} with offset indices (gltf._merge).
export function merge(meshes) {
  if (!meshes.length) return null;
  if (meshes.length === 1) return meshes[0];
  const verts = [];
  const tris = [];
  let base = 0;
  for (const m of meshes) {
    for (const v of m.verts) verts.push(v);
    for (const t of m.tris) tris.push([t[0] + base, t[1] + base, t[2] + base]);
    base += m.verts.length;
  }
  return { verts, tris };
}

// One merged world-space mesh per node (what a clickable viewer object needs).
export function nodeMesh(node) {
  return merge(nodeMeshes(node));
}
