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

  // "Net/<mA>/[a:b]" — current only when known; "[N pins]" for multi-pin nets.
  function netText(facts, name) {
    const net = facts.nets[name];
    if (!net) return name;
    const parts = [name];
    const cur = current(net.current_a);
    if (cur) parts.push(cur);
    parts.push(ends(net));
    return parts.join('/');
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

  return {describe, lcscUrl, load, netText, partText, LCSC_PRODUCT};
});
