/* Same-origin Gerber targets carry actual instance identity and shown token.
   Selection never fetches, invokes a model or writes the board. */
(() => {
  'use strict';
  const el = (id) => document.getElementById(id);
  const manifestEl = el('pcb-selection-manifest');
  if (!manifestEl) return;
  const manifest = JSON.parse(manifestEl.textContent);
  const object = el('pcb-board-object'), choice = el('pcb-instance-choice');
  const form = el('design-chat-form'), hidden = el('pcb-chat-context');
  const identity = el('pcb-selection-identity'), status = el('pcb-selection-status');
  const copy = el('pcb-copy-footprint'), send = form.querySelector('button');
  let selected = null, svgDoc = null, bound = false, observer = null;
  const clear = (message = 'Select a component instance.') => {
    selected = null; choice.value = ''; hidden.value = ''; identity.textContent = '';
    copy.disabled = true; send.disabled = true; status.textContent = message;
    if (svgDoc) svgDoc.querySelectorAll('[data-pcb-instance]').forEach((node) => {
      if (node.classList.contains('pcb-selected')) node.classList.remove('pcb-selected');
    });
  };
  const matchesRoot = () => svgDoc &&
    svgDoc.documentElement.dataset.pcbBoard === String(manifest.context.board.ref_id) &&
    svgDoc.documentElement.dataset.pcbSnapshot === manifest.context.shown.snapshot;
  const select = (id) => {
    clear();
    if (!bound || !matchesRoot()) { clear('Shown board changed or is unavailable. Reload to select.'); return; }
    const matches = manifest.instances.filter((item) => String(item.instance_id) === String(id));
    if (matches.length !== 1) return;
    selected = matches[0]; choice.value = String(selected.instance_id);
    const fp = selected.footprint;
    identity.textContent = `Instance ${selected.instance_id} · ${selected.refdes}\n` +
      `Footprint: ${fp.available ? fp.scheme + ':' + JSON.stringify(fp.id) : 'unavailable'}\n` +
      `Part: ${fp.part_lcsc || 'unavailable'} · package label: ${fp.label || 'unavailable'}`;
    hidden.value = JSON.stringify({...manifest.context, selected: [selected]});
    copy.disabled = !fp.available; send.disabled = false;
    status.textContent = `Selected ${selected.refdes}. Changes require a proposal and explicit Apply.`;
    svgDoc.querySelectorAll('[data-pcb-instance]').forEach((node) => {
      if (node.dataset.pcbInstance === String(selected.instance_id)) node.classList.add('pcb-selected');
    });
  };
  const bind = () => {
    if (bound && svgDoc === object.contentDocument && matchesRoot()) return;
    if (observer) observer.disconnect();
    clear(); bound = false;
    try { svgDoc = object.contentDocument; } catch (_) { svgDoc = null; }
    if (!matchesRoot()) { clear('Shown board changed or is unavailable. Reload to select.'); return; }
    bound = true;
    const style = svgDoc.createElementNS('http://www.w3.org/2000/svg', 'style');
    style.textContent = '.pcb-selected { stroke: #facc15; stroke-width: 0.15; }';
    svgDoc.documentElement.appendChild(style);
    svgDoc.addEventListener('click', (event) => {
      if (!matchesRoot()) { bound = false; clear('Shown board changed. Reload.'); return; }
      // elementsFromPoint includes stacked visible targets. Never take the
      // first component when two different instances overlap at this point.
      const hits = new Set();
      svgDoc.elementsFromPoint(event.clientX, event.clientY).forEach((node) => {
        const target = node.closest('[data-pcb-instance]');
        if (target && !target.closest('.layer.off')) hits.add(target.dataset.pcbInstance);
      });
      if (hits.size > 1) { clear('Overlapping instances: choose the exact instance from the list.'); return; }
      if (hits.size === 1) select([...hits][0]);
    });
    observer = new MutationObserver((records) => {
      if (!matchesRoot()) { bound = false; clear('Shown board changed. Reload.'); }
      else if (records.some((r) => r.target.classList?.contains('layer'))) clear();
    });
    observer.observe(svgDoc.documentElement, {attributes: true, subtree: true, attributeFilter: ['data-pcb-board', 'data-pcb-snapshot', 'class']});
  };
  object.addEventListener('load', bind);
  // An object can finish before this deferred script registers its listener.
  if (object.contentDocument?.documentElement.dataset.pcbSnapshot) bind();
  choice.addEventListener('change', () => select(choice.value));
  choice.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(choice.value); }
  });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') clear(); });
  el('pcb-deselect').addEventListener('click', () => clear());
  copy.addEventListener('click', async () => {
    if (!selected?.footprint.available || !matchesRoot()) return;
    try { await navigator.clipboard.writeText(JSON.stringify(selected.footprint)); }
    catch (_) { status.textContent = 'Clipboard unavailable; select and copy the identity text.'; }
  });
  form.addEventListener('submit', (event) => {
    if (!selected || !bound || !matchesRoot()) { event.preventDefault(); clear('Reload and select the shown instance before Send.'); }
  });
  clear();
})();
