// Keyboard shortcuts for the web UI. Plain JS, no dependencies, loaded
// `defer` from base.html.j2 beside tour.js.
//
// Contract with templates: an element opts in with
//   data-shortcut="<keys>"        "/", "e", or a chord such as "g d"
//   data-shortcut-label="<name>"  optional; falls back to aria-label,
//                                 title, then the element's text
// and the key does what a click (or, for inputs, a focus) on it does.
// `?` builds a <dialog> from whatever [data-shortcut] the page carries at
// that moment, so the help list can never drift from the page.

(function () {
  'use strict';

  const CHORD_WINDOW_MS = 1500;
  const CHORD_LEAD = 'g';

  // Same guard as the graph page's key handler: never steal typing, never
  // fight a browser/OS shortcut, never fire on auto-repeat or an IME.
  function ignored(ev) {
    const t = ev.target;
    return (
      ev.ctrlKey || ev.metaKey || ev.altKey ||
      ev.isComposing || ev.repeat || ev.defaultPrevented ||
      !!(t && (t.isContentEditable || /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName)))
    );
  }

  function labelOf(el) {
    return (
      el.dataset.shortcutLabel ||
      el.getAttribute('aria-label') ||
      (el.getAttribute('title') || '').replace(/\s*\([^)]*\)\s*$/, '') ||
      (el.textContent || '').trim().replace(/\s+/g, ' ')
    );
  }

  function visible(el) {
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  }

  // Elements bound to `keys`. The page's own control wins over the header's
  // (Drive's query box beats the global search), so page content is listed
  // before anything inside <header>.
  function targets(keys) {
    const all = [...document.querySelectorAll('[data-shortcut]')]
      .filter((e) => e.dataset.shortcut === keys);
    const inHeader = (e) => (e.closest('header') ? 1 : 0);
    return all.sort((a, b) => inHeader(a) - inHeader(b));
  }

  function fire(el) {
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) {
      el.focus();
      if (typeof el.select === 'function') el.select();
    } else {
      el.click();
    }
  }

  function trigger(keys) {
    const list = targets(keys);
    // Prefer a visible target; a collapsed dropdown anchor still navigates.
    const el = list.find(visible) || list[0];
    if (!el) return false;
    fire(el);
    return true;
  }

  // ── help overlay ───────────────────────────────────────────────────
  let dialog = null;

  function closeHelp() {
    if (dialog && dialog.open) dialog.close();
  }

  function openHelp() {
    if (dialog) dialog.remove();
    const rows = new Map();
    for (const el of document.querySelectorAll('[data-shortcut]')) {
      const keys = el.dataset.shortcut;
      if (!rows.has(keys)) rows.set(keys, labelOf(el));
    }
    rows.set('?', 'This help');
    rows.set('Esc', 'Close / leave a field');

    dialog = document.createElement('dialog');
    dialog.setAttribute('aria-label', 'Keyboard shortcuts');
    dialog.className =
      'rounded-lg border border-line bg-surface text-fg shadow-xl p-4 max-w-md w-full';
    const h = document.createElement('h2');
    h.className = 'text-sm font-semibold mb-2';
    h.textContent = 'Keyboard shortcuts';
    dialog.appendChild(h);
    const dl = document.createElement('dl');
    dl.className = 'grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm';
    for (const [keys, label] of rows) {
      const dt = document.createElement('dt');
      for (const k of keys.split(' ')) {
        const kbd = document.createElement('kbd');
        kbd.className = 'font-mono text-xs border border-line rounded px-1 mr-1';
        kbd.textContent = k;
        dt.appendChild(kbd);
      }
      const dd = document.createElement('dd');
      dd.className = 'text-fg-muted';
      dd.textContent = label;
      dl.appendChild(dt);
      dl.appendChild(dd);
    }
    dialog.appendChild(dl);
    dialog.addEventListener('click', (e) => { if (e.target === dialog) closeHelp(); });
    dialog.addEventListener('close', () => { dialog.remove(); dialog = null; });
    document.body.appendChild(dialog);
    dialog.showModal();
  }

  // ── key handling ───────────────────────────────────────────────────
  let pending = null;  // timer id while a g-chord is armed

  function disarm() {
    if (pending !== null) { clearTimeout(pending); pending = null; }
  }

  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') {
      // Native <dialog> closes itself on Esc; otherwise leave a field.
      disarm();
      const t = document.activeElement;
      if (t && t !== document.body && /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName)) t.blur();
      return;
    }
    if (ignored(ev)) return;

    if (pending !== null) {
      disarm();
      if (trigger(CHORD_LEAD + ' ' + ev.key)) ev.preventDefault();
      return;
    }
    if (ev.key === CHORD_LEAD) {
      pending = setTimeout(disarm, CHORD_WINDOW_MS);
      return;
    }
    if (ev.key === '?') {
      ev.preventDefault();
      if (dialog && dialog.open) closeHelp(); else openHelp();
      return;
    }
    if (ev.key === '/' || ev.key.length === 1) {
      if (document.querySelector('dialog[open]')) return;
      if (trigger(ev.key)) ev.preventDefault();
    }
  });

  window.precisShortcuts = { trigger, targets, ignored, labelOf };
})();
