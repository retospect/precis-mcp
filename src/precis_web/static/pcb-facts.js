/* pcb-facts.js — the ONE formatter behind the board page's hover tooltip
   (pcb-hover.js) and Selection box (pcb-selection.js), so the two never
   disagree (gr495389 / gr495390 / gr495391).

   Facts are the SVG's <metadata id="pcb-facts"> JSON (precis_web/pcb_hover.py):
     {parts: {REFDES: {value, package, rating, lcsc}},
      nets:  {NAME: {pins: ["R1.1", ...], current_a, status, reason}}}
   Everything returned is plain text; callers put it in with textContent, so
   part values typed by users are never parsed as markup. */
(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PcbFacts = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // Fixed template, only ever filled with a validated C-number.
  const LCSC_PRODUCT = 'https://www.lcsc.com/product-detail/';
  const LCSC_RE = /^C\d+$/;

  function lcscUrl(c) {
    return typeof c === 'string' && LCSC_RE.test(c) ? LCSC_PRODUCT + c + '.html' : null;
  }

  function load(svgDoc) {
    const meta = svgDoc && svgDoc.getElementById && svgDoc.getElementById('pcb-facts');
    if (!meta) return {parts: {}, nets: {}};
    try {
      const f = JSON.parse(meta.textContent);
      return {parts: f.parts || {}, nets: f.nets || {}};
    } catch (_) { return {parts: {}, nets: {}}; }
  }

  function current(a) {
    if (typeof a !== 'number' || !isFinite(a) || a <= 0) return null;
    return a < 1 ? Math.round(a * 1000) + 'mA' : +a.toFixed(2) + 'A';
  }

  function ends(net) {
    const pins = net.pins || [];
    if (pins.length === 2) return '[' + pins[0] + ':' + pins[1] + ']';
    if (pins.length === 1) return '[' + pins[0] + ']';
    return '[' + pins.length + ' pins]';
  }

  // The current segment of a net hover; never empty (gr496367). A set value is
  // "120mA". Otherwise the gap is named: a ground net sums the supply nets that
  // return through it ("return ≤ 600mA (sum of power nets: VBUS 500mA + 3V3 100mA; cascaded rails counted twice)"), a
  // signal says "signal, no current set", anything else "current: not set".
  function currentText(facts, name, net) {
    const own = current(net.current_a);
    if (own) return own;
    if (isGround(facts, name)) {
      const known = [], unset = [];
      for (const [other, n] of Object.entries(facts.nets)) {
        if (other === name || isGround(facts, other)) continue;
        if (String(n.net_class || '').toLowerCase() !== 'power') continue;
        const c = current(n.current_a);
        if (c) known.push({name: other, a: n.current_a, text: c}); else unset.push(other);
      }
      if (!known.length) {
        return 'return current: unknown (no supply net has a current set' +
          (unset.length ? '; ' + unset.join(', ') : '') + ')';
      }
      const total = current(known.reduce((s, k) => s + k.a, 0));
      return 'return ≤ ' + total + ' (sum of power nets: ' + known.map((k) => k.name + ' ' + k.text).join(' + ') +
        '; cascaded rails counted twice' +
        (unset.length ? '; current not set: ' + unset.join(', ') : '') + ')';
    }
    return String(net.net_class || '').toLowerCase() === 'signal'
      ? 'signal, no current set' : 'current: not set';
  }

  // "Net/<current>/[a:b]"; "[N pins]" for multi-pin nets.
  function netText(facts, name) {
    const net = facts.nets[name];
    if (!net) return name;
    return [name, currentText(facts, name, net), ends(net)].join('/');
  }

  // "C2/(10uF 0603 6.3V)/Pad 2"; the parenthesis only when something is known.
  function partText(facts, refdes) {
    const p = facts.parts[refdes] || {};
    const attrs = [p.value, p.package, p.rating].filter(Boolean).join(' ');
    return attrs ? refdes + '/(' + attrs + ')' : refdes;
  }

  function failure(facts, name) {
    const net = facts.nets[name];
    if (!net || !net.status || net.status === 'realized') return null;
    const label = net.status === 'failed' ? 'FAILED' : net.status;
    return net.reason && net.reason !== net.status ? label + ': ' + net.reason : label;
  }

  /* target: {kind:'pad', refdes, pad, net?} | {kind:'part', refdes}
             | {kind:'net'|'ratsnest', net}
     returns {text, lcsc, url}: text is the tooltip line, lcsc/url the
     part's stored C-number and its LCSC page (both null without one). */
  function describe(facts, target) {
    const out = {text: '', lcsc: null, url: null};
    let text = '';
    if (target.kind === 'pad' || target.kind === 'part') {
      text = partText(facts, target.refdes);
      if (target.kind === 'pad') text += '/Pad ' + target.pad;
      const lcsc = (facts.parts[target.refdes] || {}).lcsc;
      out.url = lcscUrl(lcsc);
      out.lcsc = out.url ? lcsc : null;
      const bad = target.net ? failure(facts, target.net) : null;
      if (bad) text += '/' + target.net + ' ' + bad;
    } else if (target.net) {
      text = netText(facts, target.net);
      const bad = failure(facts, target.net);
      if (bad) text += '/' + bad;
    }
    out.text = text;
    return out;
  }

  // Ground by class when the design set one, else by the usual names.
  const GROUND_RE = /^[ADP]?GND\d*$|^VSS[A-Z]*\d*$/i;
  function isGround(facts, name) {
    const net = (facts.nets || {})[name];
    if (net && typeof net.net_class === 'string' && net.net_class) {
      return net.net_class.toLowerCase() === 'ground';
    }
    return typeof name === 'string' && GROUND_RE.test(name);
  }

  function mm(v) {
    return typeof v === 'number' && isFinite(v) ? +v.toFixed(3) + 'mm' : 'unknown';
  }

  /* A via from the SVG's pcb-objects catalogue:
     "GND via/drill 0.3mm/pad 0.6mm/F.Cu-B.Cu/stitching". A via on a ground net
     is called stitching; any other net says "not stitching". */
  function viaText(facts, via) {
    const parts = [(via.net || 'no net') + ' via',
      'drill ' + mm(via.drill_mm), 'pad ' + mm(via.dia_mm)];
    const layers = via.layers || [];
    if (layers.length) parts.push(layers.length > 2 ? layers.join(',') : layers.join('-'));
    parts.push(via.net && isGround(facts, via.net) ? 'stitching' : 'not stitching');
    if (via.fixed) parts.push('fixed');
    return parts.join('/');
  }

  /* A silkscreen object: the owning part's tooltip plus the silk role
     ("R1/(10k 0603 0.1W)/silk refdes"), or the layer for board-level silk. */
  function silkText(facts, silk) {
    const anchor = silk.anchor || {};
    if (anchor.kind === 'instance' && anchor.refdes) {
      return partText(facts, anchor.refdes) + '/silk ' + (silk.role || 'outline');
    }
    return 'Silkscreen ' + (silk.role || 'art') + '/' + (silk.layer || 'unknown layer');
  }

  return {describe, isGround, lcscUrl, load, netText, partText, silkText, viaText, LCSC_PRODUCT};
});
