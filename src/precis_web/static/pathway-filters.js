/* Visual filters use stored flux only; moving the environment never solves kinetics.
 * Traversal runs on raw edges before supply folding, with a visited set for cycles.
 * Route indices stay stable so the picker and URL still identify original routes.
 */
(function (root) {
  'use strict';
  const pair = (a, b) => JSON.stringify([a, b]);
  const physical = (id) => String(id).split('~')[0];
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const fmt = (n) => Number(n.toPrecision(3)).toString();

  function thermalTicks(T) {
    if (!Number.isFinite(T) || T <= 0) { return []; }
    return [0.001, 1, 3600].map((tau, i) => ({
      tau, label: ['1 ms', '1 s', '1 h'][i],
      eV: 8.617333262e-5 * T * Math.log(1.380649e-23 * T / 6.62607015e-34 * tau),
    }));
  }

  function read(qs, ignored = []) {
    const state = {barrier: null, turnover: -10}; // null tracks the 1 s tick at live T
    for (const [key, min, max] of [['barrier', 0, 10], ['turnover', -120, 12]]) {
      const raw = qs.get('filter_' + key);
      if (raw == null) { continue; }
      const n = Number(raw);
      if (raw.trim() && Number.isFinite(n) && n >= min && n <= max) { state[key] = n; }
      else { ignored.push('filter_' + key); }
    }
    return state;
  }

  function write(qs, state) {
    for (const key of ['barrier', 'turnover']) { qs.delete('filter_' + key); }
    if (state.barrier != null) { qs.set('filter_barrier', String(state.barrier)); }
    if (state.turnover !== -10) { qs.set('filter_turnover', String(state.turnover)); }
  }

  function rateIndex(records) {
    const rates = new Map();
    (records || []).forEach((record) => {
      (record.transitions || []).forEach((t) => {
        if (!Number.isFinite(t.net_rate)) { return; }
        const from = physical(t.from), to = physical(t.to);
        // Net flux changes sign when the diagram traverses a stored step backwards.
        [[pair(from, to), t.net_rate], [pair(to, from), -t.net_rate]].forEach(([key, value]) => {
          if (!rates.has(key)) {
            rates.set(key, {value, source: record.source, environment: record.environment});
          }
        });
      });
    });
    return rates;
  }

  function barrier(edge, bepOn) {
    // The graph's finite measured barrier takes precedence over BEP estimates.
    if (Number.isFinite(edge.barrier)) { return Math.max(0, edge.barrier); }
    const b = edge.bep;
    if (bepOn && b && Number.isFinite(b.alpha) && Number.isFinite(b.beta)
        && Number.isFinite(edge.delta_e)) {
      return Math.max(0, b.alpha * edge.delta_e + b.beta);
    }
    return null;
  }

  function traverse(graph, layers, state, T) {
    const paths = graph.paths && graph.paths.length ? graph.paths : [graph.nodes.map((n) => n.id)];
    const threshold = state.barrier == null ? thermalTicks(T)[1].eV : state.barrier;
    const rates = rateIndex(graph.filter_kinetics);
    const reasons = new Map(), unknown = [], adjacency = new Map();
    graph.links.forEach((edge) => {
      const key = pair(edge.source, edge.target), ea = barrier(edge, layers.bep);
      const rate = rates.get(pair(physical(edge.source), physical(edge.target)));
      let reason = null;
      if (layers.barrier_filter && ea != null && ea > threshold) {
        reason = `barrier ${fmt(ea)} eV > ${fmt(threshold)} eV`;
      } else if (layers.turnover_filter && rate && rate.value < 10 ** state.turnover) {
        reason = `termination: no turnover, ${fmt(rate.value)} /s/site (${rate.source})`;
      }
      if (reason) { reasons.set(key, reason); }
      if (layers.barrier_filter && ea == null && barrier(edge, true) == null) { unknown.push(edge); }
      if (!adjacency.has(edge.source)) { adjacency.set(edge.source, []); }
      adjacency.get(edge.source).push(edge);
    });
    // Roots are the original routes' start materials, never newly orphaned nodes.
    const roots = [...new Set(paths.filter((p) => p.length).map((p) => p[0]))];
    const visible = new Set(roots), queue = roots.slice(), cuts = [];
    for (let i = 0; i < queue.length; i++) {
      (adjacency.get(queue[i]) || []).forEach((edge) => {
        const reason = reasons.get(pair(edge.source, edge.target));
        if (reason) { cuts.push({edge, reason}); return; }
        if (!visible.has(edge.target)) { visible.add(edge.target); queue.push(edge.target); }
      });
    }
    const routeCuts = paths.map((path) => {
      for (let i = 1; i < path.length; i++) {
        const reason = reasons.get(pair(path[i - 1], path[i]));
        if (reason) { return {index: i, label: `cut at ${path[i - 1]}→${path[i]}`, reason}; }
      }
      return null;
    });
    // Retain original path slots so surviving suffixes keep their columns.
    // Hidden nodes and cut edges are absent from the drawable graph; the
    // renderer uses the original graph for folding, layout and chemistry.
    const filtered = reasons.size ? {
      ...graph,
      nodes: graph.nodes.filter((n) => visible.has(n.id)),
      links: graph.links.filter((e) => visible.has(e.source) && visible.has(e.target)
        && !reasons.has(pair(e.source, e.target))),
      paths,
    } : graph;
    return {graph: filtered, cuts, routeCuts, unknown: unknown.filter((e) => visible.has(e.source)), rates};
  }

  function glyphs(result, xOf, byId, xScale, py, half) {
    const rows = new Map();
    return result.cuts.map(({edge, reason}) => {
      const nd = byId[edge.source], x = xOf[edge.source];
      if (x == null || !nd) { return ''; }
      const row = rows.get(edge.source) || 0;
      rows.set(edge.source, row + 1);
      const y = Number.isFinite(nd.rel_energy) ? py(nd.rel_energy) : 26;
      return `<text class="pw-filter-cut" x="${xScale(x) + half + 5}" y="${y + 15 * row}" fill="#d9534f" font-size="14"><title>${esc(edge.source + '→' + edge.target + ': ' + reason)}</title>⊣</text>`;
    }).join('');
  }

  function markRoutes(result) {
    document.querySelectorAll('.pw-path-btn').forEach((button) => {
      if (button._filterBase == null) { button._filterBase = button.innerHTML; button._filterTitle = button.title; }
      const cut = result.routeCuts[Number(button.dataset.path)];
      button.innerHTML = button._filterBase + (cut ? ' · ' + esc(cut.label) : '');
      button.title = button._filterTitle + (cut ? '\n' + cut.label + ': ' + cut.reason : '');
    });
  }

  function environmentText(environment) {
    const e = environment || {}, bits = [];
    bits.push(Number.isFinite(e.T) ? `T = ${fmt(e.T)} K` : 'T not recorded');
    if (Number.isFinite(e.p_total)) { bits.push(`${fmt(e.p_total)} bar`); }
    const pressures = Object.entries(e.pressures || {});
    if (pressures.length) { bits.push(pressures.map(([gas, p]) => `p(${gas}) = ${fmt(p)} bar`).join(', ')); }
    else if (!Number.isFinite(e.p_total)) { bits.push('pressure not recorded'); }
    if (Number.isFinite(e.U)) { bits.push(`U = ${fmt(e.U)} V RHE`); }
    if (Number.isFinite(e.pH)) { bits.push(`pH = ${fmt(e.pH)}`); }
    return 'flux at ' + bits.join(', ') + '; re-solve to update';
  }

  function honesty(environment, current) {
    const e = environment || {};
    const axes = ['T', 'p_total', 'U', 'pH'].filter((k) => Number.isFinite(current[k]));
    const stored = e.pressures || {}, selected = current.pressures || {};
    const gases = new Set([...Object.keys(stored), ...Object.keys(selected)]);
    const compositionChanged = current.pressures != null && [...gases].some((gas) =>
      !Number.isFinite(stored[gas]) || !Number.isFinite(selected[gas])
      || Math.abs(stored[gas] - selected[gas]) > 1e-8);
    return compositionChanged || axes.some((k) => !Number.isFinite(e[k]) || Math.abs(e[k] - current[k]) > 1e-8)
      ? 'not computed at the selected environment' : 'stored';
  }

  function mount(host, getLayers, state, current, records, changed) {
    if (!host) { return {update() {}}; }
    // Static markup only. Stored labels/conditions below always use textContent.
    host.innerHTML = '<summary class="cursor-pointer font-semibold">Visual filters</summary>'
      + '<label class="block mt-2"><input id="pw-filter-barrier-on" type="checkbox"> Barrier threshold (eV)</label>'
      + '<input id="pw-filter-barrier" type="range" min="0" max="10" step="0.001" list="pw-filter-ticks" aria-label="Barrier threshold in eV">'
      + '<output id="pw-filter-barrier-value"></output><datalist id="pw-filter-ticks"></datalist>'
      + '<div id="pw-filter-tick-buttons"></div>'
      + '<label class="block mt-2"><input id="pw-filter-turnover-on" type="checkbox"> Turnover floor (/s/site)</label>'
      + '<input id="pw-filter-turnover" type="range" min="-120" max="12" step="1" aria-label="Log10 turnover floor per second per site">'
      + '<output id="pw-filter-turnover-value"></output><p id="pw-filter-rates" class="mt-2"></p>'
      + '<p id="pw-filter-unknown" class="mt-2"></p>';
    const el = (name) => document.getElementById('pw-filter-' + name);
    for (const name of ['barrier', 'turnover']) {
      el(name + '-on').addEventListener('change', () => {
        getLayers()[name + '_filter'] = el(name + '-on').checked;
        changed();
      });
      el(name).addEventListener('input', () => {
        state[name] = Number(el(name).value);
        getLayers()[name + '_filter'] = true;
        changed();
      });
    }
    function update(result) {
      const env = current(), ticks = thermalTicks(env.T), layers = getLayers();
      const threshold = state.barrier == null ? ticks[1].eV : state.barrier;
      el('barrier').value = String(threshold);
      el('barrier-on').checked = layers.barrier_filter;
      el('turnover-on').checked = layers.turnover_filter;
      el('barrier-value').textContent = ` ${threshold.toFixed(3)} eV${layers.barrier_filter ? '' : ' (off)'}`;
      el('turnover').value = String(state.turnover);
      el('turnover-value').textContent = ` ${fmt(10 ** state.turnover)} /s/site`;
      el('ticks').innerHTML = ticks.map((t) => `<option value="${t.eV}">${t.label}</option>`).join('');
      const buttons = el('tick-buttons');
      buttons.innerHTML = '';
      ticks.forEach((tick) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'mr-2 underline';
        button.textContent = `${tick.label}: ${tick.eV.toFixed(2)} eV`;
        button.title = `Thermal equivalent at ${fmt(env.T)} K; select to enable barrier filtering`;
        button.addEventListener('click', () => {
          state.barrier = tick.tau === 1 ? null : tick.eV;
          layers.barrier_filter = true;
          changed();
        });
        buttons.appendChild(button);
      });
      const used = result ? [...new Set([...result.rates.values()].map((r) => r.source))] : [];
      el('rates').textContent = used.length ? records.filter((r) => used.includes(r.source))
        .map((r) => `${r.source} — ${honesty(r.environment, env)}; ${environmentText(r.environment)}`).join('. ')
        : 'No stored per-step rates — turnover leaves these steps visible.';
      el('unknown').textContent = result && result.unknown.length
        ? 'No barrier known: ' + result.unknown.map((e) => `${e.source}→${e.target}`).join(', ') + '. These steps stay visible under the barrier filter.' : '';
    }
    return {update};
  }

  root.PathwayFilters = {thermalTicks, read, write, rateIndex, barrier, traverse, glyphs, markRoutes, environmentText, honesty, mount};
})(globalThis);
