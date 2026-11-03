#!/usr/bin/env node
// Smoke test for the smartdraft reader's prefetch/render cache
// (static/sd-prefetch.js, gr553319). Run by tests/test_sd_prefetch_js.py
// (skips without node). Exits 0 when every check passes, 1 on the first
// failure.

import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import assert from 'node:assert/strict';

const here = dirname(fileURLToPath(import.meta.url));
const { createRenderCache } = await import(
  resolve(here, '../src/precis_web/static/sd-prefetch.js')
);

const ent = (v, extra) => ({ html: '<p>h</p>', v, focus: 'dc1', focusHandle: 'b1', ...extra });

// ── LRU get/put + cap ────────────────────────────────────────────────
{
  const rc = createRenderCache({ max: 3 });
  rc.put('/p?focus=dc1', ent('v1'));
  rc.put('/p?focus=dc2', ent('v1'));
  rc.put('/p?focus=dc3', ent('v1'));
  assert.equal(rc.size, 3);
  rc.get('/p?focus=dc1'); // touch — dc1 is now newest, dc2 oldest
  rc.put('/p?focus=dc4', ent('v1'));
  assert.equal(rc.size, 3);
  assert.ok(rc.get('/p?focus=dc1'), 'touched entry survives eviction');
  assert.ok(!rc.get('/p?focus=dc2'), 'least-recently-used entry evicted');
}
// Unversioned renders are never cached — an empty `v` can't revalidate.
{
  const rc = createRenderCache();
  assert.equal(rc.put('/p?focus=dc1', ent('')), false);
  assert.equal(rc.put('/p?focus=dc1', ent(null)), false);
  assert.equal(rc.size, 0);
}

// ── markStale(): a proven-old entry is flagged for the hit path ──────
{
  const rc = createRenderCache();
  rc.put('/p?focus=dc1', ent('v1'));
  rc.markStale('/p?focus=dc1');
  assert.equal(rc.get('/p?focus=dc1').stale, true, 'entry flagged stale');
  rc.markStale('/p?focus=nope'); // absent — must not throw
  rc.put('/p?focus=dc1', ent('v2'));
  assert.equal(rc.get('/p?focus=dc1').stale, undefined, 'a fresh put clears the flag');
}

// ── wanted(): the ±3 window in order, plus hits in search mode ────────
{
  const rc = createRenderCache({ radius: 3 });
  const order = Array.from({ length: 10 }, (_, i) => [`dc${i}`, 1]);
  const urlFor = (dc) => '/p?focus=' + dc;
  const want = rc.wanted('dc4', order, [], urlFor);
  assert.deepEqual(
    [...want].sort(),
    ['dc1', 'dc2', 'dc3', 'dc4', 'dc5', 'dc6', 'dc7'].map((d) => '/p?focus=' + d),
    '±3 in reading order, centred on the focus',
  );
  // Edges clamp; hits add their own window when the focus isn't in order.
  const edge = rc.wanted('dc0', order, [], urlFor);
  assert.deepEqual([...edge].sort(), ['dc0', 'dc1', 'dc2', 'dc3'].map((d) => '/p?focus=' + d));
  const hs = ['dc0', 'dc5', 'dc9'];
  const withHits = rc.wanted('dc9', order, hs, urlFor);
  assert.ok(withHits.has('/p?focus=dc5'), 'hit neighbour included');
  assert.ok(withHits.has('/p?focus=dc0'), 'the hits window is ±3 in hits, not order');
  assert.ok(withHits.has('/p?focus=dc6'), 'order neighbour included');
  assert.ok(!withHits.has('/p?focus=dc3'), 'outside both windows');
}

