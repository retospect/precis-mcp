// Node smoke for the board page's viewer glue — pcb-crossprobe.js,
// pcb-hover.js and pcb-sheets.js wired over a hand-rolled DOM (pattern:
// scripts/pcb_viewport_smoke.mjs). Run by tests/test_pcb_viewer_js.py;
// skips there when node is absent.
//
// Cases:
//   1. gr520791 — a 'pcb-selection-changed' for a layout-only object
//      (board outline, key 'outline:220') used to freeze the pane:
//      selRefdeses recursed onto the SAME handle forever (a strict-mode
//      tail call, so Safari never even logged a RangeError).
//   2. gr520541 — a hover left the info strip's 'hovering' class stuck
//      (hiding the selection behind it) when the pointer exited via a
//      fast leave or a scroll; a window scroll now clears it.
//   3. gr520541 — the position:fixed hover tip outlived the board under
//      a page scroll; the same scroll hides it.

import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const stat = (n) => resolve(here, '../src/precis_web/static/' + n);

// ── fake DOM ────────────────────────────────────────────────────────

function classList() {
  const s = new Set();
  return {
    add: (c) => s.add(c),
    remove: (c) => s.delete(c),
    contains: (c) => s.has(c),
    toggle: (c, f) => {
      (f === undefined ? !s.has(c) : f) ? s.add(c) : s.delete(c);
    },
  };
}

function elem(tag) {
  return {
    tagName: tag,
    id: '',
    attrs: {},
    dataset: {},
    style: {},
    classList: classList(),
    children: [],
    textContent: '',
    setAttribute(k, v) {
      this.attrs[k] = String(v);
    },
    getAttribute(k) {
      return this.attrs[k] === undefined ? null : this.attrs[k];
    },
    hasAttribute(k) {
      return this.attrs[k] !== undefined;
    },
    appendChild(c) {
      this.children.push(c);
      return c;
    },
    remove() {},
    querySelectorAll() {
      return [];
    },
    querySelector() {
      return null;
    },
    closest() {
      return null;
    },
  };
}

// An <object>-pane stub: listener registry + a settable svgDocument.
function objectEl() {
  const e = elem('object');
  e.listeners = {};
  e.contentDocument = null;
  e.addEventListener = function (t, f) {
    (this.listeners[t] = this.listeners[t] || []).push(f);
  };
  e.getBoundingClientRect = () => ({ left: 0, top: 0, width: 200, height: 100 });
  return e;
}

function svgDoc(byId) {
  const d = {
    listeners: {},
    _els: [],
    byId: byId || {},
    getElementById(id) {
      return this.byId[id] || null;
    },
    addEventListener(t, f) {
      (this.listeners[t] = this.listeners[t] || []).push(f);
    },
    querySelectorAll() {
      return [];
    },
    querySelector() {
      return null;
    },
    elementsFromPoint() {
      return this._els;
    },
    createElementNS(_ns, tag) {
      return elem(tag);
    },
    documentElement: elem('svg'),
  };
  d.documentElement.addEventListener = () => {};
  return d;
}

function fire(target, type, ev) {
  ev = ev || { type };
  ev.type = ev.type || type;
  for (const f of target.listeners[type] || []) f(ev);
}

