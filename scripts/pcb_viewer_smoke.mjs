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
//   2. gr520541 — a hover left the panel's 'hovering' class stuck
//      (hiding the selection behind it) when the pointer exited via a
//      fast leave or a scroll; a window scroll now clears it. (gr520537:
//      the display moved into #pcb-selection-panel — the old
//      #pcb-info-strip is gone.)
//   3. gr520541 — the position:fixed hover tip outlived the board under
//      a page scroll; the same scroll hides it.
//   4. gr520537 — the cursor readout is a <text> inside the board
//      document's layer legend; pcb-hover.js binds it from the svgDoc.
//   5. pcb-sheets.js — ?only= rides the schematic URL with sheet=.
//   6. pcb-focus.js — a part dblclick enters focus (?focus=), the
//      'only connected' button toggles ?only= alongside it, and exit
//      clears both (gr520807).
//   7. gr531205 — selecting a net pins its ratsnest isolate (solo);
//      deselect clears it.
//   8. gr531205 — hovering an airwire isolates its net (transient);
//      a scroll clears it.
//   9. gr531205 — the pcb-workbench.js ratsnest master toggle flips
//      rs-off on the board document and the legend row, remembers per
//      user, and disables itself on a fully-routed board.

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
    listeners: {},
    textContent: '',
    addEventListener(t, f) {
      (this.listeners[t] = this.listeners[t] || []).push(f);
    },
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
  'pcb-cursor-readout': elem('text'),
});
ids['pcb-selection-panel'] = elem('div'); // the one selection display
ids['pcb-selection-hover'] = elem('div');
ids['pcb-selection-identity'] = elem('pre');
ids['pcb-part-cards'] = { textContent: '{}' };
ids['pcb-sheet-map'] = { textContent: JSON.stringify({ U1: 'hdr' }) };
ids['pcb-board-object'] = objectEl();
ids['pcb-board-object'].dataset.base = '/pcb/web_pcb';
ids['pcb-board-object'].contentDocument = boardDoc;
ids['pcb-schematic-object'] = objectEl();
ids['pcb-schematic-object'].dataset.base = '/pcb/web_pcb/schematic.svg';
const schemDoc = svgDoc();
ids['pcb-schematic-object'].contentDocument = schemDoc;
ids['pcb-focus-bar'] = elem('span');
ids['pcb-focus-label'] = elem('span');
ids['pcb-focus-pin'] = elem('button');
ids['pcb-focus-only'] = elem('button');
ids['pcb-focus-exit'] = elem('button');
const panel = ids['pcb-selection-panel'];
const hoverEl = ids['pcb-selection-hover'];
const schemObj = ids['pcb-schematic-object'];

// Real modules, load order as detail.html.j2 (facts before consumers).
globalThis.PcbFacts = require(stat('pcb-facts.js'));
require(stat('pcb-sheets.js'));
require(stat('pcb-focus.js'));
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

// ── 2. gr520541: a scroll clears the panel's stuck 'hovering' ──────
const handleEl = { dataset: {}, getAttribute: () => 'U1' };
boardDoc._els = [
  {
    dataset: {},
    closest: (sel) => (sel === '[data-handle]' ? handleEl : null),
  },
];
fire(boardDoc, 'mousemove', { clientX: 1, clientY: 1 });
assert(panel.classList.contains('hovering'), 'hover marks the panel');
assert.equal(hoverEl.textContent, 'U1/(10k 0402)');
globalThis.dispatchEvent({ type: 'scroll' });
assert(!panel.classList.contains('hovering'), 'scroll clears the stuck hover');
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
assert(!panel.classList.contains('hovering'), '…and the panel is clean');

// ── 4. gr520537: the cursor readout bound from the board document ──
const readout = boardDoc.getElementById('pcb-cursor-readout');
assert(readout, 'the legend carries the readout row');
assert.equal(readout.textContent, 'x — y — mm', 'bind wrote the idle text');

// ── 5. pcb-sheets.js: ?only= rides the schematic URL with the rest ──
schemObj.dataset.sheet = 'hdr';
window.PcbSchem.refresh();
assert.equal(schemObj.attrs.data, '/pcb/web_pcb/schematic.svg?sheet=hdr');
schemObj.dataset.only = '1';
window.PcbSchem.refresh();
assert.equal(
  schemObj.attrs.data,
  '/pcb/web_pcb/schematic.svg?sheet=hdr&only=1',
  'only= survives alongside sheet=');
delete schemObj.dataset.sheet;
delete schemObj.dataset.only;
window.PcbSchem.refresh();
assert.equal(schemObj.attrs.data, '/pcb/web_pcb/schematic.svg');