// ── track(): shared fetch, {status, entry} — ok/error/declined ───────
{
  const rc = createRenderCache();
  let fetches = 0;
  const fetcher = async (url, signal) => {
    fetches++;
    return ent('v9', { focus: 'dc7', focusHandle: 'b7' });
  };
  const [a, b] = await Promise.all([rc.track('/p?focus=dc7', fetcher), rc.track('/p?focus=dc7', fetcher)]);
  assert.equal(fetches, 1, 'overlapping tracks share one fetch');
  assert.equal(a.status, 'ok');
  assert.equal(a.entry.v, 'v9');
  assert.equal(b.entry.v, 'v9');
  assert.equal(rc.get('/p?focus=dc7').v, 'v9', 'resolved entry is cached');
  // A fetcher's rejection is an 'error' — distinct from an abort.
  const failed = await rc.track('/p?focus=dc8', async () => { throw new Error('net down'); });
  assert.equal(failed.status, 'error', 'fetcher rejection resolves error, never throws');
  assert.equal(failed.entry, null);
  // A declined fetch (non-200, missing panel) is ok-but-empty.
  const declined = await rc.track('/p?focus=dc8b', async () => null);
  assert.equal(declined.status, 'ok');
  assert.equal(declined.entry, null);
  assert.equal(rc.inflightCount, 0, 'settled tracks unregister');
}

// ── abort ≠ error: a reaped fetch resolves 'aborted' ──────────────────
{
  const rc = createRenderCache();
  let sig;
  // Real fetch() rejects with AbortError on abort — model that.
  const p = rc.track('/p?focus=dc1', (url, s) => {
    sig = s;
    return new Promise((_, rej) =>
      s.addEventListener('abort', () => rej(Object.assign(new Error('killed'), { name: 'AbortError' }))));
  });
  await Promise.resolve();   // fetchers run in a microtask — let it register
  rc.reap(new Set(['/p?focus=dc9']));   // dc1 outside the keep set → abort
  assert.equal(sig.aborted, true);
  const r = await p;
  assert.equal(r.status, 'aborted', 'a reaped fetch is aborted, not failed — nav callers must not reload');
}

// ── reap(): a jump aborts the stale window's UNPINNED prefetches ──────
{
  const rc = createRenderCache();
  const order = Array.from({ length: 10 }, (_, i) => [`dc${i}`, 1]);
  const urlFor = (dc) => '/p?focus=' + dc;
  const signals = new Map();
  const hanging = (url, signal) => {
    signals.set(url, signal);
    return new Promise(() => {}); // never resolves on its own
  };
  // Simulate the ±3 window around dc4 being prefetched…
  for (const url of rc.wanted('dc4', order, [], urlFor)) rc.track(url, hanging);
  assert.equal(rc.inflightCount, 7);
  await Promise.resolve();   // fetchers run in a microtask — let them register signals
  // …then the user jumps to dc9: keep = dc6..dc9 ∪ {current} — overlap
  // with the old window is dc6/dc7, everything else aborts.
  const keep = rc.wanted('dc9', order, [], urlFor);
  keep.add('/p?focus=dc9');
  const reaped = rc.reap(keep);
  assert.equal(reaped, 5, 'dc1–dc5 aborted (dc6–dc7 survive inside the new window)');
  assert.equal(rc.inflightCount, 2);
  assert.equal(signals.get('/p?focus=dc1').aborted, true, 'stale prefetch aborted');
  assert.equal(signals.get('/p?focus=dc6').aborted, false, 'still-wanted prefetch kept');
}

