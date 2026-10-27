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
    C2: { value: '10uF', package: '0603', rating: '6.3V', lcsc: 'C19702',
          datasheet: 'https://datasheet.lcsc.com/lcsc/C19702.pdf',
          script: { file: 'main.py', line: 4, ws: 'leaf-a' } },
    C3: { value: 'C 1.0pF', package: null, rating: null, lcsc: null },
    R9: { value: '<img src=x onerror=alert(1)>', package: null, rating: null, lcsc: 'javascript:alert(1)',
          datasheet: 'javascript:alert(1)' },
    J1: { value: null, package: null, rating: null, lcsc: null },
  },
  nets: {
    VBUS: { pins: ['J1.1', 'C2.1'], net_class: 'power', current_a: 0.12, status: 'realized', reason: null },
    GND: { pins: ['J1.2', 'C2.2', 'R9.1', 'R9.2', 'X.1'], net_class: 'ground', current_a: null, status: 'realized', reason: null },
    CC2: { pins: ['J1.5', 'R9.1'], net_class: 'signal', current_a: null, status: 'failed', reason: 'no path on F.Cu' },
    SBU: { pins: ['J1.8', 'R9.2'], net_class: null, current_a: null, status: 'unrouted', reason: 'unrouted' },
  },
};

// pad: ref/(value package rating)/Pad n
assert.equal(F.describe(facts, { kind: 'pad', refdes: 'C2', pad: '2' }).text, 'C2/(10uF 0603 6.3V)/Pad 2');
// track: net/current/[a:b]; current omitted when unknown; N pins when multi-pin
assert.equal(F.describe(facts, { kind: 'net', net: 'VBUS' }).text, 'VBUS/120mA/[J1.1:C2.1]');
// every net says something about its current (gr496367): ground = computed return
assert.equal(F.describe(facts, { kind: 'net', net: 'GND' }).text,
  'GND/return ≤ 120mA (sum of power nets: VBUS 120mA; cascaded rails counted twice)/[5 pins]');
// failed net: the router's reason is shown verbatim; unrouted shows its status
assert.equal(F.describe(facts, { kind: 'ratsnest', net: 'CC2' }).text, 'CC2/signal, no current set/[J1.5:R9.1]/FAILED: no path on F.Cu');
assert.equal(F.describe(facts, { kind: 'ratsnest', net: 'SBU' }).text, 'SBU/current: not set/[J1.8:R9.2]/unrouted');
// ground sums supply nets, names the unset ones, and says so when none is set
const rails = (a, b) => ({ nets: {
  GND: { pins: [], net_class: 'ground', current_a: null },
  V5: { pins: [], net_class: 'power', current_a: a },
  V3: { pins: [], net_class: 'power', current_a: b },
  SIG: { pins: [], net_class: 'signal', current_a: 2 } } });
assert.equal(F.netText(rails(0.5, 0.1), 'GND'), 'GND/return ≤ 600mA (sum of power nets: V5 500mA + V3 100mA; cascaded rails counted twice)/[0 pins]');
assert.equal(F.netText(rails(0.5, null), 'GND'),
  'GND/return ≤ 500mA (sum of power nets: V5 500mA; cascaded rails counted twice; current not set: V3)/[0 pins]');
assert.equal(F.netText(rails(null, null), 'GND'),
  'GND/return current: unknown (no supply net has a current set; V5, V3)/[0 pins]');
assert.equal(F.netText(rails(0.5, 0.1), 'V3'), 'V3/100mA/[0 pins]');
assert.equal(F.netText(rails(0.5, null), 'V3'), 'V3/current: not set/[0 pins]');
assert.equal(
  F.describe(facts, { kind: 'pad', refdes: 'J1', pad: '5', net: 'CC2' }).text,
  'J1/Pad 5/CC2 FAILED: no path on F.Cu');

// gr520542: a track segment reads as the net's line + the segment spec
assert.equal(
  F.trackText(facts, { net: 'VBUS', layer: 'F.Cu', width_mm: 0.25 }),
  'VBUS/120mA/[J1.1:C2.1]/track 0.25mm F.Cu');
