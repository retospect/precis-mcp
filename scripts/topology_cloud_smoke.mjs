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
//
// `fork` (B3) and `fork_arm` (B4) are both OPENED parents (kind 'shape',
// each has a shown child) — the two nodes defect 1 stops drawing as
// free-floating circles. `fork_tip` (B5) is a genuine leaf, kind 'box'
// only because that is what a COLLAPSED parent looks like when it has no
// children of its own to open onto — it still gets a normal circle.

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
// Defect 3 — a finding on an OPENED parent (fork, filed under its own
// name even though the block is drawn as a hull, not a circle) and one
// on a plain leaf (fork_tip), one of each severity so both badge colours
// get exercised.
const findings = {
  fork: [{ severity: 'error', rule: 'dangling_connect', detail: 'block foo no longer exists' }],
  fork_tip: [{ severity: 'warn', rule: 'unconnected_port', detail: 'no live connect references this port' }],
};
const OPENED_PARENTS = ['B3', 'B4']; // fork, fork_arm

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

// defect 3 — findings ride the SAME tooltip builder, severity-prefixed
const findingLines = nodeTooltipLines(nodes[4], 0, findings.fork_tip);
check(
  findingLines.includes(
    'warn: unconnected_port — no live connect references this port'
  ),
  'a finding line is severity-prefixed with rule and detail'
);
check(
  nodeTooltipLines(nodes[0], 1).length === nodeTooltipLines(nodes[0], 1, []).length,
  'no findings adds no lines (an empty array is the honest default)'
);

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
  findings,
  onSelect: (id) => selected.push(id),
  doc,
});

const svg = container.children.find((c) => c.tagName === 'svg');
check(!!svg, 'the cloud built an <svg> into the container');

// defect 1 — an opened parent (fork, fork_arm) draws NO circle of its
// own; only the 3 leaves (hub, rim, fork_tip) do. Badge circles (r=3,
// defect 3) are excluded from this count on purpose — they are not node
// glyphs.
const glyphCircles = svg.byTag('circle').filter((c) => c.getAttribute('r') !== '3');
check(
  glyphCircles.length === nodes.length - OPENED_PARENTS.length,
  `opened parents draw no circle of their own (expected ${
    nodes.length - OPENED_PARENTS.length
  } node circles, got ${glyphCircles.length})`
);
const forkLabel = svg.byTag('text').find((t) => t.textContent === 'fork');
check(!!forkLabel, "an opened parent's name renders as the hull's own label");
check(
  forkLabel.attrs['font-style'] === 'italic',
  'the hull label is styled as a container label (italic), not a node label'
);
check(
  svg.byTag('text').map((t) => t.textContent).includes('fork_arm'),
  'nodes are labelled by name — including an opened parent, via its hull'
);
check(svg.byTag('polygon').length === OPENED_PARENTS.length, 'one hull per opened parent');

