#!/usr/bin/env node
// Client-render smoke test for the topology cloud
// (static/topology-cloud.js, spec slice 1 of
// docs/backlog/se-topology-cloud-and-surface-notes.md).
//
// Usage: node scripts/topology_cloud_smoke.mjs
// Exits 0 when every check passes, 1 on the first failure.
// Invoked by tests/test_topology_cloud_js.py, which skips when node is
// unavailable — the same shape as scripts/cad_tessellate_parity.mjs.
//
// Why a hand-rolled DOM stub rather than jsdom/Playwright: this reader had
// NO client-JS coverage at all (the gap gr338976's mermaid race exposed),
// and the cheapest thing that closes it without adding a browser-sized
// dependency is a ~50-line element stub that records what the module
// built. It catches the class of bug that actually bit — "the code ran but
// the panel is empty" — plus wrong element counts, missing handlers and
// tooltip wording, which is what this module can get wrong.

import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const modPath = resolve(__dirname, '../src/precis_web/static/topology-cloud.js');
const {
  layoutTopology,
  convexHull,
  nodeTooltipLines,
  edgeTooltipLines,
  renderTopologyCloud,
} = await import(modPath);

let failures = 0;
function check(ok, msg) {
  if (!ok) {
    console.error('FAIL: ' + msg);
    failures++;
  }
}

// ── DOM stub ───────────────────────────────────────────────────────────

class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attrs = {};
    this.handlers = {};
    this.style = {};
    this.textContent = '';
    this.className = '';
    this._classes = new Set();
    this.classList = {
      add: (c) => this._classes.add(c),
      remove: (c) => this._classes.delete(c),
      contains: (c) => this._classes.has(c),
    };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren(...cs) { this.children = cs; }
  addEventListener(name, fn) { (this.handlers[name] ||= []).push(fn); }
  removeEventListener() {}
  fire(name, ev) { for (const fn of this.handlers[name] || []) fn(ev || {}); }
  descendants() {
    const out = [];
    for (const c of this.children) { out.push(c); out.push(...c.descendants()); }
    return out;
  }
  byTag(tag) { return this.descendants().filter((e) => e.tagName === tag); }
}

const doc = {
  createElement: (t) => new El(t),
  createElementNS: (_ns, t) => new El(t),
};

// ── fixture: hub—rim tie, plus an opened fork subtree ──────────────────

const nodes = [
  { id: 'B1', name: 'hub', path: '/se-x/1', parent: null, kind: 'shape',
    detail: ['envelope: cyl:r0.02h0.05'] },
  { id: 'B2', name: 'rim', path: '/se-x/2', parent: null, kind: 'shape', detail: [] },
  { id: 'B3', name: 'fork', path: '/se-x/3', parent: null, kind: 'shape', detail: [] },
  { id: 'B4', name: 'fork_arm', path: '/se-x/3/4', parent: 'B3', kind: 'shape', detail: [] },
  { id: 'B5', name: 'fork_tip', path: '/se-x/3/4/5', parent: 'B4', kind: 'box', detail: [] },
];
const connections = [
  { path: '/se-x/_connections/c0', a_name: 'hub', b_name: 'rim',
    a_path: '/se-x/1', b_path: '/se-x/2', label: 'hub.pin—rim.pin (axial)',
    colour: '#16a34a', subject: 'hub.pin—rim.pin', witness_gap: 0.25 },
];
const forces = {
  'hub.pin—rim.pin': { role: 'tie', self_stress: 0.5, declared_n: 120.0 },
};

// ── 1. layout is deterministic, on-canvas, and non-degenerate ──────────

const W = 600;
const H = 520;
const edges = [{ a: 'B1', b: 'B2' }];
const runA = layoutTopology({ nodes, edges, width: W, height: H });
const runB = layoutTopology({ nodes, edges, width: W, height: H });
check(runA.length === nodes.length, 'layout returns one point per node');
for (let i = 0; i < runA.length; i++) {
  check(
    Math.abs(runA[i].x - runB[i].x) < 1e-9 && Math.abs(runA[i].y - runB[i].y) < 1e-9,
    `layout is deterministic (node ${runA[i].id} drifted between runs)`
  );
  check(
    Number.isFinite(runA[i].x) && Number.isFinite(runA[i].y),
    `layout produced a finite position for ${runA[i].id}`
  );
  check(
    runA[i].x >= 0 && runA[i].x <= W && runA[i].y >= 0 && runA[i].y <= H,
    `node ${runA[i].id} stayed on the canvas`
  );
}
// No two nodes collapse onto each other — the repulsion term's whole job.
for (let i = 0; i < runA.length; i++) {
  for (let j = i + 1; j < runA.length; j++) {
    const dx = runA[i].x - runA[j].x;
    const dy = runA[i].y - runA[j].y;
    check(
      Math.sqrt(dx * dx + dy * dy) > 14,
      `nodes ${runA[i].id}/${runA[j].id} overlap after relaxation`
    );
  }
}

