/* pcb-hover.js — tooltip over the board <object> (gr495389, gr495391).
   Pads (and the pads of failed nets) show "C2/(10uF 0603 6.3V)/Pad 2", tracks
   and ratsnest lines "NET/120mA/[R1.1:C2.2]" plus the router's failure reason.
   Text comes from the shared PcbFacts.describe and is set with textContent. */
(() => {
  'use strict';
  const object = document.getElementById('pcb-board-object');
  if (!object || !window.PcbFacts) return;
  let svgDoc = null, facts = null;
  const tip = document.createElement('div');
  tip.id = 'pcb-hover-tip';
  tip.setAttribute('role', 'tooltip');
  Object.assign(tip.style, {
    position: 'fixed', zIndex: '50', display: 'none', pointerEvents: 'none',
    maxWidth: '28rem', padding: '2px 6px', borderRadius: '3px', font: '12px ui-monospace, monospace',
    background: '#111827', color: '#f9fafb', whiteSpace: 'pre-wrap',
  });
  document.body.appendChild(tip);

  // The target under a point: the first stacked node carrying board identity,
  // skipping hidden layers (the click overlay is transparent and sits on top).
  const targetAt = (x, y) => {
    for (const n of svgDoc.elementsFromPoint(x, y)) {
      const d = n.dataset;
      if (!d) continue;
      const layer = n.closest && n.closest('.layer');
      if (layer && layer.classList.contains('off')) continue;
      if (d.pcbRatsnest) return {kind: 'ratsnest', net: d.pcbNet};
      if (d.pcbPart && d.pcbPad) return {kind: 'pad', refdes: d.pcbPart, pad: d.pcbPad, net: d.pcbNet};
      if (d.pcbNet && !d.pcbObject) return {kind: 'net', net: d.pcbNet};
    }
    return null;
  };
  const hide = () => { tip.style.display = 'none'; };
  const bind = () => {
    try { svgDoc = object.contentDocument; } catch (_) { svgDoc = null; }
    if (!svgDoc || !svgDoc.documentElement || svgDoc.__pcbHoverWired) return;
    facts = window.PcbFacts.load(svgDoc);
    // Native <title> tooltips would stack under ours; the text is the same facts.
    svgDoc.querySelectorAll('[data-pcb-net] > title, [data-pcb-part] > title').forEach((t) => t.remove());
    svgDoc.addEventListener('mousemove', (event) => {
      const target = targetAt(event.clientX, event.clientY);
      const text = target ? window.PcbFacts.describe(facts, target).text : '';
      if (!text) { hide(); return; }
      const box = object.getBoundingClientRect();
      tip.textContent = text;
      tip.style.left = box.left + event.clientX + 12 + 'px';
      tip.style.top = box.top + event.clientY + 14 + 'px';
      tip.style.display = 'block';
    });
    svgDoc.documentElement.addEventListener('mouseleave', hide);
    svgDoc.__pcbHoverWired = true;
  };
  object.addEventListener('load', bind);
  if (object.contentDocument && object.contentDocument.documentElement) bind();
})();