// ── pinned (real nav) fetches are never aborted by reap or clear ──────
{
  const rc = createRenderCache();
  let sig;
  const p = rc.track(
    '/p?focus=dc5',
    (url, s) => { sig = s; return new Promise((res) => setTimeout(() => res(ent('v5')), 5)); },
    { pinned: true },
  );
  await Promise.resolve();
  rc.reap(new Set(['/p?focus=dc9']));   // outside the new window — pinned survives
  assert.equal(sig.aborted, false, 'reap never aborts a nav-awaited fetch');
  rc.clear();                           // an edit mid-nav — pinned still survives
  assert.equal(sig.aborted, false, 'clear never aborts a nav-awaited fetch');
  const r = await p;
  // The clear moved the epoch: the resolve is stale — never cached, and
  // the awaiting nav leaves the post-mutation refresh to paint.
  assert.equal(r.status, 'stale');
  assert.equal(r.entry.v, 'v5');
  assert.ok(!rc.get('/p?focus=dc5'), 'a stale resolve must not enter the cache');
}
// …and without the intervening clear, a pinned nav fetch resolves ok:
{
  const rc = createRenderCache();
  let sig;
  const p = rc.track(
    '/p?focus=dc5',
    (url, s) => { sig = s; return new Promise((res) => setTimeout(() => res(ent('v5')), 5)); },
    { pinned: true },
  );
  await Promise.resolve();
  rc.reap(new Set(['/p?focus=dc9']));
  assert.equal(sig.aborted, false);
  const r = await p;
  assert.equal(r.status, 'ok');
  assert.equal(rc.get('/p?focus=dc5').v, 'v5', 'nav fetch still lands in the cache');
}
// A nav attaching to a prefetch's in-flight fetch upgrades it to pinned
// — the shared request then survives reap too (the reported bug: arrow
// during a prefetch of the target reaped the fetch the nav awaited).
{
  const rc = createRenderCache();
  let sig;
  rc.track('/p?focus=dc3', (url, s) => { sig = s; return new Promise((res) => setTimeout(() => res(ent('v3')), 5)); });
  const nav = rc.track('/p?focus=dc3', async () => ent('other'), { pinned: true });
  assert.equal(rc.inflightCount, 1, 'nav shares the prefetch fetch');
  await Promise.resolve();
  rc.reap(new Set(['/p?focus=dc9']));
  assert.equal(sig.aborted, false, 'upgraded fetch survives reap');
  const r = await nav;
  assert.equal(r.status, 'ok');
  assert.equal(r.entry.v, 'v3', 'nav gets the prefetch result');
}

// ── post-clear supersede: never share a possibly pre-write fetch ──────
{
  const rc = createRenderCache();
  const first = rc.track(
    '/p?focus=dc2',
    (url, s) => new Promise((res) => setTimeout(() => res(ent('v1')), 15)),
    { pinned: true },
  );
  await Promise.resolve();
  rc.clear();
  let secondRan = false;
  const second = rc.track(
    '/p?focus=dc2',
    async () => { secondRan = true; return ent('v2'); },
    { pinned: true },
  );
  const [r1, r2] = await Promise.all([first, second]);
  assert.equal(secondRan, true, 'a post-clear track starts a fresh fetch, not the stale share');
  assert.equal(r1.status, 'stale');
  assert.equal(r2.status, 'ok');
  assert.equal(rc.get('/p?focus=dc2').v, 'v2');
}

// ── dropFocusHandle(): invalidate the edited block's entry ───────────
{
  const rc = createRenderCache();
  rc.put('/p?focus=dc1', ent('v1', { focus: 'dc1', focusHandle: 'H1' }));
  rc.put('/p?focus=dc2', ent('v1', { focus: 'dc2', focusHandle: 'H2' }));
  assert.equal(rc.dropFocusHandle('H1'), 1);
  assert.ok(!rc.get('/p?focus=dc1'), 'edited block\'s entry dropped');
  assert.ok(rc.get('/p?focus=dc2'), 'other entries untouched');
  assert.equal(rc.dropFocusHandle('nope'), 0);
}

// ── clear(): a post-mutation reset can't be re-poisoned ──────────────
{
  const rc = createRenderCache();
  rc.put('/p?focus=dc1', ent('v1'));
  let signal;
  const slow = (u, s) => { signal = s; return new Promise((r) => setTimeout(() => r(ent('v1')), 20)); };
  const inFlight = rc.track('/p?focus=dc9', slow);
  await Promise.resolve();   // let the fetcher start and receive its signal
  rc.clear();
  assert.equal(signal.aborted, true, 'clear aborts unpinned in-flight fetches');
  assert.equal(rc.size, 0);
  const stale = await inFlight;
  assert.equal(stale.status, 'aborted');
  assert.ok(!rc.get('/p?focus=dc9'), 'a pre-clear resolve must not re-enter the cache');
}

console.log('sd-prefetch smoke: ok');
