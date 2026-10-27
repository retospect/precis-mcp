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
  const factsBox = el('pcb-selection-facts');
  const copy = el('pcb-copy-selection'), send = form.querySelector('button');
  const toComponent = el('pcb-select-component'), toNet = el('pcb-select-net');
  const deselect = el('pcb-deselect');
  const thumb = el('pcb-selection-thumb');
  // Bottom to top, as the SVG overlay paints them: the top kind under the
  // pointer wins, so a pad beats the part body and the net beneath it.
  const PRIORITY = ['instance', 'net', 'track', 'outline', 'silk', 'pad', 'via', 'fiducial'];
  const GROUPS = {pad: 'Pads', via: 'Vias', silk: 'Silkscreen', outline: 'Board outline',
                  track: 'Tracks', fiducial: 'Fiducials', net: 'Nets'};
  let selected = null, svgDoc = null, bound = false, observer = null;
  let catalogue = new Map();
  const canon = (v) => JSON.stringify(v, (_, x) =>
    x && typeof x === 'object' && !Array.isArray(x)
      ? Object.fromEntries(Object.keys(x).sort().map((k) => [k, x[k]])) : x);
  const pt = (p) => `(${p[0]}, ${p[1]})`;
  // The catalog package name per refdes (gr520540) — the same
  // #pcb-part-cards JSON the hover cards read, so the Selection box shows
  // "0402", never the opaque easyeda footprint id.
  const partCards = (() => {
    try { return JSON.parse(el('pcb-part-cards').textContent); } catch (_) { return {}; }
  })();
  const packageOf = (refdes) => (partCards[refdes] || {}).package || null;
  const label = (o) => {
    switch (o.kind) {
      case 'instance': return `${o.refdes}`;
      case 'pad': return `Pad ${o.pin} of ${o.refdes || '?'} · ${o.layer}` + (o.net ? ` · ${o.net}` : '');
      case 'via': return `Via ${pt([o.x, o.y])}` + (o.net ? ` · ${o.net}` : '');
      case 'silk': return `Silk ${o.layer} · ${o.role}` + (o.content ? ` · ${o.content}` : '');
      case 'outline': return `Outline segment ${o.index}`;
      case 'track': return `Track ${o.net || 'no net'} · ${o.layer}` +
        (o.width_mm != null ? ` · ${o.width_mm}mm` : '');
      case 'fiducial': return `Fiducial ${o.id}`;
      case 'net': return `Net ${o.name}`;
      default: return o.key;
    }
  };
  // The part's placement from the page manifest: "x 12.5 mm, y 3 mm · layer F.Cu · rotation 90°".
  // A missing value says so; the line never drops out.
  const placement = (instanceId) => {
    const p = (manifest.poses || {})[instanceId];
    if (!p || p.x == null || p.y == null) return 'Placement: not placed';
    const rot = p.rot == null ? 'unknown' : `${p.rot}°`;
    return `Placement: x ${p.x} mm, y ${p.y} mm · layer ${p.layer || 'unknown'} · rotation ${rot}` +
      (p.fixed ? ` · fixed (${p.fixed})` : '');
  };
  const describe = (o) => {
    switch (o.kind) {
      case 'instance':
        return `${o.refdes}\n` +
          `${placement(o.instance_id)}\n` +
          `Package: ${packageOf(o.refdes) || 'unavailable'}\n` +
          `Part: ${o.footprint.part_lcsc || 'unavailable'}`;
      case 'pad': {
        const g = o.geometry;
        return `Pad ${o.pin} of ${o.refdes || 'no component'}` +
          (o.instance_id == null ? ' (not a component pad)' : '') + '\n' +
          `Pin: ${o.pin_name ?? 'unmatched'} · pad number: ${o.pad_number ?? 'unmatched'}\n` +
          `Geometry: ${g.shape} at ${pt([g.x, g.y])}` + (g.w != null ? ` · ${g.w}` + (g.h != null ? `×${g.h}` : '') + ' mm' : '') +
          (g.drill != null ? ` · drill ${g.drill} mm` : '') + (g.vertices ? ` · ${g.vertices} vertices` : '') + '\n' +
          `Layer: ${o.layer} · net: ${o.net ?? 'none'}` +
          (o.instance_id == null ? '' : `\nPart ${o.refdes}: ${placement(o.instance_id)}`);
      }
      case 'via':
        return `Via at ${pt([o.x, o.y])}\nNet: ${o.net ?? 'none'}\n` +
          `Drill: ${o.drill_mm ?? 'unknown'} mm · diameter: ${o.dia_mm ?? 'unknown'} mm\n` +
          `Layers: ${o.layers.join(', ')}${o.fixed ? ' · fixed' : ''}`;
      case 'silk':
        return `Silkscreen ${o.role}\nLayer: ${o.layer}\nContent: ${o.content ?? 'not recorded'}\n` +
          `Anchor: ${o.anchor.kind === 'instance' ? o.anchor.refdes : 'board'}` +
          (o.anchor.kind === 'instance' ? `\nPart ${o.anchor.refdes}: ${placement(o.anchor.instance_id)}` : '');
      case 'outline':
        return `Board outline segment ${o.index}\nLayer: ${o.layer}\n${pt(o.start)} → ${pt(o.end)}`;
      case 'track':
        return `Track on ${o.net ?? 'no net'}\n` +
          `Layer: ${o.layer} · width ${o.width_mm ?? 'unknown'} mm\n` +
          `${pt(o.start)} → ${pt(o.end)}`;
      case 'fiducial':
        return `Fiducial ${o.id}\nPosition: ${pt([o.x, o.y])}\nCopper diameter: ${o.copper_dia_mm ?? 'unknown'} mm\nLayers: ${o.layers.join(', ')}`;
      case 'net': return `Net ${o.name} (id ${o.net_id})`;
      default: return o.key;
    }
  };
  // The same PcbFacts.describe text the hover tooltip shows, plus the part's
  // LCSC page link when it has a stored C-number (none: no link at all).
  const factsTarget = (o) => {
    switch (o.kind) {
      case 'pad': return o.refdes ? {kind: 'pad', refdes: o.refdes, pad: o.pin, net: o.net} : (o.net ? {kind: 'net', net: o.net} : null);
      case 'instance': return {kind: 'part', refdes: o.refdes};
      case 'silk': return o.anchor.kind === 'instance' ? {kind: 'part', refdes: o.anchor.refdes} : null;
      case 'net': return {kind: 'net', net: o.name};
      case 'via': case 'track': return o.net ? {kind: 'net', net: o.net} : null;
      default: return null;
    }
  };
  const showFacts = (o) => {
    factsBox.textContent = '';
    const t = window.PcbFacts && factsTarget(o);
    if (!t) return;
    const d = window.PcbFacts.describe(window.PcbFacts.load(svgDoc), t);
    if (d.text) factsBox.append(d.text);
    if (d.url) {
      const a = document.createElement('a');
      a.href = d.url; a.textContent = d.lcsc; a.target = '_blank'; a.rel = 'noopener noreferrer';
      factsBox.append(' · LCSC ', a);
    }
    // gr520537: the datasheet link sits next to the C-number, in a new tab.
    if (d.datasheet) {
      const a = document.createElement('a');
      a.href = d.datasheet; a.textContent = 'datasheet ↗';
      a.target = '_blank'; a.rel = 'noopener noreferrer';
      factsBox.append(' ', a);
    }
  };
  // gr520538: the preview box shows the part's cached JLCPCB image (same
  // /part/<refdes>/thumb route the hover card uses — always our own
  // cached copy, never a hotlink) when the selection names a part.
  const SLUG = ((object.dataset.base || '').split('/pcb/')[1] || '').split('/')[0];
  const previewRefdes = (o) => {
    if (!o) return null;
    if (o.kind === 'instance' || o.kind === 'pad') return o.refdes || null;
    if (o.kind === 'silk' && o.anchor.kind === 'instance') return o.anchor.refdes;
    return null;
  };
  const showPreview = (o) => {
    if (!thumb) return;
    const refdes = previewRefdes(o);
    if (!refdes) {
      thumb.hidden = true;
      thumb.removeAttribute('src');
      return;
    }
    thumb.onerror = () => {
      thumb.hidden = true;
      thumb.removeAttribute('src');
    };
    thumb.src = `/pcb/${encodeURIComponent(SLUG)}/part/${encodeURIComponent(refdes)}/thumb`;
    thumb.alt = `${refdes} part image`;
    thumb.hidden = false;
  };
  const highlight = (key) => {
    if (!svgDoc) return;
    svgDoc.querySelectorAll('.pcb-select .pcb-selected').forEach((n) => n.classList.remove('pcb-selected'));
    if (key) svgDoc.querySelectorAll('.pcb-select [data-pcb-object]').forEach((n) => {
      if (n.dataset.pcbObject === key) n.classList.add('pcb-selected');
    });
  };
  // gr520544: fisheye navigation — a selection is never a dead end. The
  // nav line links the related objects: pad → part + net, via/track →
  // net, net → its pads, part → its pads and their nets.
  const nav = el('pcb-selection-nav');
  const navLink = (key, text) => {
    const a = document.createElement('a');
    a.href = '#';
    a.textContent = text;
    a.className = 'underline hover:text-fg-secondary';
    a.addEventListener('click', (ev) => {
      ev.preventDefault();
      if (catalogue.has(key)) select(key);
    });
    return a;
  };
  const padsOf = (pred) =>
    [...catalogue.values()].filter((x) => x.kind === 'pad' && pred(x));
  const netName = (nid) => (catalogue.get(`net:${nid}`) || {}).name || `net ${nid}`;
  const showNav = (o) => {
    if (!nav) return;
    nav.textContent = '';
    if (!o) return;
    const add = (label, links) => {
      if (!links.length) return;
      const span = document.createElement('span');
      span.append(`${label} `);
      links.forEach(([key, text], i) => {
        if (i) span.append(' ');
        span.appendChild(navLink(key, text));
      });
      nav.appendChild(span);
      nav.append(' ');
    };
    const padLabel = (p) => `${p.refdes || '?'}.${p.pin}`;
    switch (o.kind) {
      case 'pad':
        add('part', o.instance_id != null && o.refdes
          ? [[`instance:${o.instance_id}`, o.refdes]] : []);
        add('net', o.net_id != null && o.net ? [[`net:${o.net_id}`, o.net]] : []);
        break;
      case 'via':
      case 'track':
        add('net', o.net_id != null && o.net ? [[`net:${o.net_id}`, o.net]] : []);
        break;
      case 'net':
        add('pads', padsOf((p) => p.net_id === o.net_id).map((p) => [p.key, padLabel(p)]));
        break;
      case 'instance': {
        const pads = padsOf((p) => p.instance_id === o.instance_id);
        add('pads', pads.map((p) => [p.key, padLabel(p)]));
        const nids = [...new Set(pads.map((p) => p.net_id).filter((n) => n != null))];
        add('nets', nids.map((nid) => [`net:${nid}`, netName(nid)]));
        break;
      }
      case 'silk':
        if (o.anchor.kind === 'instance' && o.anchor.refdes) {
          add('part', [[`instance:${o.anchor.instance_id}`, o.anchor.refdes]]);
        }
        break;
      default:
        break;
    }
  };
  const clear = (message = 'Select a board object.') => {
    selected = null; choice.value = ''; hidden.value = ''; identity.textContent = '';
    factsBox.textContent = '';
    showPreview(null);
    showNav(null);
    copy.disabled = send.disabled = toComponent.disabled = toNet.disabled =
      deselect.disabled = true;
    status.textContent = message;
    highlight(null);
    document.dispatchEvent(new CustomEvent('pcb-selection-changed', {detail: {object: null, text: ''}}));
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
      case 'via': case 'silk': case 'outline': case 'track': case 'fiducial': return o.key || null;
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
    showFacts(o);
    showPreview(o);
    showNav(o);
    hidden.value = JSON.stringify({...manifest.context, selected: [o]});
    copy.disabled = false; send.disabled = false; deselect.disabled = false;
    const instanceId = o.kind === 'pad' ? o.instance_id : (o.kind === 'silk' && o.anchor.kind === 'instance' ? o.anchor.instance_id : null);
    toComponent.disabled = instanceId == null || !catalogue.has(`instance:${instanceId}`);
    toComponent.dataset.target = instanceId == null ? '' : `instance:${instanceId}`;
    const netId = (o.kind === 'pad' || o.kind === 'via' || o.kind === 'track') ? o.net_id : null;
    toNet.disabled = netId == null || !catalogue.has(`net:${netId}`);
    toNet.dataset.target = netId == null ? '' : `net:${netId}`;
    status.textContent = o.kind === 'instance'
      ? `Selected ${o.refdes}. Changes require a proposal and explicit Apply.`
      : `Selected ${label(o)}. Other objects are discussion-only; select the component to propose a move.`;
    highlight(key);
    const handle = handleOf(o);
    if (handle) document.dispatchEvent(new CustomEvent('pcb-selection-attach', {detail: {handle}}));
    // The workbench's info strip mirrors the selection between hovers
    // (pcb-crossprobe.js listens); the object rides along so the strip
    // can light the same expansion in both panes.
    document.dispatchEvent(new CustomEvent('pcb-selection-changed', {detail: {object: o, text: describe(o)}}));
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
      // gr520543 'part' granularity: a pad, the part body or anchored
      // silkscreen resolves to the whole part; a track resolves to its
      // net. The first resolvable object in hit order wins.
      if (granularity() === 'part') {
        const instKey = (o) => {
          if (!o) return null;
          if (o.kind === 'instance') return `instance:${o.instance_id}`;
          if (o.kind === 'pad') return o.instance_id == null ? null : `instance:${o.instance_id}`;
          if (o.kind === 'silk' && o.anchor.kind === 'instance') return `instance:${o.anchor.instance_id}`;
          if (o.kind === 'track') return o.net_id == null ? null : `net:${o.net_id}`;
          return null;
        };
        for (const kind of [...kinds].reverse()) {
          for (const [key] of [...found].filter(([, k]) => k === kind)) {
            const target = instKey(catalogue.get(key));
            if (target && catalogue.has(target)) { select(target); return; }
          }
        }
      }
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
  // The schematic pane's clicks arrive here as the SAME board object —
  // pcb-crossprobe.js translates a schematic handle to this catalogue's
  // key and asks for a normal select (panel, chat context, chips).
  document.addEventListener('pcb-cross-select', (event) => {
    const key = event.detail && event.detail.key;
    if (key && catalogue.has(key)) select(key);
  });
  object.addEventListener('load', bind);
  // An object can finish before this deferred script registers its listener.
  if (object.contentDocument?.documentElement.dataset.pcbSnapshot) bind();
  choice.addEventListener('change', () => select(choice.value));
  choice.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(choice.value); }
  });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') clear(); });
  deselect.addEventListener('click', () => clear());
  // gr520543: the detail/part granularity toggle (layout pane toolbar).
  // 'detail' keeps the top-kind pick; 'part' resolves to the whole part.
  // Remembered per user like the other workbench toggles.
  const granBtn = el('pcb-granularity');
  const GRAN_KEY = 'precis:pcb-workbench:granularity';
  let gran = 'detail';
  try { gran = localStorage.getItem(GRAN_KEY) || 'detail'; } catch (_) { /* same */ }
  const granularity = () => gran;
  const paintGran = () => {
    if (!granBtn) return;
    granBtn.textContent = `select: ${gran}`;
    granBtn.setAttribute('aria-pressed', String(gran === 'part'));
  };
  paintGran();
  if (granBtn) granBtn.addEventListener('click', () => {
    gran = gran === 'part' ? 'detail' : 'part';
    try { localStorage.setItem(GRAN_KEY, gran); } catch (_) { /* same */ }
    paintGran();
  });
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