// defect 2 — containment is drawn (dashed, thin, unhoverable), separate
// from the solid coloured connect lines.
const allLines = svg.byTag('line');
const connectLines = allLines.filter((l) => !l.attrs['stroke-dasharray']);
const containLines = allLines.filter((l) => l.attrs['stroke-dasharray']);
check(connectLines.length === connections.length, 'one solid line per connect');
check(
  containLines.length === nodes.filter((n) => n.parent).length,
  'one dashed containment line per parent link'
);
check(
  containLines.every((l) => !(l.handlers.mousemove || []).length),
  'containment lines carry no hover handler — not hoverable as connects'
);
check(
  containLines.every((l) => l.attrs['pointer-events'] === 'none'),
  'containment lines do not intercept pointer events either'
);
for (const line of allLines) {
  check(
    ['x1', 'y1', 'x2', 'y2'].every((k) => Number.isFinite(Number(line.getAttribute(k)))),
    'every line (connect or containment) got finite endpoints'
  );
}
for (const g of svg.byTag('g').filter((g) => g.attrs.transform)) {
  check(/^translate\(-?\d/.test(g.attrs.transform), 'node groups are positioned');
}

// defect 3 — a badge on the opened-parent hull (error, red) and on a
// plain leaf (warn, amber).
const badges = svg.byTag('circle').filter((c) => c.getAttribute('r') === '3');
check(badges.length === 2, `one badge per node carrying a finding (got ${badges.length})`);
check(
  badges.some((c) => c.getAttribute('fill') === '#dc2626'),
  'an error-tier finding gets a red badge'
);
check(
  badges.some((c) => c.getAttribute('fill') === '#f59e0b'),
  'a warn-tier finding gets an amber badge'
);

// click → onSelect drives the 3D selection: a plain node via its circle's
// `g`, an opened parent via its hull polygon (defect 1's "keep it
// clickable" requirement).
const nodeClickTargets = svg.byTag('g').filter((g) => (g.handlers.click || []).length);
const hullClickTargets = svg.byTag('polygon').filter((p) => (p.handlers.click || []).length);
check(
  nodeClickTargets.length + hullClickTargets.length === nodes.length,
  'every node is clickable, whether via its own circle or its hull'
);
check(hullClickTargets.length === OPENED_PARENTS.length, 'each opened parent is clickable via its hull');
nodeClickTargets[0].fire('click');
check(selected.length === 1, 'clicking a node calls onSelect once');
check(/^B\d+$/.test(selected[0]), `onSelect passes the B<uid> node id (got ${selected[0]})`);
hullClickTargets[0].fire('click');
check(
  selected.includes('B3'),
  "clicking an opened parent's hull selects that block, exactly as clicking its circle used to"
);

// highlight paints exactly one node and clears the previous one — for a
// plain node, via its circle; for an opened parent (no circle), via its
// hull's stroke instead (defect 1).
const paintedCircles = () =>
  svg.byTag('circle').filter((c) => c.getAttribute('fill') === '#f59e0b' && c.getAttribute('r') !== '3');
const highlightedHulls = () =>
  svg.byTag('polygon').filter((p) => p.getAttribute('stroke') === '#f59e0b');
cloud.highlight('B2');
check(paintedCircles().length === 1, 'highlight paints exactly one node circle');
check(highlightedHulls().length === 0, 'no hull is highlighted for a plain-node selection');
cloud.highlight('B3'); // fork — opened parent, no circle
check(paintedCircles().length === 0, 'highlighting an opened parent clears the previous circle highlight');
check(
  highlightedHulls().length === 1,
  "highlighting an opened parent highlights its hull's stroke instead of a circle"
);
cloud.highlight('B2');
check(highlightedHulls().length === 0, 'highlighting a plain node clears a previous hull highlight');
check(paintedCircles().length === 1, 'and repaints the circle highlight');

// hover fills the tooltip with text (never markup) and leaving hides it
const tip = container.children.find((c) => c.tagName === 'div');
check(!!tip, 'a tooltip element exists');
nodeClickTargets[0].fire('mousemove', { offsetX: 10, offsetY: 10 });
check(tip.textContent.length > 0, 'hovering a node fills the tooltip');
check(!tip.classList.contains('hidden'), 'hovering a node shows the tooltip');
nodeClickTargets[0].fire('mouseleave');
check(tip.classList.contains('hidden'), 'leaving a node hides the tooltip');

// defect 3 — an opened parent's hull hover lists ITS OWN findings too
hullClickTargets[0].fire('mousemove', { clientX: 5, clientY: 5 });
check(
  tip.textContent.includes('error: dangling_connect'),
  "an opened parent's hull hover lists its own validator findings"
);
hullClickTargets[0].fire('mouseleave');

// drag pins, double-click releases
const posOf = (id) => cloud.positions.get(id);
nodeClickTargets[0].fire('pointerdown', {});
check(posOf(nodes[0].id).pinned === true, 'pointerdown pins the dragged node');
nodeClickTargets[0].fire('dblclick', {});
check(posOf(nodes[0].id).pinned === false, 'double-click releases the pin');

if (failures) {
  console.error(`topology-cloud smoke: ${failures} failure(s)`);
  process.exit(1);
}
console.log('topology-cloud smoke OK');
process.exit(0);
