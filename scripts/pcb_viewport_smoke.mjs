// Node smoke for static/viewport.js — the shared pan/zoom module behind
// the pcb workbench's two panes (docs/backlog/pcb-workbench-layout.md
// slice 1; se adopts the same module in slice 6). Run by
// tests/test_pcb_viewport_js.py; needs no browser: a Controller is
// driven over a fake svg/event-target, so wheel, pinch, drag, fit and
// reset are all exercised through their real event paths.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const V = require('../src/precis_web/static/viewport.js');

function fakeSvg() {
  const attrs = {viewBox: '0 0 200 100', width: '1600', height: '800'};
  return {
    attrs,
    tagName: 'svg',
    getAttribute(k) {
      return attrs[k] == null ? null : attrs[k];
    },
    setAttribute(k, v) {
      attrs[k] = String(v);
    },
    getBoundingClientRect() {
      return {left: 0, top: 0, width: 200, height: 100};
    },
  };
}

function fakeTarget() {
  return {
    nodeType: 9,
    listeners: {},
    addEventListener(t, f) {
      (this.listeners[t] = this.listeners[t] || []).push(f);
    },
    defaultView: null,
  };
}

function fire(target, type, ev) {
  ev.preventDefault = ev.preventDefault || (() => {});
  ev.stopImmediatePropagation =
    ev.stopImmediatePropagation || (() => {});
  ev.stopPropagation = ev.stopPropagation || (() => {});
  for (const f of target.listeners[type] || []) f(ev);
}

function viewBox(svg) {
  return V._parseBox(svg.getAttribute('viewBox'));
}

function rig() {
  const svg = fakeSvg();
  const doc = fakeTarget();
  const ctl = new V.Controller(svg, doc);
  return {svg, doc, ctl};
}

// ── wheel zooms toward the cursor ────────────────────────────────────
{
  const {svg, doc} = rig();
  fire(doc, 'wheel', {deltaY: -120, deltaX: 0, deltaMode: 0, clientX: 100, clientY: 50});
  const b = viewBox(svg);
  const f = Math.exp(120 * 0.0022);
  assert(Math.abs(b.w - 200 / f) < 0.01, `zoom factor: ${b.w}`);
  // the cursor's board point (100,50) stays under the cursor
  assert(Math.abs(b.x - (100 - 100 / f)) < 0.01, `zoom anchor x: ${b.x}`);
  assert(Math.abs(b.y - (50 - 50 / f)) < 0.01, `zoom anchor y: ${b.y}`);
}

// ── a trackpad two-finger scroll pans, ctrl+wheel (pinch) zooms ──────
{
  const {svg, doc} = rig();
  fire(doc, 'wheel', {deltaY: 8, deltaX: 12, deltaMode: 0, clientX: 0, clientY: 0});
  let b = viewBox(svg);
  assert(Math.abs(b.x - -12) < 0.01 && Math.abs(b.y - -8) < 0.01, `trackpad pan: ${b.x},${b.y}`);
  assert(b.w === 200, 'a two-finger scroll never zooms');
}
{
  const {svg, doc} = rig();
  fire(doc, 'wheel', {deltaY: -30, deltaX: 0, deltaMode: 0, ctrlKey: true, clientX: 100, clientY: 50});
  const b = viewBox(svg);
  assert(b.w < 200, 'ctrl+wheel (the pinch gesture) zooms');
}

// ── a left-drag on empty space pans and swallows the click ────────────
{
  const {svg, doc} = rig();
  fire(doc, 'pointerdown', {pointerId: 1, button: 0, clientX: 50, clientY: 40, target: {}});
  fire(doc, 'pointermove', {pointerId: 1, clientX: 80, clientY: 50});
  fire(doc, 'pointerup', {pointerId: 1, clientX: 80, clientY: 50});
  const b = viewBox(svg);
  assert(Math.abs(b.x - -30) < 0.01 && Math.abs(b.y - -10) < 0.01, `drag pan: ${b.x},${b.y}`);
  let stopped = false;
  fire(doc, 'click', {stopImmediatePropagation: () => { stopped = true; }});
  assert(stopped, 'a panned drag must not land as a select click');
}
{
  // …but a press that never travels still clicks through.
  const {doc} = rig();
  fire(doc, 'pointerdown', {pointerId: 1, button: 0, clientX: 50, clientY: 40, target: {}});
  fire(doc, 'pointerup', {pointerId: 1, clientX: 50, clientY: 40});
  let stopped = false;
  fire(doc, 'click', {stopImmediatePropagation: () => { stopped = true; }});
  assert(!stopped, 'a clean click is not suppressed');
}

