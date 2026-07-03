#!/usr/bin/env node
// Render-parity check: run the browser tessellator (static/cad-tessellate.js)
// over a corpus of design nodes and diff its (verts, tris) against the golden
// meshes the Python server tessellator (precis.cad.tessellate) produced.
//
// Usage: node scripts/cad_tessellate_parity.mjs <cases.json>
// Exits 0 on full parity, 1 on the first mismatch (with a diagnostic on stderr).
// Invoked by tests/test_cad_parity.py, which skips when node is unavailable.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const modPath = resolve(__dirname, '../src/precis_web/static/cad-tessellate.js');
const { nodeMesh } = await import(modPath);

const TOL = 1e-6;

function fail(msg) { console.error('PARITY FAIL: ' + msg); process.exit(1); }

const casesPath = process.argv[2];
if (!casesPath) fail('no cases file given');
const cases = JSON.parse(readFileSync(casesPath, 'utf8')).cases;

let checked = 0;
for (const c of cases) {
  for (const item of c.nodes) {
    const got = nodeMesh(item.node);
    const exp = item.expected;
    const label = `${c.design}/${item.node.name}`;
    if (!got) fail(`${label}: JS produced no mesh`);
    if (got.verts.length !== exp.verts.length)
      fail(`${label}: vertex count ${got.verts.length} != ${exp.verts.length}`);
    if (got.tris.length !== exp.tris.length)
      fail(`${label}: triangle count ${got.tris.length} != ${exp.tris.length}`);
    for (let i = 0; i < exp.verts.length; i++) {
      for (let k = 0; k < 3; k++) {
        if (Math.abs(got.verts[i][k] - exp.verts[i][k]) > TOL)
          fail(`${label}: vertex[${i}][${k}] ${got.verts[i][k]} != ${exp.verts[i][k]}`);
      }
    }
    for (let i = 0; i < exp.tris.length; i++) {
      for (let k = 0; k < 3; k++) {
        if (got.tris[i][k] !== exp.tris[i][k])
          fail(`${label}: tri[${i}][${k}] ${got.tris[i][k]} != ${exp.tris[i][k]}`);
      }
    }
    checked++;
  }
}
console.log(`parity OK — ${checked} nodes match within ${TOL}`);
process.exit(0);
