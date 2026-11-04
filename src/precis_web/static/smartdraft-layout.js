/* Layout lives outside #sd-content's replaced children: reader navigation
   reapplies it, while delegated controls keep working on the new markup. */
// sd-layout-pure:begin
function sdLayoutState(raw, width) {
  const saved = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
  const state = {};
  const sides = ['search', 'collaborate'];
  for (const side of sides) {
    const value = saved[side];
    // Cap before adding widths so even finite values near MAX_VALUE are safe.
    state[side] = Math.max(220, Math.min(width - 372,
      typeof value === 'number' && Number.isFinite(value) && value > 0
        ? value : width * .28));
    state[side + 'Hidden'] = saved[side + 'Hidden'] === true;
  }
  const visible = sides.filter(side => !state[side + 'Hidden']);
  const budget = width - 360 - visible.length * 12;
  const extra = visible.reduce((sum, side) => sum + state[side] - 220, 0);
  const available = Math.max(0, budget - visible.length * 220);
  if (extra > available) {
    for (const side of visible) {
      state[side] = 220 + (state[side] - 220) * available / extra;
    }
  }
  return state;
}
// sd-layout-pure:end

(() => {
  const root = document.getElementById('sd-content');
  if (!root) return;
  const key = 'precis.smartdraft.layout.v1';
  const sides = ['search', 'collaborate'];
  const desktop = window.matchMedia('(min-width: 1024px)');
  let state = {};
  let drag = null;
  try { state = JSON.parse(window.localStorage.getItem(key)); } catch (_) {}

  function persist() {
    try { window.localStorage.setItem(key, JSON.stringify(state)); } catch (_) {}
  }
  function wide() {
    return desktop.matches && root.clientWidth >= 824;
  }
  function gripFor(side) {
    return root.querySelector('[data-sd-resize="' + side + '"]');
  }
  function refresh() {
    const active = wide();
    root.classList.toggle('sd-layout-wide', active);
    if (active) state = sdLayoutState(state, root.clientWidth);
    for (const side of sides) {
      const hidden = active && state[side + 'Hidden'];
      root.querySelector('#sd-' + side).hidden = !!hidden;
      root.querySelector('[data-sd-show="' + side + '"]').hidden = !hidden;
      const grip = gripFor(side);
      grip.hidden = !active || !!hidden;
      if (active) {
        const other = side === 'search' ? 'collaborate' : 'search';
        const otherWidth = state[other + 'Hidden'] ? 0 : state[other] + 12;
        grip.setAttribute('aria-valuemin', '220');
        grip.setAttribute('aria-valuemax', String(Math.floor(root.clientWidth - 372 - otherWidth)));
        grip.setAttribute('aria-valuenow', String(Math.round(state[side])));
        grip.setAttribute('aria-valuetext', Math.round(state[side]) + ' pixels');
      }
    }
    root.style.gridTemplateColumns = active
      ? (state.searchHidden ? '0px 0px ' : state.search + 'px 12px ')
        + 'minmax(360px, 1fr) '
        + (state.collaborateHidden ? '0px 0px' : '12px ' + state.collaborate + 'px')
      : '';
    // Explicit columns keep Focus in track 3 when a side is display:none.
    root.querySelector('#sd-search').style.gridColumn = active ? '1' : '';
    gripFor('search').style.gridColumn = active ? '2' : '';
    root.querySelector('#sd-focus').style.gridColumn = active ? '3' : '';
    gripFor('collaborate').style.gridColumn = active ? '4' : '';
    root.querySelector('#sd-collaborate').style.gridColumn = active ? '5' : '';
  }
  function resize(side, width) {
    const other = side === 'search' ? 'collaborate' : 'search';
    const otherWidth = state[other + 'Hidden'] ? 0 : state[other] + 12;
    state[side] = Math.max(220, Math.min(root.clientWidth - 372 - otherWidth, width));
    refresh();
  }
  function show(side) {
    if (!sides.includes(side) || !wide()) return;
    state = sdLayoutState(state, root.clientWidth);
    state[side + 'Hidden'] = false;
    refresh();
    persist();
  }
  root.addEventListener('click', event => {
    const hide = event.target.closest('[data-sd-hide]');
    const expand = event.target.closest('[data-sd-show]');
    if (!wide() || (!hide && !expand)) return;
    if (hide) {
      const side = hide.dataset.sdHide;
      state[side + 'Hidden'] = true;
      refresh();
      persist();
      root.querySelector('[data-sd-show="' + side + '"]').focus();
    } else {
      const side = expand.dataset.sdShow;
      show(side);
      root.querySelector('[data-sd-hide="' + side + '"]').focus();
    }
  });
  root.addEventListener('keydown', event => {
    const grip = event.target.closest('[data-sd-resize]');
    if (!grip && !event.target.closest('[data-sd-hide]')
        && !event.target.closest('[data-sd-show]')) return;
    // Keep native button/tab behavior without triggering reader shortcuts.
    event.stopPropagation();
    if (!grip || !wide()) return;
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    const side = grip.dataset.sdResize;
    const direction = (event.key === 'ArrowRight' ? 1 : -1)
      * (side === 'search' ? 1 : -1);
    resize(side, state[side] + direction * (event.shiftKey ? 50 : 10));
    persist();
  });
  root.addEventListener('pointerdown', event => {
    const grip = event.target.closest('[data-sd-resize]');
    if (!grip || !wide() || event.button !== 0 || !event.isPrimary) return;
    event.preventDefault();
    const side = grip.dataset.sdResize;
    drag = { grip, side, pointer: event.pointerId, x: event.clientX, width: state[side] };
    grip.focus();
    grip.setPointerCapture(event.pointerId);
    root.classList.add('sd-layout-dragging');
  });
  root.addEventListener('pointermove', event => {
    if (!drag || drag.pointer !== event.pointerId || !wide()) return;
    const delta = (event.clientX - drag.x) * (drag.side === 'search' ? 1 : -1);
    resize(drag.side, drag.width + delta);
  });
  function endDrag(event) {
    if (!drag || (event && drag.pointer !== event.pointerId)) return;
    const previous = drag;
    drag = null;
    if (previous.grip.hasPointerCapture(previous.pointer)) {
      previous.grip.releasePointerCapture(previous.pointer);
    }
    root.classList.remove('sd-layout-dragging');
    persist();
  }
  root.addEventListener('pointerup', endDrag);
  root.addEventListener('pointercancel', endDrag);
  root.addEventListener('lostpointercapture', endDrag);
  window.addEventListener('resize', () => { endDrag(); refresh(); });
  window.__sdLayout = { refresh: () => { endDrag(); refresh(); }, show };
  refresh();
})();
