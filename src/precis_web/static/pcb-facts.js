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

  // current_a is amps, a plain number the agent sets explicitly (gr520542):
  // no hidden default — unset shows nothing, a set value renders in mA
  // under 1 A, in A otherwise.
  function current(a) {
    if (typeof a !== 'number' || !isFinite(a) || a < 0) return null;
    return a < 1 ? Math.round(a * 1000) + 'mA' : +a.toFixed(2) + 'A';
  }

  function ends(net) {
    const pins = net.pins || [];
    if (pins.length === 2) return '[' + pins[0] + ':' + pins[1] + ']';
    if (pins.length === 1) return '[' + pins[0] + ']';
    return '[' + pins.length + ' pins]';
  }

  // "Net/<current>/[a:b]" — the current segment only when one is set
  // (gr520542: no "?mA" placeholder); "[N pins]" for multi-pin nets.
  function netText(facts, name) {
    const net = facts.nets[name];
    if (!net) return name;
    const parts = [name];
    const cur = current(net.current_a);
    if (cur !== null) parts.push(cur);
    parts.push(ends(net));
    return parts.join('/');
  }

  // "C2/(10uF 0603 6.3V)/Pad 2"; the parenthesis only when something is known.
  function partText(facts, refdes) {
    const p = facts.parts[refdes] || {};
    const attrs = [p.value, p.package, p.rating].filter(Boolean).join(' ');
    return attrs ? refdes + '/(' + attrs + ')' : refdes;
  }

  // The same attrs without the parens — "C2/10uF 0603 6.3V" (gr520541).
  // A label fallback like "C 1.0pF" repeats the refdes's class letter, so
  // that leading "C " token is dropped too.
  function partLine(facts, refdes) {
    const p = facts.parts[refdes] || {};
    let value = p.value;
    const prefix = refdes.match(/^[A-Za-z]+/);
    if (value && prefix && value.indexOf(prefix[0] + ' ') === 0) {
      value = value.slice(prefix[0].length + 1);
    }
    const attrs = [value, p.package, p.rating].filter(Boolean).join(' ');
    return attrs ? refdes + '/' + attrs : refdes;
  }

  function failure(facts, name) {
    const net = facts.nets[name];
    if (!net || !net.status || net.status === 'realized') return null;
    const label = net.status === 'failed' ? 'FAILED' : net.status;
    return net.reason && net.reason !== net.status ? label + ': ' + net.reason : label;
  }

  // Only a web URL ever becomes a link — a stored datasheet field is user-
  // supplied catalog data, so anything not http(s) is dropped here.
  const WEB_URL = /^https?:\/\//i;
  function webUrl(u) {
    return typeof u === 'string' && WEB_URL.test(u) ? u : null;
  }

  /* target: {kind:'pad', refdes, pad, net?} | {kind:'part', refdes}
             | {kind:'net'|'ratsnest', net}
     returns {text, lcsc, url, datasheet, script}: text is the tooltip line,
     lcsc/url the part's stored C-number and its LCSC page (both null
     without one), datasheet its catalog datasheet URL (gr520537), script
     the traced placement's board-script site {file, line, ws} — null when
     no construction trace names a maker (gr520540). */
  function describe(facts, target) {
    const out = {text: '', lcsc: null, url: null, datasheet: null, script: null};
    let text = '';
    if (target.kind === 'pad' || target.kind === 'part') {
      text = partText(facts, target.refdes);
      if (target.kind === 'pad') text += '/Pad ' + target.pad;
      const part = facts.parts[target.refdes] || {};
      out.url = lcscUrl(part.lcsc);
      out.lcsc = out.url ? part.lcsc : null;
      out.datasheet = webUrl(part.datasheet);
      out.script = part.script || null;
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

  /* A track segment (gr520542): the net's own line plus the segment's
     width and layer — "SIG/120mA/[R1.1:C2.2]/track 0.25mm F.Cu". */
  function trackText(facts, o) {
    const name = o.net || 'no net';
    const base = facts.nets && facts.nets[name] ? netText(facts, name) : name;
    const seg = ['track', o.width_mm != null ? mm(o.width_mm) : null, o.layer]
      .filter(Boolean).join(' ');
    const bad = o.net ? failure(facts, name) : null;
    return base + '/' + seg + (bad ? '/' + bad : '');
  }

  /* A silkscreen object: refdes / value+package / element — "C3/1pF 0402/
     silk outline" (gr520541: the "(...)" label form was noise), or the
     layer for board-level silk. */
  function silkText(facts, silk) {
    const anchor = silk.anchor || {};
    if (anchor.kind === 'instance' && anchor.refdes) {
      return partLine(facts, anchor.refdes) + '/silk ' + (silk.role || 'outline');
    }
    return 'Silkscreen ' + (silk.role || 'art') + '/' + (silk.layer || 'unknown layer');
  }

  return {describe, isGround, lcscUrl, load, netText, partText, silkText, trackText, viaText, webUrl, LCSC_PRODUCT};
});