assert.equal(
  F.trackText(facts, { net: 'CC2', layer: 'B.Cu', width_mm: null }),
  'CC2/signal, no current set/[J1.5:R9.1]/track B.Cu/FAILED: no path on F.Cu');
assert.equal(F.trackText(facts, { net: null, layer: 'F.Cu', width_mm: 0.3 }),
  'no net/track 0.3mm F.Cu');

// vias: net, drill, pad, layers, stitching only on a ground net (gr496368)
const via = { net: 'GND', drill_mm: 0.3, dia_mm: 0.6, layers: ['F.Cu', 'B.Cu'], fixed: false };
assert.equal(F.viaText(facts, via), 'GND via/drill 0.3mm/pad 0.6mm/F.Cu-B.Cu/stitching');
assert.equal(
  F.viaText(facts, { ...via, net: 'VBUS', drill_mm: null, fixed: true }),
  'VBUS via/drill unknown/pad 0.6mm/F.Cu-B.Cu/not stitching/fixed');
assert.equal(F.viaText(facts, { ...via, net: null }), 'no net via/drill 0.3mm/pad 0.6mm/F.Cu-B.Cu/not stitching');
assert.equal(F.isGround({ nets: { X: { net_class: 'ground' } } }, 'X'), true, 'class beats name');
assert.equal(F.isGround({ nets: { GND: { net_class: 'power' } } }, 'GND'), false, 'class beats name');
// silkscreen: the owning part's tooltip, else the layer (gr520541: the
// parenthesised label form dropped; a value repeating the refdes's class
// letter loses that token — 'C 1.0pF' on C3 reads '1.0pF')
const silkC2 = { role: 'refdes', layer: 'F_Silkscreen', anchor: { kind: 'instance', refdes: 'C2', instance_id: 1 } };
assert.equal(F.silkText(facts, silkC2), 'C2/10uF 0603 6.3V/silk refdes');
const silkC3 = { role: 'outline', layer: 'F_Silkscreen', anchor: { kind: 'instance', refdes: 'C3', instance_id: 2 } };
assert.equal(F.silkText(facts, silkC3), 'C3/1.0pF/silk outline');
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
// gr520537: a stored datasheet rides describe as a link — http(s) only
assert.equal(F.describe(facts, { kind: 'part', refdes: 'C2' }).datasheet,
  'https://datasheet.lcsc.com/lcsc/C19702.pdf');
assert.equal(F.describe(facts, { kind: 'pad', refdes: 'C2', pad: '1' }).datasheet,
  'https://datasheet.lcsc.com/lcsc/C19702.pdf', 'pad and part agree');
assert.equal(F.describe(facts, { kind: 'part', refdes: 'R9' }).datasheet, null,
  'a non-web datasheet URL is dropped');
assert.equal(F.describe(facts, { kind: 'part', refdes: 'J1' }).datasheet, null);
// gr520540: a traced placement's board-script site rides describe as
// {file, line, ws} — null when the trace names no maker
assert.deepEqual(F.describe(facts, { kind: 'part', refdes: 'C2' }).script,
  { file: 'main.py', line: 4, ws: 'leaf-a' });
assert.deepEqual(F.describe(facts, { kind: 'pad', refdes: 'C2', pad: '1' }).script,
  { file: 'main.py', line: 4, ws: 'leaf-a' }, 'pad and part agree');
assert.equal(F.describe(facts, { kind: 'part', refdes: 'J1' }).script, null);
// user text is returned as text, never altered into markup
assert.ok(F.describe(facts, { kind: 'part', refdes: 'R9' }).text.includes('<img src=x onerror=alert(1)>'));

// load(): metadata JSON, tolerant of absence and garbage
const doc = (txt) => ({ getElementById: () => (txt === undefined ? null : { textContent: txt }) });
assert.deepEqual(F.load(doc(undefined)), { parts: {}, nets: {} });
assert.deepEqual(F.load(doc('{not json')), { parts: {}, nets: {} });
assert.equal(F.load(doc(JSON.stringify(facts))).nets.CC2.status, 'failed');
console.log('ok');