// globals the modules read at eval time
const winListeners = {};
globalThis.window = globalThis;
globalThis.addEventListener = (t, f) => {
  (winListeners[t] = winListeners[t] || []).push(f);
};
globalThis.dispatchEvent = (ev) => {
  for (const f of winListeners[ev.type] || []) f(ev);
};
globalThis.CustomEvent = class {
  constructor(type, init) {
    this.type = type;
    this.detail = init && init.detail;
  }
};
globalThis.CSS = { escape: (s) => String(s).replace(/["\\]/g, '\\$&') };

const ids = {};
const docListeners = {};
globalThis.document = {
  getElementById: (id) => ids[id] || null,
  createElement: (tag) => elem(tag),
  body: elem('body'),
  addEventListener: (t, f) => {
    (docListeners[t] = docListeners[t] || []).push(f);
  },
  dispatchEvent: (ev) => {
    for (const f of docListeners[ev.type] || []) f(ev);
  },
};

const facts = {
  parts: {
    U1: { value: '10k', package: '0402', rating: null, lcsc: null },
    R1: { value: '4k7', package: '0402', rating: null, lcsc: null },
  },
  nets: {
    SIG: { pins: ['U1.1', 'R1.1'], net_class: 'signal' },
  },
};
const objects = [
  { key: 'outline:220', kind: 'outline' },
  { key: 'inst:1', kind: 'instance', refdes: 'U1' },
];

const boardDoc = svgDoc({
  'pcb-facts': { textContent: JSON.stringify(facts) },
  'pcb-objects': { textContent: JSON.stringify(objects) },
});
ids['pcb-info-detail'] = elem('div'); // the strip
ids['pcb-info-hover'] = elem('span');
ids['pcb-info-selection'] = elem('span');
ids['pcb-cursor-readout'] = elem('span');
ids['pcb-part-cards'] = { textContent: '{}' };
ids['pcb-sheet-map'] = { textContent: JSON.stringify({ U1: 'hdr' }) };
ids['pcb-board-object'] = objectEl();
ids['pcb-board-object'].dataset.base = '/pcb/web_pcb';
ids['pcb-board-object'].contentDocument = boardDoc;
ids['pcb-schematic-object'] = objectEl();
ids['pcb-schematic-object'].dataset.base = '/pcb/web_pcb/schematic.svg';
const strip = ids['pcb-info-detail'];
const hoverEl = ids['pcb-info-hover'];
const schemObj = ids['pcb-schematic-object'];

// Real modules, load order as detail.html.j2 (facts before consumers).
globalThis.PcbFacts = require(stat('pcb-facts.js'));
require(stat('pcb-sheets.js'));
require(stat('pcb-crossprobe.js'));
require(stat('pcb-hover.js'));

// ── 1. gr520791: a layout-only selection returns, and a net selection
//       still cross-probes onto its sheet ────────────────────────────
assert.doesNotThrow(() =>
  document.dispatchEvent(
    new CustomEvent('pcb-selection-changed', {
      detail: { object: { kind: 'outline', key: 'outline:220' } },
    })
  ),
  'outline click must not recurse forever (RangeError before the fix)');
assert.equal(schemObj.attrs.data, undefined, 'a layout-only object switches no sheet');

// …and the same path on a real object: net:SIG's members (U1 on sheet
// 'hdr', R1 ungrouped) switch the pane to 'hdr'.
document.dispatchEvent(
  new CustomEvent('pcb-selection-changed', {
    detail: { object: { kind: 'net', name: 'SIG', key: 'net:SIG' } },
  })
);
assert.equal(
  schemObj.attrs.data,
  '/pcb/web_pcb/schematic.svg?sheet=hdr',
  'net selection cross-probes onto the member sheet');
schemObj.dataset.sheet = ''; // reset for the next cases
schemObj.attrs.data = undefined;

// ── 2. gr520541: a scroll clears the strip's stuck 'hovering' ───────
const handleEl = { dataset: {}, getAttribute: () => 'U1' };
boardDoc._els = [
  {
    dataset: {},
    closest: (sel) => (sel === '[data-handle]' ? handleEl : null),
  },
];
fire(boardDoc, 'mousemove', { clientX: 1, clientY: 1 });
assert(strip.classList.contains('hovering'), 'hover marks the strip');
assert.equal(hoverEl.textContent, 'U1/(10k 0402)');
globalThis.dispatchEvent({ type: 'scroll' });
assert(!strip.classList.contains('hovering'), 'scroll clears the stuck hover');
assert.equal(hoverEl.textContent, '');

// ── 3. gr520541: the hover tip shows on mousemove, hides on scroll ──
boardDoc._els = [{ dataset: { pcbNet: 'SIG' }, closest: () => null }];
const tip = document.body.children.find((c) => c.id === 'pcb-hover-tip');
assert(tip, 'pcb-hover.js mounted its tip div');
assert.equal(tip.style.display, 'none');
fire(boardDoc, 'mousemove', { clientX: 5, clientY: 6 });
assert.equal(tip.style.display, 'block', 'mousemove shows the tip');
assert(tip.textContent.startsWith('SIG/'), `tip text: ${tip.textContent}`);
globalThis.dispatchEvent({ type: 'scroll' });
assert.equal(tip.style.display, 'none', 'scroll hides the fixed tip');
assert(!strip.classList.contains('hovering'), '…and the strip is clean');

console.log('pcb viewer smoke: ok');
