/* Stored per-transition kinetics. No solver, network request, or environment repricing. */
const PWStepKinetics = (() => {
  const fields = [
    ['ea_forward', 'Ea forward', 'eV'], ['ea_reverse', 'Ea reverse', 'eV'],
    ['k_f', 'k_f', '/s'], ['k_b', 'k_b', '/s'], ['net_rate', 'Net flux', '/site/s'],
    ['z_i', 'Reversibility z_i', ''], ['X_RC', 'X_RC', ''],
    ['selectivity', 'Product selectivity', ''],
  ];
  const layers = [
    {key: 'kinetics', label: 'Kinetics on transitions', help: 'Show selected transition labels at the stored environment. Hover always retains the stored values.'},
    {key: 'kin_source', label: 'Barrier source', help: 'NEB, BEP with its recorded fit band, or no barrier.'},
    ...fields.map(([key, label]) => ({key: 'kin_' + key, label, defaultOn: false,
      help: label + ' from the named stored solve; absent values are never zero.'})),
  ];
  const escape = value => String(value).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
  const num = value => Number.isFinite(value) ? Number(value).toPrecision(3) : 'absent';
  const environment = env => {
    if (!env || !Object.keys(env).length) return 'environment not recorded';
    return Object.entries(env).map(([key, value]) => {
      if (key === 'T') return 'T=' + num(value) + ' K';
      if (key === 'temperature') return null;
      return key + '=' + (typeof value === 'object' ? JSON.stringify(value) : String(value));
    }).filter(Boolean).join(', ');
  };
  function source(data) {
    return 'source=' + (data.source || 'none') + (data.source === 'bep'
      ? (Number.isFinite(data.band_eV) ? ' ±' + num(data.band_eV) + ' eV (fit band)' : ' (band absent)') : '');
  }
  function quantity(data, key, label, unit, provenance) {
    const q = (data.quantities || {})[key];
    if (key === 'X_RC' && !q) {
      const note = data.drc_note || 'No usable rate control.';
      return note === 'No usable rate control.' ? note : label + '=absent — ' + note;
    }
    const value = q && Number.isFinite(q.value) ? num(q.value) + (unit ? ' ' + unit : '') : 'absent';
    const product = key === 'selectivity' && data.product ? ' (' + data.product + ')' : '';
    return label + product + '=' + value + (q && (provenance || key === 'X_RC')
      ? ' — ' + q.solve + (provenance ? '; ' + environment(q.environment) : '') : '');
  }
  function text(link) {
    const data = (link || {}).step_kinetics || {};
    return [data.name || '', source(data), 'Stored environment; changing U or pH does not re-solve rates.',
      ...fields.map(([key, label, unit]) => quantity(data, key, label, unit, true))].join('\n');
  }
  function label(link, view = {}) {
    if (view.kinetics === false) return '';
    const data = (link || {}).step_kinetics || {};
    const labels = view.kin_source === false ? [] : [source(data)];
    fields.forEach(([key, title, unit]) => {
      if (view['kin_' + key]) labels.push(quantity(data, key, title, unit, false));
    });
    return labels.join(' · ');
  }
  function inset(link) {
    const data = (link || {}).step_kinetics || {};
    return (data.arrhenius || []).map(series => {
      const points = (series.points || []).filter(p => p.T > 0 && Number.isFinite(p.T) && p.turnover > 0 && Number.isFinite(p.turnover));
      if (new Set(points.map(p => p.T)).size < 3) return '';
      const xs = points.map(p => 1 / p.T), ys = points.map(p => Math.log(p.turnover));
      const xmin = Math.min(...xs), xmax = Math.max(...xs);
      if (xmin === xmax) return '';
      const ymin = Math.min(...ys), ymax = Math.max(...ys);
      const x = v => 42 + 230 * (v - xmin) / (xmax - xmin);
      const y = v => 108 - 80 * (v - ymin) / (ymax - ymin || 1);
      const curve = points.map((p, i) => x(xs[i]) + ',' + y(ys[i])).join(' ');
      const title = 'Arrhenius: apparent, whole network — ' + series.solve;
      return '<figure class="pw-arrhenius"><figcaption>' + escape(title) + '</figcaption>' +
        '<svg viewBox="0 0 310 145" width="310" role="img" aria-label="' + escape(title) + '">' +
        '<path d="M42 20 V108 H280" fill="none" stroke="currentColor"/>' +
        '<polyline points="' + curve + '" fill="none" stroke="currentColor"/>' +
        points.map((p, i) => '<circle cx="' + x(xs[i]) + '" cy="' + y(ys[i]) + '" r="3"><title>' +
          escape(p.T + ' K: turnover=' + num(p.turnover) + ' /site/s') + '</title></circle>').join('') +
        '<text x="4" y="15" font-size="10">ln(turnover)</text><text x="130" y="141" font-size="10">1/T (K⁻¹)</text>' +
        '<text x="42" y="123" font-size="9">' + num(xmin) + '</text><text x="245" y="123" font-size="9">' + num(xmax) + '</text>' +
        '<text x="3" y="108" font-size="9">' + num(ymin) + '</text><text x="3" y="28" font-size="9">' + num(ymax) + '</text></svg>' +
        '<p>Per-step rates are not stored in sweeps.</p><p>' + escape(environment(series.environment)) +
        '</p><p>Fitted apparent prefactor A=' + num(series.prefactor) +
        ' /site/s; apparent Ea=' + num(series.ea_eV) + ' eV.</p><p>±10 K read-off (fit, inside stored range only): ' +
        escape((series.read_off || []).map(p => p.T + ' K: ' + num(p.turnover) + ' /site/s').join('; ') || 'absent') + '</p></figure>';
    }).join('');
  }
  function enqueue(queue, link, view, position) {
    const selected = label(link, view);
    if (!selected) return;
    selected.split(' · ').forEach((text, i) => queue.push({
      ...position, y: position.y - i * 10, text, fontSize: 7.5, bold: false,
      title: PWStepKinetics.text(link), cls: 'pw-step-kinetics',
    }));
  }
  function renderInset(links) {
    const panel = document.getElementById('pw-arrhenius-inset');
    if (!panel) return;
    // Every link carries the same whole-network fit. Show it once, without
    // tying aggregate turnover to a hovered or selected transition.
    const link = links.find(link => (link.step_kinetics || {}).arrhenius?.length);
    panel.innerHTML = inset(link);
  }
  const groups = new WeakMap();
  function controlHost(box, layer) {
    if (layer.key !== 'kinetics' && !layer.key.startsWith('kin_')) return box;
    if (!groups.has(box)) {
      const group = document.createElement('fieldset');
      group.className = 'w-full flex flex-wrap gap-x-3 gap-y-1 border-t border-line mt-1 pt-1';
      const legend = document.createElement('legend');
      legend.textContent = 'Stored transition kinetics';
      group.appendChild(legend);
      box.appendChild(group);
      groups.set(box, group);
    }
    return groups.get(box);
  }
  return {layers, text, label, inset, renderInset, controlHost, enqueue};
})();