// ── 6. pcb-focus.js: dblclick focuses; 'only connected' toggles ?only= ──
const onlyBtn = ids['pcb-focus-only'];
const exitBtn = ids['pcb-focus-exit'];
const focusBar = ids['pcb-focus-bar'];
const partEl = {
  getAttribute: (a) => (a === 'data-pcb-part' ? 'U1' : null),
  hasAttribute: (a) => a === 'data-pcb-part',
};
fire(schemDoc, 'dblclick', {
  target: { closest: (sel) => (sel === '[data-pcb-part]' ? partEl : null) },
  preventDefault() {},
});
assert.equal(schemObj.dataset.focus, 'U1', 'dblclick enters focus');
assert.equal(schemObj.attrs.data, '/pcb/web_pcb/schematic.svg?focus=U1');
assert(focusBar.classList.contains('inline-flex'), 'focus bar shows');
fire(onlyBtn, 'click');
assert.equal(schemObj.dataset.only, '1', 'only button sets dataset.only');
assert.equal(onlyBtn.getAttribute('aria-pressed'), 'true');
assert.equal(
  schemObj.attrs.data,
  '/pcb/web_pcb/schematic.svg?focus=U1&only=1',
  'only= reloads with the focus');
fire(onlyBtn, 'click');
assert.equal(schemObj.dataset.only, undefined, 'second click clears only');
assert.equal(onlyBtn.getAttribute('aria-pressed'), 'false');
assert.equal(schemObj.attrs.data, '/pcb/web_pcb/schematic.svg?focus=U1');
fire(exitBtn, 'click');
assert.equal(schemObj.dataset.focus, undefined, 'exit clears the focus');
assert.equal(schemObj.attrs.data, '/pcb/web_pcb/schematic.svg');
assert(focusBar.classList.contains('hidden'), 'focus bar hides on exit');

// ── 7. gr531205: selecting a net pins its ratsnest isolate (solo) ────
const boardRoot = boardDoc.documentElement;
document.dispatchEvent(
  new CustomEvent('pcb-selection-changed', {
    detail: { object: { kind: 'net', name: 'SIG', key: 'net:SIG' }, text: '' },
  })
);
assert(boardRoot.classList.contains('rs-isolate'), 'a net selection solos its airwires');
document.dispatchEvent(
  new CustomEvent('pcb-selection-changed', { detail: { object: null, text: '' } })
);
assert(!boardRoot.classList.contains('rs-isolate'), 'deselect clears the solo');
schemObj.attrs.data = undefined;

// ── 8. gr531205: hovering an airwire isolates its net, transiently ───
boardDoc._els = [{ dataset: { pcbRatsnest: '1', pcbNet: 'SIG' }, closest: () => null }];
fire(boardDoc, 'mousemove', { clientX: 2, clientY: 2 });
assert(boardRoot.classList.contains('rs-isolate'), 'hover isolates the net');
globalThis.dispatchEvent({ type: 'scroll' });
assert(!boardRoot.classList.contains('rs-isolate'), 'scroll clears the hover isolate');

// ── 9. gr531205: the ratsnest master toggle (pcb-workbench.js) ───────
globalThis.localStorage = {
  _s: {},
  getItem(k) { return this._s[k] === undefined ? null : this._s[k]; },
  setItem(k, v) { this._s[k] = String(v); },
};
ids['pcb-workbench'] = elem('div');
ids['pcb-ratsnest-toggle'] = elem('button');
const rsGroup = elem('g');
const rsRow = elem('g');
boardDoc.byId['ratsnest'] = rsGroup;
boardDoc.byId['legend-ratsnest'] = rsRow;
require(stat('pcb-workbench.js'));
const rsBtn = ids['pcb-ratsnest-toggle'];
assert.equal(rsBtn.disabled, false, 'a board with airwires has a live toggle');
assert.equal(rsBtn.textContent, 'ratsnest: on', 'default ON');
assert(!boardRoot.classList.contains('rs-off'));
fire(rsBtn, 'click');
assert(boardRoot.classList.contains('rs-off'), 'click hides the airwires');
assert(rsRow.classList.contains('off'), '…and dims the legend row');
assert.equal(rsBtn.textContent, 'ratsnest: off');
assert.equal(localStorage.getItem('precis:pcb-workbench:ratsnest-hidden'), '1');
fire(rsBtn, 'click');
assert(!boardRoot.classList.contains('rs-off'), 'second click shows them again');
assert(!rsRow.classList.contains('off'));
delete boardDoc.byId['ratsnest'];
fire(ids['pcb-board-object'], 'load');
assert.equal(rsBtn.disabled, true, 'a fully-routed board disables the toggle');
assert.equal(rsBtn.textContent, 'ratsnest: none');
boardDoc.byId['ratsnest'] = rsGroup;

console.log('pcb viewer smoke: ok');