// Connected pairs end closer than unconnected ones — the property that
// makes the cloud readable at all.
const at = (id) => runA.find((p) => p.id === id);
const dist = (a, b) => Math.hypot(at(a).x - at(b).x, at(a).y - at(b).y);
check(dist('B1', 'B2') < dist('B1', 'B3'), 'a connected pair sits closer than an unconnected one');
check(dist('B3', 'B4') < dist('B2', 'B4'), 'a parent sits closer to its child than a stranger does');

// ── 2. convex hull ─────────────────────────────────────────────────────

const hull = convexHull([
  { x: 0, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 10 }, { x: 0, y: 10 }, { x: 5, y: 5 },
]);
check(hull.length === 4, `hull drops interior points (got ${hull.length})`);

// ── 3. tooltip wording — the honesty rule ──────────────────────────────

const nodeLines = nodeTooltipLines(nodes[0], 1);
check(nodeLines[0] === 'hub', 'node tooltip leads with the block name');
check(nodeLines.includes('envelope: cyl:r0.02h0.05'), 'node tooltip carries declared detail');
check(nodeLines.includes('1 connect'), 'node tooltip counts connects, singular');

const withForce = edgeTooltipLines(connections[0], forces['hub.pin—rim.pin']);
check(withForce.some((l) => l.includes('120') && l.includes('N')), 'edge tooltip shows the declared preload in N');
check(
  withForce.some((l) => l.includes('self-stress coefficient') && l.includes('normalized')),
  'the self-stress coefficient is labelled as a coefficient, not newtons'
);
const noForce = edgeTooltipLines(connections[0], undefined);
check(
  noForce.some((l) => l.includes('no stability solve')),
  'with no solve the tooltip says so rather than printing a number'
);
check(
  !noForce.some((l) => /\d+(\.\d+)? N/.test(l)),
  'with no solve NO newton figure is invented'
);

// ── 4. the render actually builds a panel ──────────────────────────────

const container = new El('div');
container.clientWidth = W;
container.clientHeight = H;
const selected = [];
const cloud = renderTopologyCloud({
  container,
  nodes,
  connections,
  forces,
  onSelect: (id) => selected.push(id),
  doc,
});

const svg = container.children.find((c) => c.tagName === 'svg');
check(!!svg, 'the cloud built an <svg> into the container');
check(svg.byTag('circle').length === nodes.length, 'one circle per node');
check(svg.byTag('line').length === connections.length, 'one line per connect');
check(
  svg.byTag('text').map((t) => t.textContent).includes('fork_arm'),
  'nodes are labelled by name'
);
check(svg.byTag('polygon').length >= 1, 'an opened parent draws a hull behind its children');
for (const line of svg.byTag('line')) {
  check(
    ['x1', 'y1', 'x2', 'y2'].every((k) => Number.isFinite(Number(line.getAttribute(k)))),
    'every edge got finite endpoints (the panel is not blank)'
  );
}
for (const g of svg.byTag('g').filter((g) => g.attrs.transform)) {
  check(/^translate\(-?\d/.test(g.attrs.transform), 'node groups are positioned');
}

// click → onSelect drives the 3D selection
const nodeGroups = svg.byTag('g').filter((g) => (g.handlers.click || []).length);
check(nodeGroups.length === nodes.length, 'every node is clickable');
nodeGroups[0].fire('click');
check(selected.length === 1, 'clicking a node calls onSelect once');
check(/^B\d+$/.test(selected[0]), `onSelect passes the B<uid> node id (got ${selected[0]})`);

// highlight paints exactly one node and clears the previous one
cloud.highlight('B2');
const painted = () => svg.byTag('circle').filter((c) => c.getAttribute('fill') === '#f59e0b');
check(painted().length === 1, 'highlight paints exactly one node');
cloud.highlight('B3');
check(painted().length === 1, 'highlighting a second node clears the first');

// hover fills the tooltip with text (never markup) and leaving hides it
const tip = container.children.find((c) => c.tagName === 'div');
check(!!tip, 'a tooltip element exists');
nodeGroups[0].fire('mousemove', { offsetX: 10, offsetY: 10 });
check(tip.textContent.length > 0, 'hovering a node fills the tooltip');
check(!tip.classList.contains('hidden'), 'hovering a node shows the tooltip');
nodeGroups[0].fire('mouseleave');
check(tip.classList.contains('hidden'), 'leaving a node hides the tooltip');

// drag pins, double-click releases
const posOf = (id) => cloud.positions.get(id);
nodeGroups[0].fire('pointerdown', {});
check(posOf(nodes[0].id).pinned === true, 'pointerdown pins the dragged node');
nodeGroups[0].fire('dblclick', {});
check(posOf(nodes[0].id).pinned === false, 'double-click releases the pin');

if (failures) {
  console.error(`topology-cloud smoke: ${failures} failure(s)`);
  process.exit(1);
}
console.log('topology-cloud smoke OK');
process.exit(0);