// ── a drag ON an object is a click, not a pan ────────────────────────
{
  const {svg, doc} = rig();
  const handleEl = {closest: () => ({})};
  fire(doc, 'pointerdown', {pointerId: 1, button: 0, clientX: 50, clientY: 40, target: handleEl});
  fire(doc, 'pointermove', {pointerId: 1, clientX: 90, clientY: 40});
  fire(doc, 'pointerup', {pointerId: 1, clientX: 90, clientY: 40});
  const b = viewBox(svg);
  assert(b.x === 0 && b.w === 200, 'drag on a handle does not pan');
}
{
  // right/middle drag pans even over objects
  const {svg, doc} = rig();
  const handleEl = {closest: () => ({})};
  fire(doc, 'pointerdown', {pointerId: 1, button: 2, clientX: 50, clientY: 40, target: handleEl});
  fire(doc, 'pointermove', {pointerId: 1, clientX: 70, clientY: 40});
  fire(doc, 'pointerup', {pointerId: 1, clientX: 70, clientY: 40});
  assert(Math.abs(viewBox(svg).x - -20) < 0.01, 'right-drag pans over objects');
}

// ── a two-pointer pinch zooms about the midpoint ─────────────────────
{
  const {svg, doc} = rig();
  fire(doc, 'pointerdown', {pointerId: 1, button: 0, clientX: 50, clientY: 50, target: {}});
  fire(doc, 'pointerdown', {pointerId: 2, button: 0, clientX: 150, clientY: 50, target: {}});
  fire(doc, 'pointermove', {pointerId: 2, clientX: 190, clientY: 50});
  const b = viewBox(svg);
  assert(Math.abs(b.w - 200 / 1.4) < 0.5, `pinch zoom: ${b.w}`);
  fire(doc, 'pointerup', {pointerId: 1});
  fire(doc, 'pointerup', {pointerId: 2});
}

// ── double-click on empty space and reset both restore the base box ──
{
  const {svg, doc} = rig();
  fire(doc, 'wheel', {deltaY: -120, deltaX: 0, deltaMode: 0, clientX: 100, clientY: 50});
  fire(doc, 'dblclick', {target: {}});
  assert(svg.getAttribute('viewBox') === '0 0 200 100', 'double-click empty fits');
}
{
  const {svg, ctl} = rig();
  ctl.zoomBy(2, 100, 50);
  ctl.action('reset');
  assert(svg.getAttribute('viewBox') === '0 0 200 100', 'reset restores the base box');
  ctl.zoomBy(2, 100, 50);
  ctl.action('fit');
  assert(svg.getAttribute('viewBox') === '0 0 200 100', 'fit restores the base box');
}

// ── zoom clamps at the range ends ────────────────────────────────────
{
  const {svg, ctl} = rig();
  for (let i = 0; i < 40; i++) ctl.zoomBy(1.5);
  assert(viewBox(svg).w >= 200 / 64 - 1e-6, 'zoom-in clamp');
  for (let i = 0; i < 40; i++) ctl.zoomBy(0.5);
  assert(viewBox(svg).w <= 200 / 0.25 + 1e-6, 'zoom-out clamp');
}

// ── the layer legend stays pinned, fixed-size and hideable (gr497528) ──
{
  const legendAttrs = {};
  const legend = {
    style: {display: ''},
    setAttribute(k, v) {
      legendAttrs[k] = String(v);
    },
  };
  const svg = fakeSvg();
  svg.querySelector = (sel) => (sel === '.legend' ? legend : null);
  const ctl = new V.Controller(svg, fakeTarget());
  const t = () => legendAttrs.transform;
  // fitted: pane is 200x100 for a 200x100 box, so 1 viewBox unit = 1px
  assert(t() === 'translate(0 0) scale(1)', `fitted legend: ${t()}`);
  ctl.zoomBy(2, 100, 50);
  // zoomed 2x: the legend counter-scales to stay one CSS px per unit and
  // rides the pane's top-left corner (the new box origin)
  const b = viewBox(svg);
  assert(t() === `translate(${+b.x.toFixed(5)} ${+b.y.toFixed(5)}) scale(0.5)`,
    `zoomed legend keeps its size and corner: ${t()}`);
  ctl.panScreen(40, 20);
  const p = viewBox(svg);
  assert(t().startsWith(`translate(${+p.x.toFixed(5)} ${+p.y.toFixed(5)})`),
    `panned legend stays on the corner: ${t()}`);
  ctl.fit();
  assert(t() === 'translate(0 0) scale(1)', 'fit restores the pinned legend');
  ctl.setLegendHidden(true);
  assert(legend.style.display === 'none', 'legend hides');
  ctl.setLegendHidden(false);
  assert(legend.style.display === '', 'legend shows again');
}

// ── the wheel classifier itself ──────────────────────────────────────
assert(V._classifyWheel({deltaY: -120, deltaX: 0, deltaMode: 0}) === 'zoom');
assert(V._classifyWheel({deltaY: -120, deltaX: 0, deltaMode: 1}) === 'zoom');
assert(V._classifyWheel({deltaY: 4, deltaX: 9, deltaMode: 0}) === 'pan');
assert(V._classifyWheel({deltaY: 20, deltaX: 0, deltaMode: 0}) === 'pan');
assert(V._classifyWheel({deltaY: 20, deltaX: 0, deltaMode: 0, ctrlKey: true}) === 'zoom');

console.log('viewport.js smoke: ok');
