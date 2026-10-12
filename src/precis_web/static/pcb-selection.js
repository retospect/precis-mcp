/* Same-origin Gerber targets carry the exact identity of every selectable
   board object (instance, pad, via, silkscreen, outline segment, fiducial,
   net) and the shown token. Selection never fetches, invokes a model or
   writes the board; the server re-resolves the echoed identity on Send. */
(() => {
  'use strict';
  const el = (id) => document.getElementById(id);
  const manifestEl = el('pcb-selection-manifest');
  if (!manifestEl) return;
  const manifest = JSON.parse(manifestEl.textContent);
  const object = el('pcb-board-object'), choice = el('pcb-object-choice');
  const form = el('design-chat-form'), hidden = el('pcb-chat-context');
  const identity = el('pcb-selection-identity'), status = el('pcb-selection-status');
  const copy = el('pcb-copy-selection'), send = form.querySelector('button');
  const toComponent = el('pcb-select-component'), toNet = el('pcb-select-net');
  // Bottom to top, as the SVG overlay paints them: the top kind under the
  // pointer wins, so a pad beats the part body and the net beneath it.
  const PRIORITY = ['instance', 'net', 'outline', 'silk', 'pad', 'via', 'fiducial'];
  const GROUPS = {pad: 'Pads', via: 'Vias', silk: 'Silkscreen', outline: 'Board outline',
                  fiducial: 'Fiducials', net: 'Nets'};
  let selected = null, svgDoc = null, bound = false, observer = null;
  let catalogue = new Map();
  const canon = (v) => JSON.stringify(v, (_, x) =>
    x && typeof x === 'object' && !Array.isArray(x)
      ? Object.fromEntries(Object.keys(x).sort().map((k) => [k, x[k]])) : x);
  const pt = (p) => `(${p[0]}, ${p[1]})`;
  const label = (o) => {
    switch (o.kind) {
      case 'instance': return `${o.refdes} · instance ${o.instance_id}`;
      case 'pad': return `Pad ${o.pin} of ${o.refdes || '?'} · ${o.layer}` + (o.net ? ` · ${o.net}` : '');
      case 'via': return `Via ${pt([o.x, o.y])}` + (o.net ? ` · ${o.net}` : '');
      case 'silk': return `Silk ${o.layer} · ${o.role}` + (o.content ? ` · ${o.content}` : '');
      case 'outline': return `Outline segment ${o.index}`;
      case 'fiducial': return `Fiducial ${o.id}`;
      case 'net': return `Net ${o.name}`;
      default: return o.key;
    }
  };
  const describe = (o) => {
    switch (o.kind) {
      case 'instance': {
        const fp = o.footprint;
        return `Instance ${o.instance_id} · ${o.refdes}\n` +
          `Footprint: ${fp.available ? fp.scheme + ':' + JSON.stringify(fp.id) : 'unavailable'}\n` +
          `Part: ${fp.part_lcsc || 'unavailable'} · package label: ${fp.label || 'unavailable'}`;
      }
      case 'pad': {
        const g = o.geometry;
        return `Pad ${o.pin} of ${o.refdes || 'no component'}` +
          (o.instance_id == null ? ' (not a component pad)' : ` · instance ${o.instance_id}`) + '\n' +
          `Pin: ${o.pin_name ?? 'unmatched'} · pad number: ${o.pad_number ?? 'unmatched'}\n` +
          `Geometry: ${g.shape} at ${pt([g.x, g.y])}` + (g.w != null ? ` · ${g.w}` + (g.h != null ? `×${g.h}` : '') + ' mm' : '') +
          (g.drill != null ? ` · drill ${g.drill} mm` : '') + (g.vertices ? ` · ${g.vertices} vertices` : '') + '\n' +
          `Layer: ${o.layer} · net: ${o.net ?? 'none'}`;
      }
      case 'via':
        return `Via at ${pt([o.x, o.y])}\nNet: ${o.net ?? 'none'}\n` +
          `Drill: ${o.drill_mm ?? 'unknown'} mm · diameter: ${o.dia_mm ?? 'unknown'} mm\n` +
          `Layers: ${o.layers.join(', ')}${o.fixed ? ' · fixed' : ''}`;
      case 'silk':
        return `Silkscreen ${o.role}\nLayer: ${o.layer}\nContent: ${o.content ?? 'not recorded'}\n` +
          `Anchor: ${o.anchor.kind === 'instance' ? o.anchor.refdes + ' (instance ' + o.anchor.instance_id + ')' : 'board'}`;
      case 'outline':
        return `Board outline segment ${o.index}\nLayer: ${o.layer}\n${pt(o.start)} → ${pt(o.end)}`;
      case 'fiducial':
        return `Fiducial ${o.id}\nPosition: ${pt([o.x, o.y])}\nCopper diameter: ${o.copper_dia_mm ?? 'unknown'} mm\nLayers: ${o.layers.join(', ')}`;
      case 'net': return `Net ${o.name} (id ${o.net_id})`;
      default: return o.key;
    }
  };
  const highlight = (key) => {
    if (!svgDoc) return;
    svgDoc.querySelectorAll('.pcb-select .pcb-selected').forEach((n) => n.classList.remove('pcb-selected'));
    if (key) svgDoc.querySelectorAll('.pcb-select [data-pcb-object]').forEach((n) => {
      if (n.dataset.pcbObject === key) n.classList.add('pcb-selected');
    });
  };
  const clear = (message = 'Select a board object.') => {
    selected = null; choice.value = ''; hidden.value = ''; identity.textContent = '';
    copy.disabled = send.disabled = toComponent.disabled = toNet.disabled = true;
    status.textContent = message;
    highlight(null);
  };
  const matchesRoot = () => svgDoc && svgDoc.documentElement &&
    svgDoc.documentElement.dataset.pcbBoard === String(manifest.context.board.ref_id) &&
    svgDoc.documentElement.dataset.pcbSnapshot === manifest.context.shown.snapshot;
  // The argue-box handle of an object; same naming as gerber_view._object_handle.
  const handleOf = (o) => {
    switch (o.kind) {
      case 'instance': return o.refdes || null;
      case 'pad':
        return o.refdes && o.pin ? `${o.refdes}.${o.pin}` : (o.net ? `net:${o.net}` : null);
      case 'net': return o.name ? `net:${o.name}` : null;
      case 'via': case 'silk': case 'outline': case 'fiducial': return o.key || null;
      default: return null;
    }
  };
  const select = (key) => {
    clear();
    if (!bound || !matchesRoot()) { clear('Shown board changed or is unavailable. Reload to select.'); return; }
    const o = catalogue.get(key);
    if (!o) return;
    selected = o;
    if ([...choice.options].some((opt) => opt.value === key)) choice.value = key;
    identity.textContent = describe(o);
    hidden.value = JSON.stringify({...manifest.context, selected: [o]});
    copy.disabled = false; send.disabled = false;
    const instanceId = o.kind === 'pad' ? o.instance_id : (o.kind === 'silk' && o.anchor.kind === 'instance' ? o.anchor.instance_id : null);
    toComponent.disabled = instanceId == null || !catalogue.has(`instance:${instanceId}`);
    toComponent.dataset.target = instanceId == null ? '' : `instance:${instanceId}`;
    const netId = (o.kind === 'pad' || o.kind === 'via') ? o.net_id : null;
    toNet.disabled = netId == null || !catalogue.has(`net:${netId}`);
    toNet.dataset.target = netId == null ? '' : `net:${netId}`;
    status.textContent = o.kind === 'instance'
      ? `Selected ${o.refdes}. Changes require a proposal and explicit Apply.`
      : `Selected ${label(o)}. Other objects are discussion-only; select the component to propose a move.`;
    highlight(key);
    const handle = handleOf(o);
    if (handle) document.dispatchEvent(new CustomEvent('pcb-selection-attach', {detail: {handle}}));
  };
  // Catalogue = the SVG's own object list, with the page manifest's instance
  // identities (which carry the footprint) required to agree with it.
  const loadCatalogue = () => {
    const meta = svgDoc.getElementById('pcb-objects');
    if (!meta) return false;
    let list;
    try { list = JSON.parse(meta.textContent); } catch (_) { return false; }
    const map = new Map(list.map((o) => [o.key, o]));
    for (const inst of manifest.instances) {
      const key = inst.key || `instance:${inst.instance_id}`;
      if (map.has(key) && canon(map.get(key)) !== canon(inst)) return false;
      map.set(key, inst);
    }
    catalogue = map;
    choice.querySelectorAll('optgroup[data-dynamic]').forEach((g) => g.remove());
    for (const kind of PRIORITY) {
      if (!GROUPS[kind]) continue;
      const group = document.createElement('optgroup');
      group.label = GROUPS[kind]; group.dataset.dynamic = '1';
      for (const o of list) {
        if (o.kind !== kind) continue;
        const opt = document.createElement('option');
        opt.value = o.key; opt.textContent = label(o);
        group.appendChild(opt);
      }
      if (group.children.length) choice.appendChild(group);
    }
    return true;
  };
  const bind = () => {
    if (bound && svgDoc === object.contentDocument && matchesRoot()) return;
    if (observer) observer.disconnect();
    clear(); bound = false;
    try { svgDoc = object.contentDocument; } catch (_) { svgDoc = null; }
    if (!matchesRoot() || !loadCatalogue()) { clear('Shown board changed or is unavailable. Reload to select.'); return; }
    bound = true;
    // A rebind on the same document must not stack a second listener.
    if (!svgDoc.__pcbSelectionWired) svgDoc.addEventListener('click', (event) => {
      if (!matchesRoot()) { bound = false; clear('Shown board changed. Reload.'); return; }
      // Every stacked overlay target at this point, minus hidden layers. Never
      // take the first object when two different ones overlap at the top kind.
      const found = new Map();
      svgDoc.elementsFromPoint(event.clientX, event.clientY).forEach((node) => {
        if (!node.dataset || !node.dataset.pcbObject) return;
        const layer = node.dataset.pcbLayer;
        if (layer && svgDoc.getElementById(`layer-${layer}`)?.classList.contains('off')) return;
        found.set(node.dataset.pcbObject, node.dataset.pcbKind);
      });
      const kinds = PRIORITY.filter((k) => [...found.values()].includes(k));
      if (!kinds.length) return;
      const top = kinds[kinds.length - 1];
      const keys = [...found].filter(([, k]) => k === top).map(([key]) => key);
      if (keys.length > 1) { clear(`Overlapping ${top}s: choose from the list.`); return; }
      select(keys[0]);
    });
    svgDoc.__pcbSelectionWired = true;
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
  toComponent.addEventListener('click', () => { if (toComponent.dataset.target) select(toComponent.dataset.target); });
  toNet.addEventListener('click', () => { if (toNet.dataset.target) select(toNet.dataset.target); });
  copy.addEventListener('click', async () => {
    if (!selected || !matchesRoot()) return;
    try { await navigator.clipboard.writeText(JSON.stringify(selected)); }
    catch (_) { status.textContent = 'Clipboard unavailable; select and copy the identity text.'; }
  });
  form.addEventListener('submit', (event) => {
    if (!selected || !bound || !matchesRoot()) { event.preventDefault(); clear('Reload and select the shown object before Send.'); }
  });
  clear();
})();
