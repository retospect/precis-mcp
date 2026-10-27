/* pcb-hover.js — tooltip and cursor readout over the board <object>
   (gr495389, gr495391, gr496368).
   Pads, silkscreen (outlines and refdes text) show "C2/(10uF 0603 6.3V)/Pad 2",
   vias "GND via/drill 0.3mm/pad 0.6mm/F.Cu-B.Cu/stitching", tracks and ratsnest
   lines "NET/120mA/[R1.1:C2.2]" plus the router's failure reason. Text comes
   from the shared PcbFacts and is set with textContent. This tooltip is the
   only one: every SVG <title> (the native tooltips, including the document's
   "rendered from gerbers") is removed on bind. */
(() => {
  'use strict';
  const object = document.getElementById('pcb-board-object');
  if (!object || !window.PcbFacts) return;
  let svgDoc = null, facts = null, objects = new Map(), readout = null;
  const tip = document.createElement('div');
  tip.id = 'pcb-hover-tip';
  tip.setAttribute('role', 'tooltip');
  Object.assign(tip.style, {
    position: 'fixed', zIndex: '50', display: 'none', pointerEvents: 'none',
    maxWidth: '28rem', padding: '2px 6px', borderRadius: '3px', font: '12px ui-monospace, monospace',
    background: '#111827', color: '#f9fafb', whiteSpace: 'pre-wrap',
  });
  document.body.appendChild(tip);
  const IDLE = 'x — y — mm';

  const layerOff = (key) => {
    const g = key && svgDoc.getElementById(`layer-${key}`);
    return !!(g && g.classList.contains('off'));
  };
  const catalogue = () => {
    const meta = svgDoc.getElementById('pcb-objects');
    try { return new Map(JSON.parse(meta.textContent).map((o) => [o.key, o])); }
    catch (_) { return new Map(); }
  };

  // Every stacked node carrying board identity (hidden layers skipped; the
  // click overlay is transparent and sits on top), then the most specific wins:
  // ratsnest > pad > via > silkscreen > bare net.
  const targetAt = (x, y) => {
    const found = {};
    for (const n of svgDoc.elementsFromPoint(x, y)) {
      const d = n.dataset;
      if (!d) continue;
      const layer = n.closest && n.closest('.layer');
      if (layer && layer.classList.contains('off')) continue;
      if (d.pcbObject) {
        if (layerOff(d.pcbLayer)) continue;
        if (d.pcbKind === 'via' || d.pcbKind === 'silk') found[d.pcbKind] = found[d.pcbKind] || objects.get(d.pcbObject);
        continue;
      }
      if (d.pcbRatsnest) found.ratsnest = found.ratsnest || {kind: 'ratsnest', net: d.pcbNet};
      else if (d.pcbPart && d.pcbPad) found.pad = found.pad || {kind: 'pad', refdes: d.pcbPart, pad: d.pcbPad, net: d.pcbNet};
      else if (d.pcbNet) found.net = found.net || {kind: 'net', net: d.pcbNet};
    }
    return found;
  };
  const describeTarget = (target) => window.PcbFacts.describe(facts, target).text;
  const textFor = (found) => {
    if (found.ratsnest) return describeTarget(found.ratsnest);
    if (found.pad) return describeTarget(found.pad);
    if (found.via) return window.PcbFacts.viaText(facts, found.via);
    if (found.silk) return window.PcbFacts.silkText(facts, found.silk);
    if (found.net) return describeTarget(found.net);
    return '';
  };

  // Client point -> board mm through the board group's transform (the y flip
  // and the legend gutter live there, so no constants are repeated here).
  const boardGroup = () => svgDoc.documentElement.querySelector(':scope > g[transform]');
  const showCursor = (event) => {
    if (!readout) return;
    const g = boardGroup(), m = g && g.getScreenCTM && g.getScreenCTM();
    if (!m) { readout.textContent = IDLE; return; }
    const p = new DOMPoint(event.clientX, event.clientY).matrixTransform(m.inverse());
    readout.textContent = `x ${p.x.toFixed(2)}  y ${p.y.toFixed(2)} mm`;
  };

  const hide = () => { tip.style.display = 'none'; };
  // The tip is position:fixed — a page scroll slides the board out from
  // under it while no mouse event fires, so it stayed stuck in space
  // (gr520541). Capture scroll anywhere and hide; likewise leaving the
  // <object> itself (the in-document mouseleave misses a fast exit or a
  // scroll-out).
  window.addEventListener('scroll', hide, {capture: true, passive: true});
  object.addEventListener('mouseleave', hide);
  const bind = () => {
    try { svgDoc = object.contentDocument; } catch (_) { svgDoc = null; }
    if (!svgDoc || !svgDoc.documentElement || svgDoc.__pcbHoverWired) return;
    facts = window.PcbFacts.load(svgDoc);
    objects = catalogue();
    // The cursor readout is a <text> row inside the document's own layer
    // legend box (gr520537) — looked up per bind, never from the page DOM.
    readout = svgDoc.getElementById('pcb-cursor-readout');
    if (readout) readout.textContent = IDLE;
    // Native <title> tooltips would stack under ours; the text is the same facts.
    svgDoc.querySelectorAll('title').forEach((t) => t.remove());
    svgDoc.addEventListener('mousemove', (event) => {
      showCursor(event);
      const text = textFor(targetAt(event.clientX, event.clientY));
      if (!text) { hide(); return; }
      const box = object.getBoundingClientRect();
      tip.textContent = text;
      tip.style.left = box.left + event.clientX + 12 + 'px';
      tip.style.top = box.top + event.clientY + 14 + 'px';
      tip.style.display = 'block';
    });
    svgDoc.documentElement.addEventListener('mouseleave', () => {
      hide();
      if (readout) readout.textContent = IDLE;
    });
    svgDoc.__pcbHoverWired = true;
  };
  object.addEventListener('load', bind);
  if (object.contentDocument && object.contentDocument.documentElement) bind();
})();
