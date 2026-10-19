/* pcb-crossprobe.js — the workbench's cross-probe layer (slice 2 of
   docs/backlog/pcb-workbench-layout.md): a part, pin or net is ONE object
   with ONE handle in both panes.

   Hover over a part/pin/net/via/track in either pane temporarily replaces
   the info strip's selection slot with that object's details and lights
   its expansion in both panes; leaving restores the selection. Hover
   never changes the selection. Click in the schematic selects the same
   board object (via the `pcb-cross-select` event pcb-selection.js owns),
   which drops its handle into the chat as before.

   Power/ground exception: a pin on a net whose class is power/ground —
   or named like GND — lights only the clicked pin and reads
   "GND, N pins"; the whole net is never flooded.

   Highlight expansion for pin R1.4 on net SIG (not power/ground): the pin,
   its net, every pin on the net and their parts — in both documents. The
   expansion works off the data-handle/data-pcb-* attributes both SVGs
   stamp (gerber_view / schematic), plus the board doc's pcb-facts
   metadata for net classes. */
(function () {
  'use strict';
  const el = (id) => document.getElementById(id);
  const strip = el('pcb-info-detail');
  if (!strip || !window.PcbFacts) return;
  const hoverEl = el('pcb-info-hover');
  const selEl = el('pcb-info-selection');
  const boardObj = el('pcb-board-object');
  const schemObj = el('pcb-schematic-object');

  const DOCS = []; // {obj, doc}
  const cardsEl = el('pcb-part-cards');
  let cards = {};
  try { cards = JSON.parse(cardsEl.textContent); } catch (_) { cards = {}; }
  let facts = {parts: {}, nets: {}};
  let objects = new Map(); // board catalogue key -> identity
  let hovered = null;    // handle currently hovered
  let selecting = new Set(); // els carrying the persistent xs class

  const boardDoc = () => DOCS.find((d) => d.obj === boardObj);
  const STYLE = {
    xh: '.pcb-xh{stroke:#38bdf8;stroke-width:0.15;paint-order:stroke}'
      + '.pcb-xh .stub,.pcb-xh>path{stroke:#38bdf8;stroke-width:2.4}'
      + '.pcb-xh text{fill:#0369a1}.pcb-xh>.part{stroke:#38bdf8;stroke-width:2.2}'
      + '.pcb-xh>.part{fill:#e0f2fe}'
      + 'circle.pcb-xh,rect.pcb-xh{stroke:#38bdf8;stroke-width:0.15}',
    xs: '.pcb-xs{stroke:#facc15;stroke-width:0.15;paint-order:stroke}'
      + '.pcb-xs .stub,.pcb-xs>path{stroke:#f59e0b;stroke-width:2.4}'
      + '.pcb-xs>.part{stroke:#f59e0b;stroke-width:2.2}',
  };

  const docOf = (e) => e && e.ownerDocument;
  const docs = () => DOCS.map((d) => d.doc).filter(Boolean);

  function inject(doc, id, css) {
    if (doc.getElementById(id)) return;
    const s = doc.createElementNS('http://www.w3.org/2000/svg', 'style');
    s.id = id;
    s.textContent = css;
    doc.documentElement.appendChild(s);
  }

  // The net facts the strip needs: board doc's pcb-facts metadata when it
  // exists, else whatever the schematic stamps on data-net-class.
  function netInfo(name) {
    const n = (facts.nets || {})[name] || {};
    return n;
  }
  function netClass(name) {
    const n = netInfo(name);
    if (n.net_class) return n.net_class;
    for (const doc of docs()) {
      const elx = doc.querySelector(`[data-pcb-net="${CSS.escape(name)}"][data-net-class]`);
      if (elx && elx.dataset.netClass) return elx.dataset.netClass;
    }
    return null;
  }
  function isPowerNet(name) {
    const c = (netClass(name) || '').toLowerCase();
    return c === 'power' || c === 'ground' || window.PcbFacts.isGround(facts, name);
  }
  function netPinCount(name) {
    const n = netInfo(name);
    if (n.pins) return n.pins.length;
    const seen = new Set();
    for (const doc of docs()) {
      doc.querySelectorAll(`[data-pcb-net="${CSS.escape(name)}"][data-pcb-pad]`)
        .forEach((e) => seen.add(`${e.dataset.pcbPart}.${e.dataset.pcbPad}`));
    }
    return seen.size;
  }

  // The handle under the pointer: the first stack element whose closest()
  // hits [data-handle] (the board's transparent hit overlay carries
  // data-pcb-object, not a handle — walk past it); layout-only objects
  // fall back to their catalogue identity.
  function targetAt(doc, ev) {
    const els = doc.elementsFromPoint
      ? doc.elementsFromPoint(ev.clientX, ev.clientY)
      : [ev.target];
    for (const n of els) {
      if (!n || !n.dataset) continue;
      const layer = n.closest && n.closest('.layer');
      if (layer && layer.classList.contains('off')) continue;
      const hit = n.closest && n.closest('[data-handle]');
      if (hit) return {handle: hit.getAttribute('data-handle'), el: hit};
      if (n.dataset.pcbObject) {
        const o = objects.get(n.dataset.pcbObject);
        if (o) return {object: o, el: n};
      }
      if (n.dataset.pcbRatsnest || n.dataset.pcbNet) {
        return {net: n.dataset.pcbNet, el: n};
      }
    }
    return null;
  }

  // handle → {nets, pins:[{part,pad}], parts, objects} — the expansion the
  // spec asks to light. A pin pulls in its net (from the element or the
  // facts member lists); a part pulls in its pins.
  function expansion(t) {
    const out = {nets: new Set(), pins: [], parts: new Set(), keys: new Set()};
    const h = t.handle;
    if (t.object) {
      out.keys.add(t.object.key);
      return out;
    }
    if (t.net) out.nets.add(t.net);
    if (!h) return out;
    if (h.startsWith('net:')) out.nets.add(h.slice(4));
    else if (h.includes(':')) out.keys.add(h); // via:/silk:/… catalogue key
    else if (h.includes('.')) {
      const [refdes, pin] = h.split('.', 2);
      out.pins.push({part: refdes, pad: pin});
      const net = (t.el && t.el.dataset && t.el.dataset.pcbNet) || pinNet(refdes, pin);
      // Power/ground nets light ONLY the clicked pin — never the part box,
      // never the net.
      if (net && isPowerNet(net)) return {...out, net, pinOnly: true};
      if (net) out.nets.add(net);
      out.parts.add(refdes);
    } else out.parts.add(h);
    for (const net of out.nets) {
      for (const doc of docs()) {
        doc.querySelectorAll(`[data-pcb-net="${CSS.escape(net)}"][data-pcb-pad]`)
          .forEach((e) => {
            out.pins.push({part: e.dataset.pcbPart, pad: e.dataset.pcbPad});
            out.parts.add(e.dataset.pcbPart);
          });
      }
    }
    return out;
  }

  function pinNet(refdes, pad) {
    for (const [name, n] of Object.entries(facts.nets || {})) {
      if ((n.pins || []).includes(`${refdes}.${pad}`)) return name;
    }
    return null;
  }

  function mark(exp, cls) {
    const found = [];
    for (const doc of docs()) {
      for (const net of exp.nets) {
        doc.querySelectorAll(`[data-pcb-net="${CSS.escape(net)}"]`)
          .forEach((e) => { e.classList.add(cls); found.push(e); });
      }
      for (const p of exp.pins) {
        doc.querySelectorAll(
          `[data-pcb-part="${CSS.escape(p.part)}"][data-pcb-pad="${CSS.escape(p.pad)}"]`
        ).forEach((e) => { e.classList.add(cls); found.push(e); });
        doc.querySelectorAll(`[data-handle="${CSS.escape(`${p.part}.${p.pad}`)}"]`)
          .forEach((e) => { e.classList.add(cls); found.push(e); });
      }
      for (const part of exp.parts) {
        doc.querySelectorAll(`[data-handle="${CSS.escape(part)}"]`)
          .forEach((e) => { e.classList.add(cls); found.push(e); });
      }
      for (const key of exp.keys) {
        doc.querySelectorAll(`[data-pcb-object="${CSS.escape(key)}"]`)
          .forEach((e) => { e.classList.add(cls); found.push(e); });
      }
    }
    return found;
  }

  function clearCls(cls) {
    for (const doc of docs()) {
      doc.querySelectorAll('.' + cls).forEach((e) => e.classList.remove(cls));
    }
  }

  function describe(t) {
    const h = t.handle;
    if (t.object) {
      const o = t.object;
      if (o.kind === 'via') return window.PcbFacts.viaText(facts, o);
      if (o.kind === 'silk') return window.PcbFacts.silkText(facts, o);
      if (o.kind === 'net') return window.PcbFacts.netText(facts, o.name || '');
      return o.key;
    }
    if (h) {
      if (h.startsWith('net:')) {
        const name = h.slice(4);
        return isPowerNet(name)
          ? `${name}, ${netPinCount(name)} pins`
          : window.PcbFacts.netText(facts, name);
      }
      if (h.includes('.')) {
        const [refdes, pin] = h.split('.', 2);
        const net = (t.el && t.el.dataset.pcbNet) || pinNet(refdes, pin);
        const base = window.PcbFacts.describe(facts, {kind: 'pad', refdes, pad: pin, net}).text;
        return net && isPowerNet(net)
          ? `${base} — ${net}, ${netPinCount(net)} pins`
          : base;
      }
      if (!h.includes(':')) return window.PcbFacts.partText(facts, h);
      const o = objects.get(h);
      if (o) return describe({object: o, el: t.el});
      return h;
    }
    if (t.net) return window.PcbFacts.netText(facts, t.net);
    return '';
  }

  // Part card (both panes, spec slice 3): refdes, value, package, LCSC
  // number and stock, plus the cached thumbnail — always our own route,
  // never a hotlink.
  function partCard(refdes) {
    const c = cards[refdes];
    if (!c) return null;
    const card = document.createElement('span');
    card.className = 'pcb-part-card inline-flex items-center gap-2 align-middle';
    if (c.lcsc) {
      const img = document.createElement('img');
      img.src = `/pcb/${encodeURIComponent(SLUG)}/part/${encodeURIComponent(refdes)}/thumb`;
      img.alt = '';
      img.style.cssText = 'width:28px;height:28px;object-fit:contain';
      img.onerror = () => img.remove();
      card.appendChild(img);
    }
    const line = document.createElement('span');
    const fields = [c.value, c.package].filter(Boolean).join(' ');
    line.textContent =
      refdes + (fields ? ` (${fields})` : '') +
      (c.lcsc ? ` · ${c.lcsc}` : '') +
      (c.stock != null ? ` · stock ${c.stock}` : '');
    card.appendChild(line);
    return card;
  }
  const SLUG = (boardObj && boardObj.dataset.base || '')
    .split('/pcb/')[1].split('/')[0];

  function setHover(t) {
    clearCls('pcb-xh');
    hovered = t;
    hoverEl.textContent = '';
    if (t) {
      mark(expansion(t), 'pcb-xh');
      const refdes = t.handle && (t.handle.includes('.') ? t.handle.split('.')[0] : t.handle);
      const card = refdes && !refdes.includes(':') ? partCard(refdes) : null;
      if (card) hoverEl.appendChild(card);
      else hoverEl.textContent = describe(t);
      strip.classList.add('hovering');
    } else {
      strip.classList.remove('hovering');
    }
  }

  // Schematic handle → the board catalogue key of the same object.
  function boardKey(h) {
    if (h.startsWith('net:')) {
      const name = h.slice(4);
      for (const [k, o] of objects) {
        if (o.kind === 'net' && o.name === name) return k;
      }
      return null;
    }
    if (h.includes(':')) return objects.has(h) ? h : null;
    if (h.includes('.')) {
      const [refdes, pin] = h.split('.', 2);
      for (const [k, o] of objects) {
        if (o.kind === 'pad' && o.refdes === refdes && String(o.pin) === pin) return k;
      }
      return null;
    }
    for (const [k, o] of objects) {
      if (o.kind === 'instance' && o.refdes === h) return k;
    }
    return null;
  }

  // Board object → the handle it shares with the schematic (argue's
  // grammar; the same naming pcb-selection.js's handleOf uses).
  function handleOf(o) {
    switch (o.kind) {
      case 'instance': return o.refdes || null;
      case 'pad':
        return o.refdes && o.pin ? `${o.refdes}.${o.pin}` : (o.net ? `net:${o.net}` : null);
      case 'net': return o.name ? `net:${o.name}` : null;
      default: return o.key || null;
    }
  }

  function selectSchematic(t) {
    if (!t || !t.handle) return;
    clearCls('pcb-xs');
    const exp = expansion(t);
    selecting = new Set(mark(exp, 'pcb-xs'));
    const key = boardKey(t.handle);
    if (key) {
      document.dispatchEvent(new CustomEvent('pcb-cross-select', {detail: {key}}));
    } else {
      // No board object (unplaced design): the strip still shows it and
      // the handle still joins the chat.
      selEl.textContent = describe(t);
      document.dispatchEvent(
        new CustomEvent('pcb-selection-attach', {detail: {handle: t.handle}})
      );
      document.dispatchEvent(
        new CustomEvent('pcb-selection-changed', {detail: {text: describe(t)}})
      );
    }
  }

  function wire(obj) {
    const onload = () => {
      let doc = null;
      try { doc = obj.contentDocument; } catch (_) { doc = null; }
      if (!doc || !doc.documentElement || doc.__pcbXprobe) return;
      doc.__pcbXprobe = true;
      inject(doc, 'pcb-xh-css', STYLE.xh);
      inject(doc, 'pcb-xs-css', STYLE.xs);
      DOCS.push({obj, doc});
      if (obj === boardObj) {
        facts = window.PcbFacts.load(doc);
        const meta = doc.getElementById('pcb-objects');
        try { objects = new Map(JSON.parse(meta.textContent).map((o) => [o.key, o])); }
        catch (_) { objects = new Map(); }
      }
      doc.addEventListener('mousemove', (ev) => setHover(targetAt(doc, ev)));
      doc.documentElement.addEventListener('mouseleave', () => setHover(null));
      if (obj === schemObj) {
        doc.addEventListener('click', (ev) => selectSchematic(targetAt(doc, ev)));
      }
    };
    obj.addEventListener('load', onload);
    if (obj.contentDocument && obj.contentDocument.documentElement) onload();
  }

  if (boardObj) wire(boardObj);
  if (schemObj) wire(schemObj);

  // The selection panel stays the source of truth for what the strip
  // shows between hovers; pcb-selection.js announces it here.
  document.addEventListener('pcb-selection-changed', (ev) => {
    const d = ev.detail || {};
    selEl.textContent = d.text || '';
    clearCls('pcb-xs');
    selecting = new Set();
    // A selection lights the same expansion a click resolved — the pin,
    // its net, every pin on it and their parts, in both panes.
    if (d.object && d.object.key) {
      const h = handleOf(d.object);
      selecting = new Set(
        mark(h ? expansion({handle: h}) : {nets: new Set(), pins: [], parts: new Set(), keys: new Set([d.object.key])}, 'pcb-xs')
      );
    }
  });
})();
