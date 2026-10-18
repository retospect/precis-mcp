#!/usr/bin/env node
// Smoke test for the board page's shared tooltip/Selection formatter
// (static/pcb-facts.js, gr495389/90/91). Run by tests/test_pcb_facts_js.py
// (skips without node). Exits non-zero on the first failed assertion.

import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import assert from 'node:assert/strict';

const here = dirname(fileURLToPath(import.meta.url));
const F = createRequire(import.meta.url)(
  resolve(here, '../src/precis_web/static/pcb-facts.js'));

const facts = {
  parts: {
    C2: { value: '10uF', package: '0603', rating: '6.3V', lcsc: 'C19702' },
    R9: { value: '<img src=x onerror=alert(1)>', package: null, rating: null, lcsc: 'javascript:alert(1)' },
    J1: { value: null, package: null, rating: null, lcsc: null },
  },
  nets: {
    VBUS: { pins: ['J1.1', 'C2.1'], current_a: 0.12, status: 'realized', reason: null },
    GND: { pins: ['J1.2', 'C2.2', 'R9.1', 'R9.2', 'X.1'], current_a: null, status: 'realized', reason: null },
    CC2: { pins: ['J1.5', 'R9.1'], current_a: null, status: 'failed', reason: 'no path on F.Cu' },
    SBU: { pins: ['J1.8', 'R9.2'], current_a: null, status: 'unrouted', reason: 'unrouted' },
  },
};

// pad: ref/(value package rating)/Pad n
assert.equal(F.describe(facts, { kind: 'pad', refdes: 'C2', pad: '2' }).text, 'C2/(10uF 0603 6.3V)/Pad 2');
// track: net/current/[a:b]; current omitted when unknown; N pins when multi-pin
assert.equal(F.describe(facts, { kind: 'net', net: 'VBUS' }).text, 'VBUS/120mA/[J1.1:C2.1]');
assert.equal(F.describe(facts, { kind: 'net', net: 'GND' }).text, 'GND/[5 pins]');
// failed net: the router's reason is shown verbatim; unrouted shows its status
assert.equal(F.describe(facts, { kind: 'ratsnest', net: 'CC2' }).text, 'CC2/[J1.5:R9.1]/FAILED: no path on F.Cu');
assert.equal(F.describe(facts, { kind: 'ratsnest', net: 'SBU' }).text, 'SBU/[J1.8:R9.2]/unrouted');
assert.equal(
  F.describe(facts, { kind: 'pad', refdes: 'J1', pad: '5', net: 'CC2' }).text,
  'J1/Pad 5/CC2 FAILED: no path on F.Cu');

// vias: net, drill, pad, layers, stitching only on a ground net (gr496368)
const via = { net: 'GND', drill_mm: 0.3, dia_mm: 0.6, layers: ['F.Cu', 'B.Cu'], fixed: false };
assert.equal(F.viaText(facts, via), 'GND via/drill 0.3mm/pad 0.6mm/F.Cu-B.Cu/stitching');
assert.equal(
  F.viaText(facts, { ...via, net: 'VBUS', drill_mm: null, fixed: true }),
  'VBUS via/drill unknown/pad 0.6mm/F.Cu-B.Cu/not stitching/fixed');
assert.equal(F.viaText(facts, { ...via, net: null }), 'no net via/drill 0.3mm/pad 0.6mm/F.Cu-B.Cu/not stitching');
assert.equal(F.isGround({ nets: { X: { net_class: 'ground' } } }, 'X'), true, 'class beats name');
assert.equal(F.isGround({ nets: { GND: { net_class: 'power' } } }, 'GND'), false, 'class beats name');
// silkscreen: the owning part's tooltip, else the layer
const silkR9 = { role: 'refdes', layer: 'F_Silkscreen', anchor: { kind: 'instance', refdes: 'C2', instance_id: 1 } };
assert.equal(F.silkText(facts, silkR9), 'C2/(10uF 0603 6.3V)/silk refdes');
assert.equal(F.silkText(facts, { role: 'title', layer: 'F_Silkscreen', anchor: { kind: 'board' } }),
  'Silkscreen title/F_Silkscreen');

// LCSC link: fixed template, validated C-number, otherwise nothing at all
const c2 = F.describe(facts, { kind: 'part', refdes: 'C2' });
assert.equal(c2.url, 'https://www.lcsc.com/product-detail/C19702.html');
assert.equal(c2.lcsc, 'C19702');
assert.equal(F.describe(facts, { kind: 'pad', refdes: 'C2', pad: '1' }).url, c2.url, 'pad and part agree');
for (const bad of ['javascript:alert(1)', 'c123', 'C12a', 'C', '', null, undefined, 'C1/../x']) {
  assert.equal(F.lcscUrl(bad), null, `rejects ${bad}`);
}
const none = F.describe(facts, { kind: 'part', refdes: 'J1' });
assert.equal(none.url, null);
assert.equal(none.lcsc, null);
assert.equal(F.describe(facts, { kind: 'part', refdes: 'R9' }).url, null, 'malformed C-number: no link');
// user text is returned as text, never altered into markup
assert.ok(F.describe(facts, { kind: 'part', refdes: 'R9' }).text.includes('<img src=x onerror=alert(1)>'));

// load(): metadata JSON, tolerant of absence and garbage
const doc = (txt) => ({ getElementById: () => (txt === undefined ? null : { textContent: txt }) });
assert.deepEqual(F.load(doc(undefined)), { parts: {}, nets: {} });
assert.deepEqual(F.load(doc('{not json')), { parts: {}, nets: {} });
assert.equal(F.load(doc(JSON.stringify(facts))).nets.CC2.status, 'failed');
console.log('ok');
